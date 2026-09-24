"""FICTIONAL electoral geography: House district plan, districts and Senate seats."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ApiJSON, SessionDep, respond
from app.services.read import districts as districts_read

router = APIRouter(tags=["geography"])


@router.get("/districts", summary="House districts of the active plan")
def districts(session: SessionDep, province: str | None = None, plan: int | None = None) -> ApiJSON:
    return respond(districts_read.districts(session, province, plan))


@router.get("/districts/plan", summary="District plan metadata, validation summary, deviation statistics")
def district_plan(session: SessionDep, plan: int | None = None) -> ApiJSON:
    return respond(districts_read.plan_summary(session, plan))


@router.get("/districts/{code}", summary="District detail: statistics, municipalities, neighbours, history")
def district(code: str, session: SessionDep, plan: int | None = None) -> ApiJSON:
    return respond(districts_read.district_detail(session, code, plan))


@router.get("/senate/seats", summary="The 24 Senate seats: class, current holder, next election")
def senate_seats(session: SessionDep) -> ApiJSON:
    return respond(districts_read.senate_seats(session))
