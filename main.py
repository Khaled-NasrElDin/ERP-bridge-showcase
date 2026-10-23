from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from uuid import uuid4
from tenant_config import load_tenants
from mapping_engine import inspect_batch, resolve_mapping
from fastapi import HTTPException
from mapping_engine import (
    CUSTOMER_MAPPINGS,
    inspect_schema,
    inspect_batch,
    normalize_inventory
)
from fastapi import HTTPException
from sqlalchemy import select
import httpx
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from db import engine, InventoryRecord, TenantInventoryRecord, SyncCheckpoint, SyncRun
from pydantic import BaseModel, Field
from writeback import router as writeback_router, init_writeback_table
from observability import router as monitoring_router, install_monitoring
from console_routes import router as console_router

app = FastAPI()

app.include_router(console_router)

install_monitoring(app)
app.include_router(monitoring_router)

init_writeback_table()
app.include_router(writeback_router)

class InventoryItem(BaseModel):
    product_id: str
    name: str
    quantity: int = Field(ge=0)
    warehouse: str

class CanonicalInventoryItem(BaseModel):
    sku: str = Field(min_length=1)
    product_name: str = Field(min_length=1)
    stock_on_hand: int = Field(ge=0)
    warehouse: str = Field(min_length=1)
    source_version: int = Field(default=0, ge=0)

@app.get("/health")
def health_check():
    return {
        "status": "ok",
        "service": "ERP Bridge"
    }


inventory = [
    {
        "product_id": "P001",
        "name": "Laptop",
        "quantity": 150,
        "warehouse": "New York"
    },
    {
        "product_id": "P002",
        "name": "Monitor",
        "quantity": 80,
        "warehouse": "Boston"
    },
    {
        "product_id": "P003",
        "name": "Keyboard",
        "quantity": 300,
        "warehouse": "New York"
    }
]

@app.get("/inventory")
def get_inventory():
    return {
        "total_products": len(inventory),
        "products": inventory
    }

@app.post("/inventory", status_code=201)
def add_inventory_item(item: InventoryItem):
    inventory.append(item.model_dump())
    return item

@app.get("/integration/preview")
def preview_erp_inventory():

    url = "http://127.0.0.1:8001/erp/inventory"


    try:
        response = httpx.get(url, timeout=5.0)
        response.raise_for_status()

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="ERP connection timed out. Existing inventory was preserved."
        )

    except httpx.RequestError:
        raise HTTPException(
            status_code=503,
            detail="ERP is unavailable. Existing inventory was preserved."
        )

    except httpx.HTTPStatusError:
        raise HTTPException(
            status_code=502,
            detail="ERP returned an error. Existing inventory was preserved."
        )

    data = response.json()  

    return data


@app.get("/integration/normalized")
def get_normalized_inventory():

    url = "http://127.0.0.1:8001/erp/inventory"

    response = httpx.get(url, timeout=5.0)
    response.raise_for_status()

    data = response.json()

    normalized_items = []

    for raw_item in data["items"]:

        item = InventoryItem.model_validate(raw_item)

        normalized_product = {
            "sku": item.product_id,
            "product_name": item.name,
            "stock_on_hand": item.quantity,
            "warehouse": item.warehouse
        }

        normalized_items.append(normalized_product)

    return {
        "total_items": len(normalized_items),
        "items": normalized_items
    }

@app.get("/dashboard", include_in_schema=False)
def dashboard():
    # Compatibility shortcut: the original dashboard.html is not part of this project.
    return RedirectResponse(url="/console", status_code=307)


@app.post("/integration/sync")
def synchronize_inventory():

    # Step 1: Fetch data from the external ERP
    url = "http://127.0.0.1:8001/erp/inventory"

    response = httpx.get(url, timeout=5.0)
    response.raise_for_status()

    data = response.json()

    # Check the entire ERP batch before opening the database session
    problem = inspect_batch(data["items"], "customer_a")

    if problem is not None:
        raise HTTPException(
            status_code=422,
            detail=problem
        )

    created = 0
    updated = 0

    # Step 2: Open a database session
    
    # Open database session
    with Session(engine) as session:

        for raw_item in data["items"]:

            # Normalize the incoming ERP record
            normalized = normalize_inventory(raw_item, "customer_a")

            # Validate standardized data
            item = CanonicalInventoryItem.model_validate(normalized)

            # Update original inventory table
            existing = session.get(
                InventoryRecord,
                (item.sku, item.warehouse)
            )

            if existing:
                existing.product_name = item.product_name
                existing.stock_on_hand = item.stock_on_hand
                updated += 1

            else:
                new_record = InventoryRecord(
                    sku=item.sku,
                    product_name=item.product_name,
                    stock_on_hand=item.stock_on_hand,
                    warehouse=item.warehouse
                )
                session.add(new_record)
                created += 1

            # Update tenant-specific inventory table
            tenant_id = "customer_a"

            tenant_record = session.get(
                TenantInventoryRecord,
                (tenant_id, item.sku, item.warehouse)
            )

            if tenant_record is None:
                session.add(
                    TenantInventoryRecord(
                        tenant_id=tenant_id,
                        sku=item.sku,
                        product_name=item.product_name,
                        stock_on_hand=item.stock_on_hand,
                        warehouse=item.warehouse
                    )
                )

            else:
                tenant_record.product_name = item.product_name
                tenant_record.stock_on_hand = item.stock_on_hand

        # Commit ALL records from BOTH tables together
        session.commit()

    return {
        "status": "success",
        "created": created,
        "updated": updated,
        "total_processed": created + updated
    }

