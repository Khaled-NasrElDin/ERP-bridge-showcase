# Security, accuracy, and limitations

[← Back to overview](../README.md)

## This is a local learning prototype

The original ERP Bridge implementation was built to learn and demonstrate enterprise integration patterns. **It must not be deployed on a public network without additional security work.**

The supplied backend's main `/tenants/...` operations endpoints lack authenticated user identity and tenant-level authorization. Tenant-specific query filters and locally restricted console routes do not guarantee access control. Do not expose them publicly as-is.

## What the showcase does and does not claim

| Supported in the original prototype | Not demonstrated / not guaranteed |
| --- | --- |
| Explicit mappings + per-tenant overrides | Automatic AI-based semantic inference |
| Pydantic validation and schema inspection | Universal semantic equivalence between ERPs |
| Page checkpoints and tracked sync runs | Exactly-once distributed execution |
| Source-revision-aware SQLite upsert | Correctness for all timestamp, version, or concurrency schemes |
| Action-ID-based write-back + mock ERP lookup | Durable ERP idempotency after mock-server restart |
| Tenant-scoped storage and monitoring queries | Tenant authorization enforced by identity / roles |
| Selected activity and alert endpoints | Complete inventory reconciliation and freshness guarantees |

## Public demo scope

`docs/index.html` is a **standalone browser simulation** with hardcoded fictional tenant inventory. It does not call your original API, trigger any real ERP operation, change a database, or exercise the private backend. The screenshot in `docs/assets/` was taken from the real *local* console running with mock ERP data.

## Private source and data handling

The complete codebase, original HTML console, and original internal configuration remain in a private repository. This showcase deliberately excludes the original SQLite database, private modules, credentials, client connections, and entire application logic.

A private repository protects unpublished implementation from public browsing, but does not make information in public documentation or screenshots secret. Git history is part of what gets published: avoid copying the private `.git` directory, and audit files before the first commit.

## Priority before any real deployment

1. Enforce authentication, RBAC, and authorization for each tenant and transfer operation.
2. Apply database migrations and reliable application lifecycle initialization.
3. Move endpoints into smaller routers and separate pure service logic from HTTP handlers.
4. Expand fault-injection, restart, concurrency, and reconciliation tests.
5. Harden SSRF and tenant URL configuration, network boundaries, credentials, and secret management.
6. Use durable storage and transaction-safe idempotency processing for outbound ERP changes.
7. Define operational service levels, retention, metrics, and incident response.

**Privacy and security posture stated here is for portfolio transparency, not a certification.**
