"""History across reported elections: lineage-aware comparisons, records and series."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query

from app.api.deps import ApiJSON, SessionDep, respond
from app.services.read import history as hist

router = APIRouter(prefix="/history", tags=["history"])


@router.get("/summary", summary="Per reported election: President, chambers, turnout")
def summary(session: SessionDep) -> ApiJSON:
    return respond(hist.summary(session))


@router.get("/compare", summary="Swing table and flips between two elections")
def compare(
    session: SessionDep,
    a: str | None = None,
    b: str | None = None,
    race: str = "PRES",
    level: Literal["municipality", "district", "province", "national"] = "municipality",
) -> ApiJSON:
    return respond(hist.compare(session, a, b, race, level))


@router.get("/closest", summary="Closest races of all reported elections")
def closest(session: SessionDep, n: int = Query(10, ge=1, le=500), race_type: str | None = None) -> ApiJSON:
    return respond(hist.records(session, "closest", n, race_type))


@router.get("/landslides", summary="Largest landslides of all reported elections")
def landslides(
    session: SessionDep, n: int = Query(10, ge=1, le=500), race_type: str | None = None
) -> ApiJSON:
    return respond(hist.records(session, "landslides", n, race_type))


@router.get("/divergence", summary="Electoral College vs popular vote per election")
def divergence(session: SessionDep) -> ApiJSON:
    return respond(hist.divergence(session))


@router.get("/municipality/{code}", summary="A municipality across elections")
def municipality(code: str, session: SessionDep, race: str = "PRES") -> ApiJSON:
    return respond(hist.geo_series(session, "municipality", code, race))


@router.get("/province/{code}", summary="A province across elections")
def province(code: str, session: SessionDep, race: str = "PRES") -> ApiJSON:
    return respond(hist.geo_series(session, "province", code, race))


@router.get("/district/{code}", summary="A House district code across elections")
def district(code: str, session: SessionDep) -> ApiJSON:
    return respond(hist.district_series(session, code))
