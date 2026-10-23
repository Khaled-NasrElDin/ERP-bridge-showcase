# ERP Bridge: Runnable FastAPI backend

This directory contains a substantial, **real runnable implementation** of the ERP Bridge learning prototype, not the JavaScript-only showcase. It uses fictional sample data and locally hosted mock ERP APIs.

It includes the original Python architecture for tenant-specific JSON mappings, canonical normalization, REST integration, SQLite persistence, version-aware paginated synchronization, checkpoints, approval-gated mock ERP write-back, reconciliation and observability.

## Run locally (Python 3.10 or newer)

Open a terminal in the `backend` directory and create a virtual environment:

```bash
python -m venv .venv
```

Activate it:

- Windows PowerShell: `.venv\Scripts\Activate.ps1`
- macOS/Linux: `source .venv/bin/activate`

Install dependencies and create a fresh local SQLite database:

```bash
python -m pip install -r requirements.txt
python dev_bootstrap.py
```

**Terminal 1**, still inside `backend`:

```bash
python -m uvicorn mock_erp:app --host 127.0.0.1 --port 8001
```

**Terminal 2**, also inside `backend`:

```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Now open:

- Operations console: http://127.0.0.1:8000/console
- FastAPI interactive API documentation: http://127.0.0.1:8000/docs
- Health: http://127.0.0.1:8000/health

### Try real synchronization against the mock ERP

With both servers running, open another terminal:

```bash
curl -X POST http://127.0.0.1:8000/tenants/customer_d/sync-all-pages
curl http://127.0.0.1:8000/tenants/customer_d/inventory
```

The first API call makes real localhost HTTP requests to the mock ERP, normalizes and validates its records, and persists them into the local SQLite database. The second returns the stored tenant-specific inventory. This is **real backend execution with synthetic data**, not real customer integration.

For other endpoints, use Swagger UI. The mock transfer endpoints demonstrate approval, execution, idempotency identifiers and reconciliation states.

## Run backend tests

From within `backend`:

```bash
python -m pytest tests -q
```

The test modules include isolated observability and operations-console tests, plus mapping contract checks.

## Security and scope

**Development use only. Do not expose this server to the public internet.**

- There is no production authentication or tenant authorization. Tenant-specific database filters are not a substitute for access control.
- The console restricts requests to loopback clients, which does not provide complete application security.
- The backend should be run with `--host 127.0.0.1`, not `--host 0.0.0.0`.
- The upstream addresses are set to localhost mock APIs in `tenants.json`. Do not point them at real systems without redesigning authentication, config protection, network controls and request policies.
- SQLite is created locally as `inventory.db`, which must never be committed. The repository's top-level `.gitignore` excludes local database files and secrets.
- The mock ERP stores some transfer state in memory, so a mock restart loses that state.
- Some learning-stage routes and architectural shortcuts remain. Database migrations, security hardening, automated end-to-end coverage, and production deployment controls are out of scope.
- Dependencies use ranges rather than a full lockfile.
- The `docs/index.html` portfolio website elsewhere in this repository remains a **static simulation**. It does not call this backend.

## What changed for this public edition

This is based on the original private development source, with synthetic `tenants.json` and `mappings.json`. No local database or private development history is included. Two small cleanup changes were made in `main.py`: the health endpoint names ERP Bridge consistently, and the legacy `/dashboard` endpoint redirects to the working `/console` rather than referencing an unavailable HTML file. The current public edition omits older experimental diagnostic scripts; the core application modules and key isolated tests are included.

The project is a **learning prototype**, not a production-secured ERP platform or a publicly hosted integration API.
