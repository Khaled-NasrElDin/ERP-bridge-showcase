# ERP Bridge — 2-minute recruiter walkthrough

[← Overview](../README.md)

A simple presentation script for recording the *real local application* or discussing it during an interview. Use simulated data only.

| Time | What to show | What to explain |
| --- | --- | --- |
| 0:00–0:20 | Operations console overview | Multi-tenant ERP inventory, synchronization, and transfer visibility |
| 0:20–0:45 | Switch tenants and inspect inventory | Tenant-scoped storage, different external schemas, canonical fields |
| 0:45–1:10 | Trigger a tracked paginated sync | Page-by-page persistence, checkpoints, source versions, run states |
| 1:10–1:35 | Propose → approve → execute a transfer | Stable idempotency identifier and explicit action states |
| 1:35–1:50 | Display monitoring / alerts | Request status, response times, and incident indicators |
| 1:50–2:00 | Show code examples / architecture | Engineering decisions and what is public versus private |

## Suggested spoken introduction

> I built ERP Bridge to explore a common integration problem: ERP systems may expose inventory through different JSON schemas, while the destination needs one consistent data model. The local prototype normalizes and validates those records, handles paginated and versioned synchronization, and tracks uncertain outbound transfers. This portfolio includes the architecture and testable examples; the full application is private.

## Demonstration hygiene

- Use only the mock ERP and fictional tenant identifiers.
- Identify the original console as a local prototype, not as a hosted production service.
- Show at least one test or controlled failure, not just a screenshot.
- Never imply that mock approval is authenticated authorization.
- Record a separate **short GIF or MP4** only after verifying the application behaves as intended. No video has been created in this package.
