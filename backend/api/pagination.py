"""Legacy paginated inventory sync extracted without altering its checkpoint semantics.

Page numbers alone do not guarantee consistency if upstream page membership changes.
The new PO engine has a separate source-sequenced feed and snapshot reconciliation.
"""
from uuid import uuid4
import httpx
from fastapi import APIRouter, HTTPException
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from db import engine, TenantInventoryRecord, SyncCheckpoint, SyncRun
from mapping_engine import inspect_batch, normalize_inventory, resolve_mapping
from tenant_config import load_tenants
from api.inventory import CanonicalInventoryItem

router = APIRouter(tags=["Inventory pagination and sync runs"])


@router.get("/integration/pagination-preview")
def preview_paginated_inventory():

    url = "http://127.0.0.1:8001/erp/customer_d/inventory/paginated"

    page = 1
    page_size = 5

    total_records = 0
    page_results = []

    while True:

        # Fetch one page from the ERP
        response = httpx.get(
            url,
            params={
                "page": page,
                "page_size": page_size
            },
            timeout=5.0
        )

        response.raise_for_status()
        data = response.json()

        items = data["items"]

        # Count the records on this page
        total_records += len(items)

        page_results.append({
            "page": page,
            "records_received": len(items)
        })

        # Stop when the ERP has no more pages
        if not data["has_more"]:
            break

        # Otherwise, fetch the next page
        page += 1

    return {
        "status": "success",
        "pages_fetched": len(page_results),
        "total_records": total_records,
        "pages": page_results
    }

def sync_page_for_tenant(
    tenant_id: str,
    page: int,
    run_id: str | None = None
):
    if page < 1:
        raise HTTPException(status_code=422, detail="Invalid page")

    # 1. Load the tenant configuration
    tenants = load_tenants()

    if tenant_id not in tenants:
        raise HTTPException(status_code=404, detail="Unknown tenant")

    config = tenants[tenant_id]
    url = config.get("paginated_inventory_url")

    if not url:
        raise HTTPException(
            status_code=400,
            detail="Pagination is not configured for this tenant"
        )

    # 2. Resolve the effective mapping
    mapping_id = config["mapping_id"]

    effective_mapping = resolve_mapping(
        mapping_id,
        config.get("mapping_overrides")
    )

    try:
        response = httpx.get(
            url,
            params={"page": page, "page_size": 5},
            timeout=5.0
        )

        response.raise_for_status()

    except httpx.HTTPStatusError as error:
        raise HTTPException(
            status_code=502,
            detail={
                "message": "External ERP returned an HTTP error",
                "tenant_id": tenant_id,
                "page": page,
                "upstream_status": error.response.status_code
            }
        ) from error

    except httpx.TimeoutException as error:
        raise HTTPException(
            status_code=504,
            detail="External ERP request timed out"
        ) from error

    except httpx.RequestError as error:
        raise HTTPException(
            status_code=503,
            detail="Cannot connect to external ERP"
        ) from error

    data = response.json()


    # 4. Inspect the complete page
    problem = inspect_batch(
        data["items"],
        mapping_id,
        mapping=effective_mapping
    )

    if problem is not None:
        raise HTTPException(status_code=422, detail=problem)


    # 5. Normalize and validate before changing the database
    items = []

    # Identify the ERP field containing the record version
    version_field = config.get("source_version_field")

    for raw_item in data["items"]:

        normalized = normalize_inventory(
            raw_item,
            mapping_id,
            mapping=effective_mapping
        )

        # Read the version from the customer's ERP
        if version_field:
            if version_field not in raw_item:
                raise HTTPException(
                    status_code=422,
                    detail=f"Missing source version: {version_field}"
                )

            normalized["source_version"] = raw_item[version_field]

        # Validate the complete canonical record
        item = CanonicalInventoryItem.model_validate(normalized)

        items.append(item)

    # 6. Save this page in one database transaction
    with Session(engine) as session:

        # Check the synchronization run, if one was provided
        if run_id is not None:

            run = session.get(SyncRun, run_id)

            if run is None or run.tenant_id != tenant_id:
                raise HTTPException(
                    status_code=404,
                    detail="Synchronization run not found"
                )

            if run.status not in ("pending", "running", "failed"):
                raise HTTPException(
                    status_code=409,
                    detail="This run cannot process more pages"
                )

            if page != run.last_completed_page + 1:
                raise HTTPException(
                    status_code=409,
                    detail="Pages must be processed in order"
                )

        # Convert validated inventory items into database rows
        rows = [
            {
                "tenant_id": tenant_id,
                "sku": item.sku,
                "warehouse": item.warehouse,
                "product_name": item.product_name,
                "stock_on_hand": item.stock_on_hand,
                "source_version": item.source_version
            }
            for item in items
        ]

        if rows:
            # Prepare a batch INSERT
            statement = sqlite_insert(
                TenantInventoryRecord
            ).values(rows)

            # Update records when the primary key already exists


            # Columns to update when the inventory record exists
            update_values = {
                "product_name": statement.excluded.product_name,
                "stock_on_hand": statement.excluded.stock_on_hand,
                "source_version": statement.excluded.source_version
            }

            # Protect versioned records from outdated updates
            if version_field:
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        "tenant_id",
                        "sku",
                        "warehouse"
                    ],
                    set_=update_values,
                    where=(
                        TenantInventoryRecord.source_version
                        < statement.excluded.source_version
                    )
                )
            else:
                # Preserve existing behavior for unversioned sources
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        "tenant_id",
                        "sku",
                        "warehouse"
                    ],
                    set_=update_values
                )

            # Execute the batch operation
            session.execute(statement)

        if run_id is not None:

            # Update this particular synchronization run
            run.last_completed_page = page

            run.status = (
                "completed"
                if not data["has_more"]
                else "running"
            )

        else:
            # Preserve our original checkpoint behavior
            checkpoint = session.get(
                SyncCheckpoint,
                (tenant_id, "inventory_paginated")
            )

            if checkpoint is None:
                checkpoint = SyncCheckpoint(
                    tenant_id=tenant_id,
                    sync_name="inventory_paginated",
                    last_completed_page=0,
                    status="not_started"
                )
                session.add(checkpoint)

            if page == checkpoint.last_completed_page + 1:
                checkpoint.last_completed_page = page
                checkpoint.status = (
                    "completed"
                    if not data["has_more"]
                    else "in_progress"
                )

            elif page > checkpoint.last_completed_page + 1:
                raise HTTPException(
                    status_code=409,
                    detail="Previous pages have not been checkpointed"
                )

        # Inventory and run progress committed together
        session.commit()

    return {
        "tenant_id": tenant_id,
        "page": page,
        "records_saved": len(items),
        "has_more": data["has_more"],
        "status": "success"
    }

