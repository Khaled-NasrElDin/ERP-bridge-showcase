from pydantic import BaseModel, Field
from uuid import uuid4
from fastapi import FastAPI, HTTPException

app = FastAPI()

erp_inventory = [
    {
        "product_id": "ERP001",
        "name": "Coffee Beans",
        "quantity": 315,
        "warehouse": "New York"
    },
    {
        "product_id": "ERP002",
        "name": "Milk",
        "quantity": 120,
        "warehouse": "Boston"
    },
    {
        "product_id": "ERP003",
        "name": "Sugar",
        "quantity": 400,
        "warehouse": "Chicago"
    }
]

@app.get("/erp/inventory")
def get_erp_inventory():
    return {
        "source": "external_erp",
        "items": erp_inventory
    }

@app.get("/erp/customer_b/inventory")
def get_customer_b_inventory():

    return {
        "items": [
            {
                "itemCode": "ERP001",
                "description": "Coffee Beans",
                "availableQty": 920,
                "site": "New York"
            }
        ]
    }

@app.get("/erp/customer_d/inventory")
def get_customer_d_inventory():

    return {
        "items": [
            {
                "itemCode": "ERP001",
                "description": "Coffee Beans",
                "availableQty": 460,
                "depot": "New York"
            },
            {
                "itemCode": "ERP004",
                "description": "Tea",
                "availableQty": 175,
                "depot": "Boston"
            }
        ]
    }

@app.get("/erp/customer_d/inventory/paginated")
def get_paginated_inventory(page: int = 1, page_size: int = 5):

    # Generate 12 example inventory records
    items = [
        {
            "itemCode": f"PG{i:03d}",
            "description": f"Product {i}",
            "availableQty": 100 + i,
            "depot": "New York",
            "revision": 9
        }
        for i in range(1, 13)
    ]

    # Calculate the start and end positions
    start = (page - 1) * page_size
    end = start + page_size

    # Return only the requested records
    paginated_items = items[start:end]

    return {
        "page": page,
        "page_size": page_size,
        "total_items": len(items),
        "items": paginated_items,
        "has_more": end < len(items)
    }

# Simulated ERP transfer-order storage
transfer_orders = {}


class TransferRequest(BaseModel):
    sku: str
    from_warehouse: str
    to_warehouse: str
    quantity: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1)


@app.post("/erp/customer_d/transfers")
def create_transfer(request: TransferRequest):

    # A transfer must have different source and destination warehouses
    if request.from_warehouse == request.to_warehouse:
        raise HTTPException(
            status_code=422,
            detail="Source and destination must be different"
        )

    key = request.idempotency_key

    # The actual business request, excluding the retry identifier
    payload = request.model_dump(
        exclude={"idempotency_key"}
    )

    # Prevent duplicate transfer orders
    if key in transfer_orders:

        previous = transfer_orders[key]

        if previous["request"] != payload:
            raise HTTPException(
                status_code=409,
                detail="Idempotency key already used for another transfer"
            )

        return previous

    # Create a new transfer order
    transfer = {
        "transfer_id": str(uuid4()),
        "status": "created",
        "request": payload
    }

    transfer_orders[key] = transfer

    return transfer

@app.get("/erp/customer_d/transfers/by-key/{key}")
def lookup_transfer_by_key(key: str):
    if key not in transfer_orders:
        raise HTTPException(status_code=404, detail="Transfer not found")
    return transfer_orders[key]


# CUSTOMER E — Simulated external ERP

customer_e_inventory = [
    {"skuCode": "E1001", "itemName": "Laptop",
     "physicalUnits": 45, "storageSite": "Paris", "revision": 1},

    {"skuCode": "E1002", "itemName": "Monitor",
     "physicalUnits": 120, "storageSite": "London", "revision": 1},

    {"skuCode": "E1003", "itemName": "Keyboard",
     "physicalUnits": 230, "storageSite": "Paris", "revision": 1},

    {"skuCode": "E1004", "itemName": "Mouse",
     "physicalUnits": 300, "storageSite": "Berlin", "revision": 1},

    {"skuCode": "E1005", "itemName": "Headphones",
     "physicalUnits": 85, "storageSite": "London", "revision": 1},

    {"skuCode": "E1006", "itemName": "Webcam",
     "physicalUnits": 60, "storageSite": "Paris", "revision": 1},

    {"skuCode": "E1007", "itemName": "USB Hub",
     "physicalUnits": 150, "storageSite": "Berlin", "revision": 1}
]


@app.get("/erp/customer_e/inventory")
def get_customer_e_inventory():
    return {"items": customer_e_inventory}


@app.get("/erp/customer_e/inventory/paginated")
def get_customer_e_paginated(page: int = 1, page_size: int = 5):

    start = (page - 1) * page_size
    end = start + page_size

    return {
        "page": page,
        "page_size": page_size,
        "total_items": len(customer_e_inventory),
        "items": customer_e_inventory[start:end],
        "has_more": end < len(customer_e_inventory)
    }
