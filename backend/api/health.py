"""Minimal liveness endpoint; existing URL and response stay unchanged."""
from fastapi import APIRouter

router = APIRouter(tags=["Health"])


@router.get("/health")
def health_check():
    return {"status": "ok", "service": "ERP Bridge"}