@router.post("/tenants/{tenant_id}/sync-page")
def synchronize_one_page(tenant_id: str, page: int = 1):
    return sync_page_for_tenant(tenant_id, page)

@router.post("/tenants/{tenant_id}/sync-all-pages")
def synchronize_all_pages(tenant_id: str):

    page = 1
    total_processed = 0
    pages_processed = 0

    while True:

        # Temporary protection against infinite pagination
        if page > 100:
            raise HTTPException(
                status_code=502,
                detail="Pagination exceeded 100 pages"
            )

        # Reuse our existing page-processing function
        result = sync_page_for_tenant(tenant_id, page)

        # Update synchronization statistics
        total_processed += result["records_saved"]
        pages_processed += 1

        # Check whether the ERP has more pages
        if not result["has_more"]:
            break

        # Continue with the next page
        page += 1

    return {
        "tenant_id": tenant_id,
        "status": "success",
        "pages_processed": pages_processed,
        "total_processed": total_processed
    }

@router.post("/tenants/{tenant_id}/resume-sync")
def resume_tenant_sync(tenant_id: str):

    # 1. Verify that the tenant exists
    tenants = load_tenants()

    if tenant_id not in tenants:
        raise HTTPException(
            status_code=404,
            detail="Unknown tenant"
        )

    # 2. Read the last saved checkpoint
    with Session(engine) as session:
        checkpoint = session.get(
            SyncCheckpoint,
            (tenant_id, "inventory_paginated")
        )

        if checkpoint is None:
            raise HTTPException(
                status_code=409,
                detail="No checkpoint found"
            )

        if checkpoint.status == "completed":
            return {
                "status": "already_completed",
                "last_completed_page": checkpoint.last_completed_page
            }

        # Resume from the NEXT page
        page = checkpoint.last_completed_page + 1

    starting_page = page
    pages_processed = 0
    total_processed = 0

    # 3. Resume synchronization
    while True:

        if pages_processed >= 100:
            raise HTTPException(
                status_code=502,
                detail="Pagination safety limit reached"
            )

        result = sync_page_for_tenant(tenant_id, page)

        pages_processed += 1
        total_processed += result["records_saved"]

        if not result["has_more"]:
            break

        page += 1

    return {
        "tenant_id": tenant_id,
        "status": "completed",
        "resumed_from_page": starting_page,
        "pages_processed": pages_processed,
        "total_processed": total_processed
    }

@router.post("/tenants/{tenant_id}/sync-runs")
def create_sync_run(tenant_id: str):

    # 1. Verify the customer exists
    tenants = load_tenants()

    if tenant_id not in tenants:
        raise HTTPException(
            status_code=404,
            detail="Unknown tenant"
        )

    # 2. Generate a unique run identifier
    run_id = str(uuid4())

    # 3. Create a new synchronization run
    with Session(engine) as session:

        new_run = SyncRun(
            run_id=run_id,
            tenant_id=tenant_id,
            sync_name="inventory_paginated",
            status="pending",
            last_completed_page=0
        )

        session.add(new_run)
        session.commit()

    return {
        "run_id": run_id,
        "tenant_id": tenant_id,
        "status": "pending",
        "last_completed_page": 0
    }


@router.post("/tenants/{tenant_id}/sync-runs/{run_id}/pages/{page}")
def process_sync_run_page(
    tenant_id: str,
    run_id: str,
    page: int
):
    try:
        result = sync_page_for_tenant(
            tenant_id,
            page,
            run_id=run_id
        )

        return {
            "run_id": run_id,
            **result
        }

    except Exception:

        # Record a failure without losing progress
        with Session(engine) as session:

            run = session.get(SyncRun, run_id)

            if (
                run is not None
                and run.tenant_id == tenant_id
                and run.status in ("pending", "running", "failed")
                and page == run.last_completed_page + 1
            ):
                run.status = "failed"
                session.commit()

        # Preserve the original error response
        raise
