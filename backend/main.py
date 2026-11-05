"""ERP Bridge v2 composition root.

Business logic is intentionally kept outside this file. Existing inventory,
monitoring, console and transfer URLs are preserved.
"""
from fastapi import FastAPI

from api.health import router as health_router
from api.inventory import router as inventory_router
from api.pagination import router as pagination_router
from api.purchase_orders import router as po_router
from console_routes import router as console_router
from observability import router as monitoring_router, install_monitoring
from writeback import router as writeback_router, init_writeback_table

app = FastAPI(title="ERP Bridge 2.0", version="2.0.0-demo")

# Include existing modules before the new PO module.
app.include_router(health_router)
app.include_router(inventory_router)
app.include_router(pagination_router)
app.include_router(console_router)
install_monitoring(app)
app.include_router(monitoring_router)
init_writeback_table()
app.include_router(writeback_router)
app.include_router(po_router)

# Retain useful import compatibility for project scripts.
from api.inventory import CanonicalInventoryItem, InventoryItem  # noqa: E402,F401
from api.pagination import sync_page_for_tenant  # noqa: E402,F401
