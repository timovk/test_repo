"""Elections: list, create, simulate, finalize, and every results page (hidden-until-reported:
``results_source`` is ``final``, ``live`` or ``hidden``)."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from app.api.deps import ApiJSON, ElectionDep, SessionDep, respond
from app.services.read import actions
from app.services.read import elections as el

router = APIRouter(prefix="/elections", tags=["elections"])

_SEED = Field(None, ge=0, lt=2**62, description="seed (default: the scenario's)")


class CreateElection(BaseModel):
    scenario: str = Field(..., description="built-in or user scenario slug, e.g. 'demo-2028'")
    seed: int | None = _SEED
    year: int | None = Field(None, ge=1900, le=2500, description="move the scenario to another election year")
    strict: bool | None = Field(None, description="reject references to places missing from the geography")
    simulate: bool = Field(False, description="simulate the (hidden) result right away")


class SimulateElection(BaseModel):
    seed: int | None = _SEED


@router.get("", summary="All elections with status, contents and headline results")
def list_elections(session: SessionDep) -> ApiJSON:
    return respond(el.elections_list(session))


@router.post("", status_code=201, summary="Create an election from a scenario")
def create_election(body: CreateElection, request: Request, session: SessionDep) -> ApiJSON:
    data = actions.create_election(
        session,
        body.scenario,
        seed=body.seed,
        year=body.year,
        strict=body.strict,
        simulate=body.simulate,
        user_dir=request.app.state.user_scenarios_dir,
    )
    return respond(data, status_code=201)


@router.get("/{election_id}", summary="Election summary")
def election(ref: ElectionDep, session: SessionDep) -> ApiJSON:
    return respond(el.election_detail(session, ref))


@router.post("/{election_id}/simulate", summary="Simulate the hidden result and the election-night timeline")
def simulate(ref: ElectionDep, session: SessionDep, body: SimulateElection | None = None) -> ApiJSON:
    return respond(actions.simulate_election(session, ref, seed=None if body is None else body.seed))


@router.post("/{election_id}/finalize", summary="Instant finish without election night")
def finalize(ref: ElectionDep, session: SessionDep) -> ApiJSON:
    return respond(actions.finalize_election(session, ref))


@router.get("/{election_id}/president", summary="Presidential race: tickets, EV, winner, tipping point")
def president(ref: ElectionDep, session: SessionDep) -> ApiJSON:
    return respond(el.president(session, ref))


@router.get("/{election_id}/electoral-college", summary="Electoral College analysis")
def electoral_college(ref: ElectionDep, session: SessionDep) -> ApiJSON:
    return respond(el.electoral_college(session, ref))


@router.get("/{election_id}/provinces", summary="Per-province results of a province-wide race family")
def provinces(ref: ElectionDep, session: SessionDep, race: str = "PRES") -> ApiJSON:
    return respond(el.provinces_results(session, ref, race))


@router.get("/{election_id}/provinces/{code}", summary="Province results page")
def province(
    ref: ElectionDep,
    code: str,
    session: SessionDep,
    sort: str = "population",
    order: Literal["asc", "desc"] = "desc",
) -> ApiJSON:
    return respond(el.province_page(session, ref, code, sort=sort, order=order))


@router.get("/{election_id}/municipalities", summary="Municipality map / table rows")
def municipalities(
    ref: ElectionDep, session: SessionDep, race: str = "PRES", province: str | None = None
) -> ApiJSON:
    return respond(el.municipality_rows(session, ref, race, province))


@router.get("/{election_id}/municipalities/{code}", summary="Municipality page: every race + precinct table")
def municipality(ref: ElectionDep, code: str, session: SessionDep, race: str | None = None) -> ApiJSON:
    return respond(el.municipality_page(session, ref, code, race=race))


@router.get("/{election_id}/house", summary="House: seat counter by party, 76 FOR CONTROL, district rows")
def house(ref: ElectionDep, session: SessionDep) -> ApiJSON:
    return respond(el.house(session, ref))


@router.get("/{election_id}/house/{district}", summary="House district race detail")
def house_district(ref: ElectionDep, district: str, session: SessionDep) -> ApiJSON:
    return respond(el.house_district(session, ref, district))


@router.get("/{election_id}/senate", summary="Senate: 24 seats, contested / not up, 13 FOR CONTROL")
def senate(ref: ElectionDep, session: SessionDep) -> ApiJSON:
    return respond(el.senate(session, ref))


@router.get("/{election_id}/governors", summary="Governor races")
def governors(ref: ElectionDep, session: SessionDep) -> ApiJSON:
    return respond(el.governors(session, ref))


@router.get("/{election_id}/mayors", summary="Mayor races")
def mayors(ref: ElectionDep, session: SessionDep, province: str | None = None) -> ApiJSON:
    return respond(el.mayors(session, ref, province))


@router.get("/{election_id}/races/{race}", summary="Any race: lines, result, calls, recounts, breakdown")
def race(ref: ElectionDep, race: str, session: SessionDep) -> ApiJSON:
    return respond(el.race_detail(session, ref, race))


@router.get("/{election_id}/calls", summary="Chronological race-call log")
def calls(
    ref: ElectionDep,
    session: SessionDep,
    race_type: str | None = None,
    status: str | None = None,
    include_superseded: bool = True,
    evidence: bool = False,
    limit: int | None = Query(None, ge=0, le=100000),
) -> ApiJSON:
    return respond(
        el.calls(
            session,
            ref,
            race_type=race_type,
            status=status,
            include_superseded=include_superseded,
            evidence=evidence,
            limit=limit,
        )
    )


@router.get("/{election_id}/timeline", summary="Reporting timeline (buckets or events)")
def timeline(
    ref: ElectionDep,
    session: SessionDep,
    detail: Literal["summary", "events"] = "summary",
    bucket_minutes: int = Query(15, ge=1, le=720),
    limit: int = Query(500, ge=1, le=10000),
    offset: int = Query(0, ge=0),
) -> ApiJSON:
    return respond(
        el.timeline(session, ref, bucket_minutes=bucket_minutes, detail=detail, limit=limit, offset=offset)
    )
