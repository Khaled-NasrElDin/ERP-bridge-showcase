"""Opt-in, localhost-only purchase-order integration sandbox.

Uses the source-tested Spherecast PO correctness core, a separate SQLite file,
and *synthetic* ERP clients. Does NOT connect to actual NetSuite or other ERPs.

All mutating demo routes require an environment opt-in AND a separate demo token.
Those are training controls, not a production identity/authorization system.
"""
import json
import os
import secrets
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from po_engine.demo import CUSTOM, MAPPING, ERP
from po_engine.integration import poll
from po_engine.monitor import freshness, reconcile, status
from po_engine.store import Store, one
from po_engine.writeback import request as request_eta, verify as verify_eta

router = APIRouter(prefix="/po", tags=["Purchase order sandbox (synthetic)"])

# Simulated source identifiers. These are NOT actual external vendor integrations.
_DEMO_SOURCES = {
    ("customer_a", "mock_standard"): MAPPING,
    ("customer_b", "mock_custom"): CUSTOM,
}
_demo_clients = {}  # Ephemeral in-memory mock ERP state (not for multiprocess servers).
_LOCAL_CLIENTS = {"127.0.0.1", "::1", "testclient"}


def _require_local(request: Request) -> None:
    address = request.client.host if request.client else ""
    if address not in _LOCAL_CLIENTS:
        raise HTTPException(status_code=403, detail="Local sandbox only")


def _require_source(tenant_id: str, source_id: str) -> None:
    if (tenant_id, source_id) not in _DEMO_SOURCES:
        raise HTTPException(status_code=404, detail="Unknown simulated tenant/source")


def _store() -> Store:
    default = Path(__file__).resolve().parents[1] / "po_integration_v2.sqlite"
    return Store(os.environ.get("ERP_BRIDGE_PO_SQLITE_PATH") or default)


def _demo_access(request: Request, x_demo_token: str | None) -> None:
    _require_local(request)
    if os.environ.get("ERP_BRIDGE_PO_DEMO_ENABLED") != "1":
        raise HTTPException(status_code=403, detail="PO demo mutations are disabled")
    secret = os.environ.get("ERP_BRIDGE_PO_DEMO_TOKEN", "")
    if not secret:
        raise HTTPException(status_code=503, detail="Demo token not configured")
    if not x_demo_token or not secrets.compare_digest(secret, x_demo_token):
        raise HTTPException(status_code=403, detail="Invalid demo token")


def _client(tenant_id: str, source_id: str) -> ERP:
    _require_source(tenant_id, source_id)
    erp = _demo_clients.get((tenant_id, source_id))
    if erp is None:
        raise HTTPException(status_code=409, detail="Simulated source not seeded in this process")
    return erp


def _source_summary(store: Store, tenant_id: str, source_id: str) -> dict:
    with store.tx() as db:
        row = one(db, "SELECT seq,checked,gap_target FROM cursors WHERE tenant=? AND source=?",
                  (tenant_id, source_id))
        count = one(db, "SELECT COUNT(*) AS n FROM records WHERE tenant=? AND source=? AND kind='po'",
                    (tenant_id, source_id))["n"]
    issues = [x for x in status(store, tenant_id, technical=True) if x["source"] == source_id]
    return {
        "tenant_id": tenant_id, "source_id": source_id, "simulated": True,
        "cursor": dict(row) if row else None, "purchase_order_count": count,
        "issue_count": len(issues),
        "consistency_note": "A cursor is not proof of completeness; inspect incident state and source contracts.",
    }


@router.get("/tenants/{tenant_id}/sources/{source_id}/status")
def po_source_status(request: Request, tenant_id: str, source_id: str):
    _require_local(request)
    _require_source(tenant_id, source_id)
    return _source_summary(_store(), tenant_id, source_id)


@router.get("/tenants/{tenant_id}/sources/{source_id}/purchase-orders")
def list_purchase_orders(request: Request, tenant_id: str, source_id: str,
                         limit: int = Query(100, ge=1, le=200)):
    _require_local(request)
    _require_source(tenant_id, source_id)
    with _store().tx() as db:
        headers = db.execute("""SELECT source_id,revision,body FROM records
             WHERE tenant=? AND source=? AND kind='po' ORDER BY source_id LIMIT ?""",
                             (tenant_id, source_id, limit)).fetchall()
        output = []
        for header in headers:
            lines = db.execute("""SELECT source_id,revision,body FROM records WHERE tenant=?
                AND source=? AND kind='line' AND parent=? ORDER BY source_id""",
                               (tenant_id, source_id, header["source_id"])).fetchall()
            output.append({"record": json.loads(header["body"]), "lines": [
                json.loads(line["body"]) for line in lines]})
    return {"tenant_id": tenant_id, "source_id": source_id, "simulated": True,
            "count": len(output), "purchase_orders": output}


@router.get("/tenants/{tenant_id}/sources/{source_id}/incidents")
def list_po_incidents(request: Request, tenant_id: str, source_id: str):
    _require_local(request)
    _require_source(tenant_id, source_id)
    items = [x for x in status(_store(), tenant_id, technical=True)
             if x["source"] == source_id]
    return {"tenant_id": tenant_id, "source_id": source_id, "count": len(items),
            "incidents": items}


