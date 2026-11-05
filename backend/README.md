# ERP Bridge 2.0 backend (development branch)

This is a **local training/portfolio sandbox**, not a production ERP gateway. The existing inventory API, transfer-action flow, and web console remain available. V2 separates previously monolithic FastAPI routes and adds a source-sequenced purchase-order demo using the user-supplied Spherecast technical-challenge correctness core.

## What changed

- `main.py` is now a composition root. Inventory and pagination API routes were moved to `api/inventory.py` and `api/pagination.py`. The `/health` route lives in `api/health.py`.
- The legacy route paths and response formats are preserved. `main.py` still exports `CanonicalInventoryItem`, `InventoryItem`, and `sync_page_for_tenant` for project scripts that imported them.
- The optional purchase-order domain is isolated in `po_engine/` with its **own** SQLite database and no change to the inventory SQLAlchemy schema.
- `api/purchase_orders.py` provides read-only inspection and explicitly opt-in **local-only** fake-source operations. Its sources are `mock_standard` and `mock_custom`, not actual NetSuite or real customer ERPs.

## Install and start

Use Python 3.11 or newer. In PowerShell, run from the repository's `backend` folder with your virtual environment activated:

```powershell
python -m pip install -r requirements-v2.txt
python dev_bootstrap.py
python -m uvicorn mock_erp:app --host 127.0.0.1 --port 8001
```

In **a separate backend terminal** with the same virtual environment:

```powershell
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs` or `http://127.0.0.1:8000/console`.

### Optional PO sandbox

Stop the bridge API first. In the terminal that will launch the **bridge API** (not the mock inventory server), explicitly enable the synthetic demo and set a development-only token:

```powershell
$env:ERP_BRIDGE_PO_DEMO_ENABLED = "1"
$env:ERP_BRIDGE_PO_DEMO_TOKEN = "replace-with-a-long-random-local-demo-token"
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

In another PowerShell terminal you can use the interactive `/docs` UI, which lets you enter the `X-Demo-Token` header, or call:

```powershell
$headers = @{ "X-Demo-Token" = "replace-with-a-long-random-local-demo-token" }
Invoke-RestMethod -Method POST -Uri "http://127.0.0.1:8000/po/demo/seed" -Headers $headers
Invoke-RestMethod -Uri "http://127.0.0.1:8000/po/tenants/customer_a/sources/mock_standard/purchase-orders"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/po/tenants/customer_b/sources/mock_custom/status"
```

The seeded simulated ERPs exist in memory in the single server process. The durable local PO projection is stored in `backend/po_integration_v2.sqlite` (not `inventory_v2.db`). After restarting the server, the seeded simulated ERP clients are gone; use a fresh sandbox database path to start a new demo. **Never delete a database containing data you care about.** For disposable demos, set `ERP_BRIDGE_PO_SQLITE_PATH` to a new temporary `.sqlite` file path before starting the server.

The fake ETA write-back route is disabled unless the demo opt-in and token are present **and** the caller explicitly sets `approved=true`. This is not production user identity, tenant authorization, or approval logging. **Never expose this service to the public internet.** No production ERP writes are supported by this patch.

## Tests

Run from `backend`:

```powershell
python -m pytest tests -q
python -m po_engine.demo
```

The regression demo is a simulation, and its synthetic throughput is not a production benchmark. The older inventory page-number checkpoints are not equivalent to source-sequenced checkpoints or complete snapshot guarantees; source contracts must be verified for every real connector.

## Reversibility

Keep `main` and the `erpbridge-v1.0` Git tag unchanged. Apply this patch only to `feature/erpbridge-v2`, commit it after verification, and revert the commit if needed:

```powershell
git revert <v2-upgrade-commit-hash>
```

`git revert` restores tracked code but **does not reverse SQLite writes or external operations**. Back up any v2 databases before testing changes. The original worktree and separate v1 database backup remain unaffected.
