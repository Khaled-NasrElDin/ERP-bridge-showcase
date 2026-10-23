"""ERP Bridge learning prototype: approval-gated, tenant-scoped ERP transfers."""
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, update
from sqlalchemy.orm import Session

from db import Base, engine
from tenant_config import load_tenants

router = APIRouter(prefix="/tenants/{tenant_id}", tags=["ERP Write-back"])


class TransferAction(Base):
    __tablename__ = "transfer_actions"

    action_id = Column(String, primary_key=True)
    tenant_id = Column(String, nullable=False, index=True)
    sku = Column(String, nullable=False)
    from_warehouse = Column(String, nullable=False)
    to_warehouse = Column(String, nullable=False)
    quantity = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default="pending_approval")
    erp_transfer_id = Column(String, nullable=True)
    last_error = Column(String, nullable=True)


def init_writeback_table():
    TransferAction.__table__.create(bind=engine, checkfirst=True)


class TransferDraft(BaseModel):
    sku: str = Field(min_length=1)
    from_warehouse: str = Field(min_length=1)
    to_warehouse: str = Field(min_length=1)
    quantity: int = Field(gt=0)


def tenant_config(tenant_id: str):
    config = load_tenants().get(tenant_id)
    if config is None:
        raise HTTPException(status_code=404, detail="Unknown tenant")
    if not config.get("transfer_url"):
        raise HTTPException(status_code=400, detail="ERP write-back not configured")
    return config


def find_action(session, tenant_id: str, action_id: str):
    action = session.get(TransferAction, action_id)
    if action is None or action.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Transfer action not found")
    return action


def action_response(action):
    return {
        "action_id": action.action_id,
        "tenant_id": action.tenant_id,
        "sku": action.sku,
        "from_warehouse": action.from_warehouse,
        "to_warehouse": action.to_warehouse,
        "quantity": action.quantity,
        "status": action.status,
        "erp_transfer_id": action.erp_transfer_id,
        "last_error": action.last_error,
    }


def flag_uncertain(action_id: str, error: str):
    with Session(engine) as session:
        session.execute(
            update(TransferAction)
            .where(
                TransferAction.action_id == action_id,
                TransferAction.status == "executing",
            )
            .values(status="needs_reconciliation", last_error=error[:500])
        )
        session.commit()


@router.post("/transfer-actions", status_code=201)
def propose_transfer(tenant_id: str, draft: TransferDraft):
    tenant_config(tenant_id)
    if draft.from_warehouse == draft.to_warehouse:
        raise HTTPException(status_code=422, detail="Warehouses must differ")
    action = TransferAction(
        action_id=str(uuid4()), tenant_id=tenant_id, **draft.model_dump(),
        status="pending_approval",
    )
    with Session(engine) as session:
        session.add(action)
        session.commit()
        return action_response(action)


@router.post("/transfer-actions/{action_id}/approve")
def approve_transfer(tenant_id: str, action_id: str):
    with Session(engine) as session:
        action = find_action(session, tenant_id, action_id)
        if action.status != "pending_approval":
            raise HTTPException(status_code=409, detail="Action is not pending approval")
        action.status = "approved"
        session.commit()
        return action_response(action)


@router.get("/transfer-actions/{action_id}")
def get_transfer(tenant_id: str, action_id: str):
    with Session(engine) as session:
        return action_response(find_action(session, tenant_id, action_id))


@router.post("/transfer-actions/{action_id}/execute")
def execute_transfer(tenant_id: str, action_id: str):
    config = tenant_config(tenant_id)
    with Session(engine) as session:
        action = find_action(session, tenant_id, action_id)
        if action.status == "completed":
            return action_response(action)  # Safe repeat: do not POST again
        if action.status != "approved":
            raise HTTPException(status_code=409, detail=f"Cannot execute action in status {action.status}")

        payload = {
            "sku": action.sku,
            "from_warehouse": action.from_warehouse,
            "to_warehouse": action.to_warehouse,
            "quantity": action.quantity,
            "idempotency_key": action.action_id,
        }

        # Compare-and-set prevents two callers both acquiring the action.
        acquired = session.execute(
            update(TransferAction)
            .where(
                TransferAction.action_id == action_id,
                TransferAction.status == "approved",
            )
            .values(status="executing")
        ).rowcount
        if acquired != 1:
            raise HTTPException(status_code=409, detail="Action already being executed")
        session.commit()

    # Remote I/O happens outside the database transaction.
    try:
        response = httpx.post(config["transfer_url"], json=payload, timeout=5.0)
        response.raise_for_status()
        erp_result = response.json()
        transfer_id = erp_result["transfer_id"]
    except httpx.TimeoutException as error:
        flag_uncertain(action_id, f"ERP timeout: {error}")
        raise HTTPException(status_code=504, detail="Unknown ERP outcome; reconcile before retrying") from error
    except httpx.RequestError as error:
        flag_uncertain(action_id, f"ERP connection failure: {error}")
        raise HTTPException(status_code=503, detail="Unknown ERP outcome; reconcile before retrying") from error
    except (httpx.HTTPStatusError, ValueError, KeyError) as error:
        flag_uncertain(action_id, f"ERP response error: {error}")
        raise HTTPException(status_code=502, detail="ERP write-back not confirmed; reconciliation required") from error

    with Session(engine) as session:
        action = find_action(session, tenant_id, action_id)
        action.erp_transfer_id = str(transfer_id)
        action.status = "completed"
        action.last_error = None
        session.commit()
        return action_response(action)


@router.post("/transfer-actions/{action_id}/reconcile")
def reconcile_transfer(tenant_id: str, action_id: str):
    config = tenant_config(tenant_id)
    lookup_base = config.get("transfer_lookup_url")
    if not lookup_base:
        raise HTTPException(status_code=400, detail="ERP lookup URL not configured")

    with Session(engine) as session:
        action = find_action(session, tenant_id, action_id)
        if action.status == "completed":
            return action_response(action)
        if action.status not in ("executing", "needs_reconciliation"):
            raise HTTPException(status_code=409, detail="This action does not require reconciliation")
        expected = {
            "sku": action.sku,
            "from_warehouse": action.from_warehouse,
            "to_warehouse": action.to_warehouse,
            "quantity": action.quantity,
        }

    try:
        # action_id is a UUID generated by our server; URL-path safe.
        response = httpx.get(f"{lookup_base.rstrip('/')}/{action_id}", timeout=5.0)
        if response.status_code == 404:
            return {
                "action_id": action_id,
                "status": "needs_reconciliation",
                "message": "Not found at ERP; absence is not proof of non-execution. Do not blindly retry.",
            }
        response.raise_for_status()
        result = response.json()
        if result["request"] != expected:
            raise HTTPException(status_code=409, detail="ERP transfer payload mismatch")
        transfer_id = str(result["transfer_id"])
    except httpx.RequestError as error:
        raise HTTPException(status_code=503, detail="ERP unavailable during reconciliation") from error
    except httpx.HTTPStatusError as error:
        raise HTTPException(status_code=502, detail="ERP lookup failed") from error
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(status_code=502, detail="Invalid ERP lookup response") from error

    with Session(engine) as session:
        action = find_action(session, tenant_id, action_id)
        action.erp_transfer_id = transfer_id
        action.status = "completed"
        action.last_error = None
        session.commit()
        return action_response(action)
