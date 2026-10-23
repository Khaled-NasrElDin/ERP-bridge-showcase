"""Isolated helper-router tests. Never touches the user's inventory.db."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import console_routes
from db import Base, SyncRun
from writeback import TransferAction


@pytest.fixture
def client(monkeypatch):
    db = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(db)
    monkeypatch.setattr(console_routes, "engine", db)
    monkeypatch.setattr(console_routes, "load_tenants", lambda: {
        "customer_d": {"inventory_url": "https://example.invalid/private?token=secret", "mapping_id": "erp_itemcode_v1", "paginated_inventory_url": "https://example.invalid/pages", "transfer_url": "https://example.invalid/writeback"},
        "customer_a": {"inventory_url": "https://example.invalid/other", "mapping_id": "customer_a"},
    })
    with Session(db) as s:
        s.add(SyncRun(run_id="run-alpha", tenant_id="customer_d", sync_name="inventory_paginated", status="failed", last_completed_page=2))
        s.add(TransferAction(action_id="action-beta", tenant_id="customer_d", sku="ERP001", from_warehouse="New York", to_warehouse="Boston", quantity=50, status="pending_approval"))
        s.commit()
    app = FastAPI()
    app.include_router(console_routes.router)
    return TestClient(app)


def test_console_and_sanitized_tenants(client):
    response = client.get("/console")
    assert response.status_code == 200
    assert "ERP Bridge" in response.text
    assert "Transfer actions" in response.text
    data = client.get("/console/api/tenants").json()["tenants"]
    assert [a["tenant_id"] for a in data] == ["customer_a", "customer_d"]
    assert data[1]["has_pagination"] is True
    assert "secret" not in str(data)
    assert "inventory_url" not in str(data)


def test_runs_and_actions_are_tenant_scoped(client):
    runs = client.get("/console/api/tenants/customer_d/runs").json()["runs"]
    actions = client.get("/console/api/tenants/customer_d/actions").json()["actions"]
    assert runs[0]["last_completed_page"] == 2
    assert actions[0]["quantity"] == 50
    assert client.get("/console/api/tenants/customer_a/runs").json()["runs"] == []
    assert client.get("/console/api/tenants/customer_a/actions").json()["actions"] == []
    assert client.get("/console/api/tenants/missing/runs").status_code == 404
