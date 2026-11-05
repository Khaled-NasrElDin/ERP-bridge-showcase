"""New PO sandbox tests; isolated SQLite and synthetic source only."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api import purchase_orders as po


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_BRIDGE_PO_SQLITE_PATH", str(tmp_path / "po_isolated.sqlite"))
    monkeypatch.setenv("ERP_BRIDGE_PO_DEMO_ENABLED", "1")
    monkeypatch.setenv("ERP_BRIDGE_PO_DEMO_TOKEN", "test-secret-only")
    monkeypatch.setattr(po, "_demo_clients", {})
    app = FastAPI()
    app.include_router(po.router)
    with TestClient(app) as test_client:
        yield test_client


TOKEN = {"X-Demo-Token": "test-secret-only"}
ROOT = "/po/tenants/customer_a/sources/mock_standard"
DEMO = "/po/demo/tenants/customer_a/sources/mock_standard"


def test_po_demo_opt_in_and_local_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("ERP_BRIDGE_PO_SQLITE_PATH", str(tmp_path / "unused.sqlite"))
    monkeypatch.delenv("ERP_BRIDGE_PO_DEMO_ENABLED", raising=False)
    monkeypatch.setenv("ERP_BRIDGE_PO_DEMO_TOKEN", "test-secret-only")
    app = FastAPI()
    app.include_router(po.router)
    with TestClient(app) as client:
        assert client.post("/po/demo/seed", headers=TOKEN).status_code == 403
        assert client.get(ROOT + "/status").status_code == 200
        assert client.get("/po/tenants/unknown/sources/mock_standard/status").status_code == 404


def test_seed_read_isolation_and_write_safety(client):
    assert client.post("/po/demo/seed").status_code == 403
    assert client.post("/po/demo/seed", headers={"X-Demo-Token": "bad"}).status_code == 403
    result = client.post("/po/demo/seed", headers=TOKEN)
    assert result.status_code == 200, result.text
    assert len(result.json()["sources"]) == 2

    first = client.get(ROOT + "/purchase-orders")
    second = client.get("/po/tenants/customer_b/sources/mock_custom/purchase-orders")
    assert first.status_code == second.status_code == 200
    assert first.json()["purchase_orders"][0]["record"]["id"] == "PO-A1"
    assert first.json()["purchase_orders"][0]["lines"][0]["id"] == "L-A1"
    assert second.json()["purchase_orders"][0]["record"]["id"] == "PO-B1"
    assert client.get(ROOT + "/incidents").json()["count"] == 0

    # Both sandbox token and explicit approval are required; no real ERP is contacted.
    payload = {"line_id": "L-A1", "eta": "2027-01-20", "operation_id": "demo-operation-1"}
    assert client.post(DEMO + "/eta", json=payload).status_code == 403
    assert client.post(DEMO + "/eta", json={**payload, "approved": True}).status_code == 403
    response = client.post(DEMO + "/eta", json={**payload, "approved": True}, headers=TOKEN)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "confirmed"
    # Retrying the same operation ID never resends to the source.
    replay = client.post(DEMO + "/eta", json={**payload, "approved": True}, headers=TOKEN)
    assert replay.json()["state"] == "confirmed"
    assert po._demo_clients["customer_a", "mock_standard"].calls == 1

    synced = client.post(DEMO + "/sync", headers=TOKEN)
    assert synced.status_code == 200
    actions = client.get(ROOT + "/operations").json()["operations"]
    assert actions[0]["state"] == "settled"
    data = client.get(ROOT + "/purchase-orders").json()
    assert data["purchase_orders"][0]["lines"][0]["eta"] == "2027-01-20"
    assert client.get("/po/tenants/customer_b/sources/mock_custom/operations").json()["count"] == 0


def test_po_scoped_error_does_not_block_another_order(client):
    assert client.post("/po/demo/seed", headers=TOKEN).status_code == 200
    erp = po._demo_clients["customer_a", "mock_standard"]
    erp.change("line", {"line_id": "L-A1", "rev": 2, "po_id": "PO-A1",
                        "sku": "SKU-A", "qty": -5, "price": "10.00", "eta": "2027-01-10"})
    erp.change("po", {"po_id": "PO-A2", "rev": 1,
                      "number": "DEMO-003", "supplier": "Synthetic Vendor C"})
    erp.change("line", {"line_id": "L-A2", "rev": 1, "po_id": "PO-A2",
                        "sku": "SKU-C", "qty": 9, "price": "3.50", "eta": "2027-01-14"})
    assert client.post(DEMO + "/sync", headers=TOKEN).status_code == 200
    result = client.get(ROOT + "/purchase-orders").json()["purchase_orders"]
    assert len(result) == 2
    assert result[0]["lines"][0]["qty"] == "5"
    assert result[1]["lines"][0]["qty"] == "9"
    issues = client.get(ROOT + "/incidents").json()["incidents"]
    assert any(issue["scope"] == "po:PO-A1" for issue in issues)
    assert not any(issue["scope"] == "po:PO-A2" for issue in issues)