@router.get("/tenants/{tenant_id}/sources/{source_id}/operations")
def list_po_operations(request: Request, tenant_id: str, source_id: str,
                       limit: int = Query(100, ge=1, le=200)):
    _require_local(request)
    _require_source(tenant_id, source_id)
    with _store().tx() as db:
        operations = [dict(x) for x in db.execute("""SELECT op_id,line_id,po_id,eta,
            state,base_rev,po_rev,remote_rev FROM operations WHERE tenant=? AND source=?
            ORDER BY rowid DESC LIMIT ?""", (tenant_id, source_id, limit))]
    return {"tenant_id": tenant_id, "source_id": source_id, "count": len(operations),
            "operations": operations}


@router.post("/demo/seed")
def seed_demo(request: Request, x_demo_token: str | None = Header(default=None)):
    """Seed ONLY fictional POs. Intentional no-overwrite behavior on restart."""
    _demo_access(request, x_demo_token)
    store = _store()
    summaries = []
    for (tenant, source), mapping in _DEMO_SOURCES.items():
        if (tenant, source) in _demo_clients:
            summaries.append(_source_summary(store, tenant, source))
            continue
        with store.tx() as db:
            existing = one(db, """SELECT 1 FROM records WHERE tenant=? AND source=? LIMIT 1""",
                           (tenant, source))
            cursor = one(db, """SELECT 1 FROM cursors WHERE tenant=? AND source=? LIMIT 1""",
                         (tenant, source))
        if existing or cursor:
            raise HTTPException(status_code=409, detail=(
                "Demo database already contains this source but its in-memory mock client "
                "does not exist. Use a fresh sandbox database path to restart the demo."))
        client = ERP(mapping)
        if (tenant, source) == ("customer_a", "mock_standard"):
            client.change("po", {"po_id": "PO-A1", "rev": 1,
                                 "number": "DEMO-001", "supplier": "Synthetic Vendor A"})
            client.change("line", {"line_id": "L-A1", "rev": 1, "po_id": "PO-A1",
                                   "sku": "SKU-A", "qty": 5, "price": "10.00", "eta": "2027-01-10"})
        else:
            client.change("po", {"order": "PO-B1", "version": 1,
                                 "reference": "DEMO-002", "vendor": "Synthetic Vendor B"})
            client.change("line", {"item": "L-B1", "version": 1, "order": "PO-B1",
                                   "product": "SKU-B", "amount": 8, "cost": "15.50",
                                   "delivery": "2027-01-11"})
        _demo_clients[tenant, source] = client
        poll(store, tenant, source, client, mapping)
        reconcile(store, tenant, source, client, mapping)
        summaries.append(_source_summary(store, tenant, source))
    return {"mode": "simulated_local_demo", "sources": summaries}


@router.post("/demo/tenants/{tenant_id}/sources/{source_id}/sync")
def sync_demo_source(request: Request, tenant_id: str, source_id: str,
                     x_demo_token: str | None = Header(default=None)):
    _demo_access(request, x_demo_token)
    client = _client(tenant_id, source_id)
    store = _store()
    poll(store, tenant_id, source_id, client, _DEMO_SOURCES[tenant_id, source_id])
    freshness(store, tenant_id, source_id)
    return _source_summary(store, tenant_id, source_id)


@router.post("/demo/tenants/{tenant_id}/sources/{source_id}/reconcile")
def reconcile_demo_source(request: Request, tenant_id: str, source_id: str,
                          x_demo_token: str | None = Header(default=None)):
    _demo_access(request, x_demo_token)
    client = _client(tenant_id, source_id)
    store = _store()
    checked = reconcile(store, tenant_id, source_id, client,
                        _DEMO_SOURCES[tenant_id, source_id])
    return {"records_examined": checked, **_source_summary(store, tenant_id, source_id)}


class EtaChange(BaseModel):
    line_id: str = Field(min_length=1)
    eta: str = Field(min_length=10)
    approved: bool = False
    operation_id: str | None = None


@router.post("/demo/tenants/{tenant_id}/sources/{source_id}/eta")
def change_demo_eta(request: Request, tenant_id: str, source_id: str,
                    change: EtaChange, x_demo_token: str | None = Header(default=None)):
    """ONLY mock ERP write-back. Demo token + explicit approval are required."""
    _demo_access(request, x_demo_token)
    client = _client(tenant_id, source_id)
    if change.approved is not True:
        raise HTTPException(status_code=403, detail="Explicit demo approval required")
    try:
        state = request_eta(_store(), tenant_id, source_id, client, change.line_id,
                            change.eta, approved=True, op_id=change.operation_id)
    except (ValueError, PermissionError) as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"tenant_id": tenant_id, "source_id": source_id, "state": state,
            "note": "Mock ERP only; developer demo token does not constitute business authorization."}


@router.post("/demo/tenants/{tenant_id}/sources/{source_id}/operations/{operation_id}/verify")
def verify_demo_operation(request: Request, tenant_id: str, source_id: str, operation_id: str,
                          x_demo_token: str | None = Header(default=None)):
    _demo_access(request, x_demo_token)
    client = _client(tenant_id, source_id)
    store = _store()
    with store.tx() as db:
        exists = one(db, "SELECT 1 FROM operations WHERE tenant=? AND source=? AND op_id=?",
                     (tenant_id, source_id, operation_id))
    if not exists:
        raise HTTPException(status_code=404, detail="Unknown operation for this source")
    state = verify_eta(store, operation_id, client)
    return {"operation_id": operation_id, "state": state}
