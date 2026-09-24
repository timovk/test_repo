"""Campaigns: FICTIONAL plans and SIMULATED effects."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import ApiJSON, ElectionDep, SessionDep, respond
from app.services.read import campaigns as camp

router = APIRouter(prefix="/campaigns", tags=["campaigns"])


class PlanRequest(BaseModel):
    seed: int | None = Field(None, ge=0, lt=2**62)
    budgets: dict[str, float] | None = Field(None, description="party → budget (abstract units)")
    strategies: dict[str, str] | None = Field(
        None, description="party → balanced | battleground | base | expansion"
    )


@router.get("/{election_id}", summary="Campaigns, allocations and effects by target")
def campaigns(
    ref: ElectionDep, session: SessionDep, party: str | None = None, all_targets: bool = False
) -> ApiJSON:
    return respond(camp.campaigns(session, ref, party=party, all_targets=all_targets))


@router.post("/{election_id}/plan", summary="What-if campaign plan (not stored)")
def plan(ref: ElectionDep, session: SessionDep, body: PlanRequest | None = None) -> ApiJSON:
    b = body or PlanRequest()
    return respond(camp.plan_preview(session, ref, seed=b.seed, budgets=b.budgets, strategies=b.strategies))
