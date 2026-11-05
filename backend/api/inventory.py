"""Inventory HTTP endpoints extracted from the original main.py without changing contracts.

The global toy inventory and legacy integration endpoints remain for compatibility.
A later version can replace them with explicit services after contract tests exist.
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
import httpx

from db import engine, InventoryRecord, TenantInventoryRecord
from mapping_engine import (
    CUSTOMER_MAPPINGS, inspect_schema, inspect_batch,
    normalize_inventory, resolve_mapping,
)
from tenant_config import load_tenants

router = APIRouter(tags=["Inventory and normalization"])


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

@router.get("/inventory")
def get_inventory():
    return {
        "total_products": len(inventory),
        "products": inventory
    }

@router.post("/inventory", status_code=201)
def add_inventory_item(item: InventoryItem):
    inventory.append(item.model_dump())
    return item

@router.get("/integration/preview")
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


@router.get("/integration/normalized")
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

@router.get("/dashboard", include_in_schema=False)
def dashboard():
    # Compatibility shortcut: the original dashboard.html is not part of this project.
    return RedirectResponse(url="/console", status_code=307)


@router.post("/integration/sync")
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

@router.get("/integration/stored")
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

@router.post("/integration/normalize/{customer}")
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

@router.get("/tenants/{tenant_id}/inventory")
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

@router.post("/tenants/{tenant_id}/sync")
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
