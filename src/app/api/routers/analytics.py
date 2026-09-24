"""Per-election analytics (reported elections)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ApiJSON, ElectionDep, SessionDep, respond
from app.services.read import analytics as analytics_read

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/{election_id}", summary="Lean, elasticity, competitiveness, efficiency gap, EC efficiency")
def analytics(ref: ElectionDep, session: SessionDep) -> ApiJSON:
    return respond(analytics_read.election_analytics(session, ref))
