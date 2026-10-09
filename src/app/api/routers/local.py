"""In-between local elections: the calendar of local election days and the local elections."""

from __future__ import annotations

import datetime as dt
from datetime import date

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import ApiJSON, SessionDep, respond
from app.services.read import actions
from app.services.read import local as loc

router = APIRouter(prefix="/local", tags=["local"])


@router.get("/calendar", summary="Planned local election days and what is on each ballot")
def calendar(
    session: SessionDep,
    start: date | None = Query(None, description="first date (default: the latest reported election)"),
    end: date | None = Query(None, description="last date (default: start + 12 months)"),
    province: str | None = Query(None, description="province code, e.g. GE"),
) -> ApiJSON:
    return respond(loc.calendar(session, start=start, end=end, province=province))


@router.get("/elections", summary="Stored local elections with race counts")
def elections(session: SessionDep, year: int | None = None, province: str | None = None) -> ApiJSON:
    return respond(loc.elections(session, year=year, province=province))


class CreateLocal(BaseModel):
    province_code: str = Field(..., description="province code, e.g. GE")
    date: dt.date = Field(..., description="one of the province's local election days")
    simulate: bool = Field(False, description="simulate the (hidden) result right away")


@router.post("/elections", status_code=201, summary="Create the local election of a province on a local day")
def create(body: CreateLocal, session: SessionDep) -> ApiJSON:
    return respond(
        actions.create_local_election(session, body.province_code, body.date, simulate=body.simulate),
        status_code=201,
    )