@app.get("/integration/stored")
def get_stored_inventory():

    with Session(engine) as session:

        statement = select(InventoryRecord)

        records = session.scalars(statement).all()

        result = []

        for record in records:
            result.append({
                "sku": record.sku,
                "product_name": record.product_name,
                "stock_on_hand": record.stock_on_hand,
                "warehouse": record.warehouse
            })

        return {
            "total_items": len(result),
            "items": result
        }

@app.post("/integration/normalize/{customer}")
def normalize_customer_inventory(customer: str, record: dict):

    # 1. Check that we know this customer
    if customer not in CUSTOMER_MAPPINGS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown customer: {customer}"
        )

    # 2. Inspect the incoming ERP fields
    inspection = inspect_schema(record, customer)

    # 3. Reject records with missing required fields
    if not inspection["valid"]:
        raise HTTPException(
            status_code=422,
            detail=inspection
        )

    # 4. Normalize the valid ERP record
    normalized = normalize_inventory(record, customer)

    return {
        "customer": customer,
        "data": normalized
    }

@app.get("/tenants/{tenant_id}/inventory")
def get_tenant_inventory(tenant_id: str):

    # Check that the tenant exists in our configuration
    if tenant_id not in load_tenants():
        raise HTTPException(
            status_code=404,
            detail="Unknown tenant"
        )

    with Session(engine) as session:

        # Retrieve ONLY this tenant's inventory
        statement = select(TenantInventoryRecord).where(
            TenantInventoryRecord.tenant_id == tenant_id
        )

        records = session.scalars(statement).all()

        return {
            "tenant_id": tenant_id,
            "total_items": len(records),
            "items": [
                {
                    "sku": record.sku,
                    "product_name": record.product_name,
                    "warehouse": record.warehouse,
                    "stock_on_hand": record.stock_on_hand,
                    "source_version": record.source_version
                }
                for record in records
            ]
        }

@app.post("/tenants/{tenant_id}/sync")
def synchronize_tenant_inventory(tenant_id: str):

    # 1. Load customer configuration
    tenants = load_tenants()

    if tenant_id not in tenants:
        raise HTTPException(
            status_code=404,
            detail="Unknown tenant"
        )

    config = tenants[tenant_id]
    url = config["inventory_url"]
    mapping_id = config["mapping_id"]

    if mapping_id not in CUSTOMER_MAPPINGS:
        raise HTTPException(
            status_code=500,
            detail="Tenant mapping is not configured"
        )
    # Build the effective mapping for this tenant
    effective_mapping = resolve_mapping(
        mapping_id,
        config.get("mapping_overrides")
)

    # 2. Fetch this customer's ERP data
    response = httpx.get(url, timeout=5.0)
    response.raise_for_status()
    data = response.json()

    # 3. Inspect the incoming batch
    problem = inspect_batch(
    data["items"],
    mapping_id,
    mapping=effective_mapping
)

    if problem is not None:
        raise HTTPException(
            status_code=422,
            detail=problem
        )

    # 4. Normalize and validate all records
    items = []

    for raw_item in data["items"]:
        normalized = normalize_inventory(
            raw_item,
            mapping_id,
            mapping=effective_mapping
        )
        item = CanonicalInventoryItem.model_validate(normalized)
        items.append(item)

    created = 0
    updated = 0

    # 5. Store inventory for the correct tenant
    with Session(engine) as session:

        for item in items:

            existing = session.get(
                TenantInventoryRecord,
                (tenant_id, item.sku, item.warehouse)
            )

            if existing is None:
                session.add(
                    TenantInventoryRecord(
                        tenant_id=tenant_id,
                        sku=item.sku,
                        warehouse=item.warehouse,
                        product_name=item.product_name,
                        stock_on_hand=item.stock_on_hand
                    )
                )
                created += 1

            else:
                existing.product_name = item.product_name
                existing.stock_on_hand = item.stock_on_hand
                updated += 1

        # Commit once, after processing every record
        session.commit()

    return {
        "tenant_id": tenant_id,
        "status": "success",
        "created": created,
        "updated": updated,
        "total_processed": created + updated
    }

@app.get("/integration/pagination-preview")
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

@app.post("/tenants/{tenant_id}/sync-page")
def synchronize_one_page(tenant_id: str, page: int = 1):
    return sync_page_for_tenant(tenant_id, page)

@app.post("/tenants/{tenant_id}/sync-all-pages")
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

@app.post("/tenants/{tenant_id}/resume-sync")
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

@app.post("/tenants/{tenant_id}/sync-runs")
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


@app.post("/tenants/{tenant_id}/sync-runs/{run_id}/pages/{page}")
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
