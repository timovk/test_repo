"""The world clock: today, the agenda, the news, and rolling from election day to election day
(docs/CLOCK.md)."""

from __future__ import annotations

import datetime as dt
from datetime import date

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from app.api.deps import ApiJSON, SessionDep, respond
from app.services.read import clock

router = APIRouter(prefix="/clock", tags=["clock"])


@router.get("", summary="Today, today's elections, the next election day and the running job")
def state(session: SessionDep) -> ApiJSON:
    return respond(clock.state(session))


@router.get("/agenda", summary="Election days around today: finished, today's and planned")
def agenda(
    session: SessionDep,
    start: date | None = Query(None, description="first date (default: 120 days before today)"),
    end: date | None = Query(None, description="last date (default: a year after today)"),
) -> ApiJSON:
    return respond(clock.agenda(session, start=start, end=end))


@router.get("/news", summary="What happened: vacancies, recalls, results and your own people")
def news(
    session: SessionDep,
    since: date | None = Query(None, description="default: 180 days before today"),
    until: date | None = Query(None, description="default: today"),
) -> ApiJSON:
    return respond(clock.news(session, since=since, until=until))


@router.get("/job", summary="The running (or last) count / skip job")
def job() -> ApiJSON:
    return respond({"job": clock.job_status()})


@router.post("/next", summary="Go to the next election day (today's elections must be finished)")
def advance(session: SessionDep) -> ApiJSON:
    return respond(clock.advance(session))


@router.post("/watch", summary="Simulate today's election so its election night can start")
def watch(session: SessionDep) -> ApiJSON:
    return respond(clock.watch(session))


@router.post("/count", status_code=202, summary="Count today's election instantly (background job)")
def count(session: SessionDep) -> ApiJSON:
    return respond(clock.count(session), status_code=202)


class Skip(BaseModel):
    date: dt.date = Field(..., description="the date to skip to; every election day before it is counted")


@router.post("/skip", status_code=202, summary="Skip ahead: count every election day before a date")
def skip(body: Skip, session: SessionDep) -> ApiJSON:
    return respond(clock.skip(session, body.date), status_code=202)
