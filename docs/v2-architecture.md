# ERP Bridge v2: modular API + source-sequenced PO sandbox

The v2 code in `feature/erpbridge-v2` retains the v1 inventory and warehouse-transfer API contracts. The new PO integration is deliberately **separate** from inventory persistence:

```text
main.py (FastAPI app composition)
  +-- api/health.py
  +-- api/inventory.py ----> db.py (SQLAlchemy -> inventory_v2.db)
  +-- api/pagination.py --> db.py (legacy page-number checkpoints)
  +-- console_routes.py / observability.py / writeback.py (existing modules)
  +-- api/purchase_orders.py [localhost-only, opt-in fake-source mutations]
         +-- po_engine/integration.py (sequenced change feed + PO-scoped errors)
         +-- po_engine/monitor.py (snapshot/watermark reconciliation + incidents)
         +-- po_engine/writeback.py (source CAS + idempotency/receipts)
         +-- po_engine/store.py (sqlite3 -> po_integration_v2.sqlite)
         +-- po_engine/demo.py (synthetic source adapters and regression scenarios)
```

## Deliberate boundaries

1. **Inventory compatibility**: old URLs are retained; the code is moved, not rewritten. Existing inventory syncing still assumes page stability for its page-number checkpoints.
2. **Separate domain**: POs are `(tenant, source, kind, source_id)`-scoped. Lines reference an ERP-side PO identity. Do not assume identical identity across sources means identical entities.
3. **No real ERP connector**: `mock_standard` and `mock_custom` are in-memory simulators, not vendor adapters. The demo's source contract supports monotonic change sequences, complete snapshots with watermark, atomic PO-wide revision checks, durable idempotency and receipt lookup. An actual source must **prove** these capabilities before any analogous write path is enabled.
4. **Honest write-back state**: an accepted remote write can remain `confirmed` until later polling observes the same source revision; lost replies remain `unknown` and must be verified, not blindly resent.
5. **No production security**: local client checks and a developer token are only protections for a demo. Existing transfer-action endpoints are not production authenticated. A real deployment needs user authentication, tenant-scoped authorization, role-based approvals, secret management, audit integrity, encryption and threat modeling.
6. **PO data persistence is separate**: no shared SQLAlchemy migrations or changes to v1 database are introduced. The demo sqlite path can be overridden using `ERP_BRIDGE_PO_SQLITE_PATH`.
7. **Performance**: simulated tests don't establish a two-minute SLA or throughput under actual network/ERP rate limiting.

## Next milestones (not implemented in this patch)

- Define typed interfaces and conformance tests for real vendor adapters.
- Introduce a production identity provider, tenant-scoped RBAC, and a truly authorized approval workflow.
- Replace in-memory demo-source registry with durable connector lifecycle and secrets management.
- Add proper migrations for persistent schema evolution and a task scheduler/background worker.
- Improve inventory source consistency guarantees, and integrate PO incident summaries into the operations console (the PO data currently has dedicated `/po/...` API views).
- Test against production-like source quirks, rate limits, partial failures and durable write receipt recovery.
