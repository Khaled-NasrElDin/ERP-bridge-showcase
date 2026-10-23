"""Local ERP Bridge observability milestone.

Audits POST integration/write-back requests, exposes tenant-scoped diagnostic views,
and derives alerts from durable synchronization/write-back state.

For training only: /ops routes have no authentication; do not deploy publicly.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from sqlalchemy import Column, Integer, String, func, select
from sqlalchemy.orm import Session

from db import Base, SyncRun, TenantInventoryRecord, engine
from tenant_config import load_tenants
from writeback import TransferAction

logger = logging.getLogger("erp_bridge.observability")
router = APIRouter(prefix="/ops", tags=["Integration monitoring"])


class IntegrationEvent(Base):
    __tablename__ = "integration_events"

    event_id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    event_type = Column(String, nullable=False)
    method = Column(String, nullable=False)
    path = Column(String, nullable=False)
    status_code = Column(Integer, nullable=False)
    duration_ms = Column(Integer, nullable=False)
    occurred_at_utc = Column(String, nullable=False, index=True)


def utc_iso(value=None):
    """UTC ISO timestamps with fixed format, safe for lexical comparisons."""
    if value is None:
        value = datetime.now(timezone.utc)
    return value.isoformat(timespec="seconds")


def _tracked_request(request: Request):
    if request.method != "POST":
        return None
    sections = request.url.path.strip("/").split("/")
    if len(sections) < 3 or sections[0] != "tenants" or not sections[1]:
        return None
    if sections[2] in ("sync", "sync-page", "sync-all-pages", "resume-sync"):
        return sections[1], "synchronization"
    if (sections[2] == "sync-runs" and len(sections) >= 6
            and sections[4] == "pages"):
        return sections[1], "synchronization"
    if sections[2] == "sync-runs" and len(sections) == 3:
        return sections[1], "run_registration"
    if sections[2] == "transfer-actions":
        return sections[1], "writeback"
    return None


def install_monitoring(app: FastAPI):
    """Call once at application setup, before serving requests."""
    IntegrationEvent.__table__.create(bind=engine, checkfirst=True)

    @app.middleware("http")
    async def audit_integration_requests(request: Request, call_next):
        tracked = _tracked_request(request)
        if tracked is None:
            return await call_next(request)

        tenant_id, event_type = tracked
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            # Do not record request bodies, ERP credentials or response data.
            try:
                with Session(engine) as session:
                    session.add(IntegrationEvent(
                        event_id=str(uuid4()),
                        tenant_id=tenant_id,
                        event_type=event_type,
                        method=request.method,
                        path=request.url.path,
                        status_code=status_code,
                        duration_ms=duration_ms,
                        occurred_at_utc=utc_iso(),
                    ))
                    session.commit()
            except Exception:
                # A metrics failure must not change a successful ERP write's outcome.
                logger.exception("Could not persist integration audit event")


def _require_tenant(tenant_id: str):
    if tenant_id not in load_tenants():
        raise HTTPException(status_code=404, detail="Unknown tenant")


def _status_counts(session, model, tenant_id):
    result = session.execute(
        select(model.status, func.count())
        .where(model.tenant_id == tenant_id)
        .group_by(model.status)
    ).all()
    return {status: count for status, count in result}


def _event_response(event):
    return {
        "event_id": event.event_id,
        "tenant_id": event.tenant_id,
        "event_type": event.event_type,
        "method": event.method,
        "path": event.path,
        "status_code": event.status_code,
        "duration_ms": event.duration_ms,
        "occurred_at_utc": event.occurred_at_utc,
    }


@router.get("/tenants/{tenant_id}/summary")
def tenant_summary(tenant_id: str, window_minutes: int = Query(60, ge=1, le=10080)):
    _require_tenant(tenant_id)
    since = utc_iso(datetime.now(timezone.utc) - timedelta(minutes=window_minutes))

    with Session(engine) as session:
        runs = _status_counts(session, SyncRun, tenant_id)
        actions = _status_counts(session, TransferAction, tenant_id)
        inventory_count = session.scalar(
            select(func.count()).select_from(TenantInventoryRecord)
            .where(TenantInventoryRecord.tenant_id == tenant_id)
        )
        upstream_failures = session.scalar(
            select(func.count()).select_from(IntegrationEvent).where(
                IntegrationEvent.tenant_id == tenant_id,
                IntegrationEvent.status_code >= 500,
                IntegrationEvent.occurred_at_utc >= since,
            )
        )
        last_sync_request = session.scalar(
            select(func.max(IntegrationEvent.occurred_at_utc)).where(
                IntegrationEvent.tenant_id == tenant_id,
                IntegrationEvent.event_type == "synchronization",
                IntegrationEvent.status_code < 400,
            )
        )

    alert_count = (runs.get("failed", 0)
                   + actions.get("needs_reconciliation", 0)
                   + upstream_failures)
    return {
        "tenant_id": tenant_id,
        "inventory_records": inventory_count,
        "sync_runs_by_status": runs,
        "transfer_actions_by_status": actions,
        "recent_5xx_requests": upstream_failures,
        "error_window_minutes": window_minutes,
        "last_successful_sync_request_at_utc": last_sync_request,
        "alert_count": alert_count,
        "attention": "needed" if alert_count else "none_detected",
        "note": (
            "Last successful request is not proof of a complete/fresh inventory snapshot. "
            "A historical failed run stays visible until its status is resolved."
        ),
    }


@router.get("/tenants/{tenant_id}/events")
def tenant_events(tenant_id: str, limit: int = Query(20, ge=1, le=100)):
    _require_tenant(tenant_id)
    with Session(engine) as session:
        events = session.scalars(
            select(IntegrationEvent)
            .where(IntegrationEvent.tenant_id == tenant_id)
            .order_by(IntegrationEvent.occurred_at_utc.desc(), IntegrationEvent.event_id.desc())
            .limit(limit)
        ).all()
        return {"tenant_id": tenant_id, "events": [_event_response(e) for e in events]}


@router.get("/tenants/{tenant_id}/alerts")
def tenant_alerts(tenant_id: str, window_minutes: int = Query(60, ge=1, le=10080)):
    _require_tenant(tenant_id)
    since = utc_iso(datetime.now(timezone.utc) - timedelta(minutes=window_minutes))
    with Session(engine) as session:
        failed_runs = session.scalars(
            select(SyncRun).where(SyncRun.tenant_id == tenant_id, SyncRun.status == "failed")
        ).all()
        uncertain_actions = session.scalars(
            select(TransferAction).where(
                TransferAction.tenant_id == tenant_id,
                TransferAction.status == "needs_reconciliation",
            )
        ).all()
        recent_server_errors = session.scalars(
            select(IntegrationEvent).where(
                IntegrationEvent.tenant_id == tenant_id,
                IntegrationEvent.status_code >= 500,
                IntegrationEvent.occurred_at_utc >= since,
            ).order_by(IntegrationEvent.occurred_at_utc.desc()).limit(30)
        ).all()

        alerts = [
            {"type": "failed_sync_run", "run_id": r.run_id,
             "last_completed_page": r.last_completed_page}
            for r in failed_runs
        ]
        alerts += [
            {"type": "writeback_requires_reconciliation", "action_id": a.action_id,
             "last_error": a.last_error}
            for a in uncertain_actions
        ]
        alerts += [
            {"type": "recent_http_error", "path": e.path,
             "http_status": e.status_code, "occurred_at_utc": e.occurred_at_utc}
            for e in recent_server_errors
        ]
    return {"tenant_id": tenant_id, "count": len(alerts), "alerts": alerts}
