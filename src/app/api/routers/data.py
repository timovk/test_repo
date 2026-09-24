"""Data provenance and system validation."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ApiJSON, SessionDep, respond
from app.services.read import meta as meta_read

router = APIRouter(prefix="/data", tags=["system"])


@router.get(
    "/provenance", summary="REAL sources, DERIVED transforms, FICTIONAL constructs, SIMULATED outputs"
)
def provenance(session: SessionDep) -> ApiJSON:
    return respond(meta_read.provenance(session))


@router.get("/validation", summary="Constitutional and integrity validation of the stored system")
def validation(session: SessionDep, elections: bool = False) -> ApiJSON:
    """``elections=true`` also reconciles every stored election (slow on the real country)."""
    return respond(meta_read.validation_report(session, elections=elections))
