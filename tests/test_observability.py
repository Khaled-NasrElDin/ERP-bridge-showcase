"""Safe isolated tests: do not modify your live inventory.db."""

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import observability
from db import Base, SyncRun
from writeback import TransferAction


def test_request_metrics_and_tenant_isolation(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'isolated_ops.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(observability, "engine", engine)
    monkeypatch.setattr(observability, "load_tenants", lambda: {
        "customer_a": {}, "customer_d": {},
    })

    app = FastAPI()
    observability.install_monitoring(app)
    app.include_router(observability.router)

    @app.post("/tenants/{tenant_id}/sync-page")
    def fake_sync_page(tenant_id: str, page: int = 1):
        if page == 3:
            raise HTTPException(status_code=502, detail="upstream failed")
        return {"tenant_id": tenant_id, "page": page}

    with TestClient(app) as client:
        assert client.post("/tenants/customer_d/sync-page?page=1").status_code == 200
        assert client.post("/tenants/customer_d/sync-page?page=3").status_code == 502
        assert client.post("/tenants/customer_a/sync-page?page=1").status_code == 200

        summary_d = client.get("/ops/tenants/customer_d/summary").json()
        assert summary_d["recent_5xx_requests"] == 1
        assert summary_d["attention"] == "needed"
        events_d = client.get("/ops/tenants/customer_d/events").json()["events"]
        assert len(events_d) == 2
        assert {event["status_code"] for event in events_d} == {200, 502}

        summary_a = client.get("/ops/tenants/customer_a/summary").json()
        assert summary_a["recent_5xx_requests"] == 0
        assert len(client.get("/ops/tenants/customer_a/events").json()["events"]) == 1
        assert client.get("/ops/tenants/not_a_tenant/summary").status_code == 404


def test_alerts_for_failed_run_and_uncertain_writeback(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'isolated_alerts.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(observability, "engine", engine)
    monkeypatch.setattr(observability, "load_tenants", lambda: {"customer_d": {}})

    with Session(engine) as session:
        session.add(SyncRun(run_id="test_run", tenant_id="customer_d",
                            sync_name="inventory_paginated", status="failed",
                            last_completed_page=2))
        session.add(TransferAction(action_id="test_action", tenant_id="customer_d",
                                   sku="ERP001", from_warehouse="New York",
                                   to_warehouse="Boston", quantity=50,
                                   status="needs_reconciliation", last_error="timeout"))
        session.commit()

    app = FastAPI()
    observability.install_monitoring(app)
    app.include_router(observability.router)
    with TestClient(app) as client:
        result = client.get("/ops/tenants/customer_d/alerts").json()
        types = {alert["type"] for alert in result["alerts"]}
        assert result["count"] == 2
        assert types == {"failed_sync_run", "writeback_requires_reconciliation"}
        summary = client.get("/ops/tenants/customer_d/summary").json()
        assert summary["sync_runs_by_status"]["failed"] == 1
        assert summary["transfer_actions_by_status"]["needs_reconciliation"] == 1
