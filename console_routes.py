from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import SyncRun, engine
from tenant_config import load_tenants
from writeback import TransferAction

router = APIRouter(tags=["Local operations console"])
_CONSOLE_HTML = Path(__file__).with_name("bridge_console.html")
_LOCAL_CLIENTS = {"127.0.0.1", "::1", "testclient"}


def _require_local(request: Request):
    address = request.client.host if request.client else ""
    if address not in _LOCAL_CLIENTS:
        raise HTTPException(status_code=403, detail="Local-only training console")


def _check_tenant(tenant_id: str):
    if tenant_id not in load_tenants():
        raise HTTPException(status_code=404, detail="Unknown tenant")


@router.get("/console", include_in_schema=False)
def show_console(request: Request):
    _require_local(request)
    return FileResponse(_CONSOLE_HTML, media_type="text/html")


@router.get("/console/api/tenants")
def list_console_tenants(request: Request):
    """Expose metadata only, NEVER the ERP URLs or credentials from tenants.json."""
    _require_local(request)
    tenants = load_tenants()
    return {"tenants": [
        {
            "tenant_id": name,
            "mapping_id": conf.get("mapping_id", ""),
            "has_mapping_overrides": bool(conf.get("mapping_overrides")),
            "has_pagination": bool(conf.get("paginated_inventory_url")),
            "has_writeback": bool(conf.get("transfer_url")),
            "has_reconciliation": bool(conf.get("transfer_lookup_url")),
            "has_source_version": bool(conf.get("source_version_field")),
        }
        for name, conf in sorted(tenants.items())
    ]}


@router.get("/console/api/tenants/{tenant_id}/runs")
def list_console_runs(request: Request, tenant_id: str, limit: int = Query(50, ge=1, le=200)):
    _require_local(request)
    _check_tenant(tenant_id)
    with Session(engine) as session:
        runs = session.scalars(
            select(SyncRun)
            .where(SyncRun.tenant_id == tenant_id)
            .order_by(SyncRun.run_id.desc())
            .limit(limit)
        ).all()
        return {"tenant_id": tenant_id, "runs": [
            {
                "run_id": r.run_id,
                "sync_name": r.sync_name,
                "status": r.status,
                "last_completed_page": r.last_completed_page,
            }
            for r in runs
        ]}


@router.get("/console/api/tenants/{tenant_id}/actions")
def list_console_actions(request: Request, tenant_id: str, limit: int = Query(50, ge=1, le=200)):
    _require_local(request)
    _check_tenant(tenant_id)
    with Session(engine) as session:
        actions = session.scalars(
            select(TransferAction)
            .where(TransferAction.tenant_id == tenant_id)
            .order_by(TransferAction.action_id.desc())
            .limit(limit)
        ).all()
        return {"tenant_id": tenant_id, "actions": [
            {
                "action_id": a.action_id,
                "sku": a.sku,
                "from_warehouse": a.from_warehouse,
                "to_warehouse": a.to_warehouse,
                "quantity": a.quantity,
                "status": a.status,
                "erp_transfer_id": a.erp_transfer_id,
                "last_error": a.last_error,
            }
            for a in actions
        ]}
