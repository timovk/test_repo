"""FICTIONAL polls and their SIMULATED averages."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import ApiJSON, ElectionDep, SessionDep, respond
from app.services.read import actions
from app.services.read import polls as polls_read

router = APIRouter(prefix="/polls", tags=["polls"])


class NewPoll(BaseModel):
    pollster: str = Field(..., min_length=1, max_length=120, description="invented pollster name")
    poll_type: str = Field(
        ...,
        description="national_president | province_president | house_district | senate | governor | generic_house",
    )
    geo_code: str = Field("NL", description="NL, a province (NB), a district (NB-07) or a Senate seat (NB-1)")
    start_date: date
    end_date: date
    sample_size: int = Field(..., gt=0)
    population: str = Field("LV", pattern="^(LV|RV|A)$")
    method: str = "online"
    undecided_pct: float | None = Field(None, ge=0, le=100)
    margin_of_error: float | None = Field(None, ge=0)
    results: dict[str, float] = Field(..., description="option (party code or label) → percent")
    notes: str | None = None


@router.get("/{election_id}", summary="Polls of an election with pollsters and groups")
def polls(
    ref: ElectionDep,
    session: SessionDep,
    type: str | None = None,
    geo: str | None = None,
    pollster: str | None = None,
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> ApiJSON:
    return respond(
        polls_read.polls(session, ref, poll_type=type, geo=geo, pollster=pollster, limit=limit, offset=offset)
    )


@router.get("/{election_id}/average", summary="Polling average of one poll group")
def average(
    ref: ElectionDep,
    session: SessionDep,
    type: str | None = None,
    geo: str | None = None,
    as_of: date | None = None,
) -> ApiJSON:
    return respond(polls_read.poll_average(session, ref, poll_type=type, geo=geo, as_of=as_of))


@router.post("/{election_id}", status_code=201, summary="Add a manual (FICTIONAL) poll")
def add_poll(ref: ElectionDep, body: NewPoll, session: SessionDep) -> ApiJSON:
    return respond(actions.add_poll(session, ref, body.model_dump()), status_code=201)
