"""FICTIONAL parties and candidates."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.api.deps import ApiJSON, SessionDep, respond
from app.services.read import people

router = APIRouter(tags=["parties & candidates"])


@router.get("/parties", summary="Parties: identity, lineage, electoral record")
def parties(session: SessionDep) -> ApiJSON:
    return respond(people.parties(session))


@router.get("/candidates", summary="Search candidates")
def candidates(
    session: SessionDep,
    search: str | None = None,
    party: str | None = None,
    office: str | None = Query(
        None, description="serving office prefix: PRES, VP, HOUSE, SEN, GOV, LTGOV, MAYOR"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ApiJSON:
    return respond(
        people.candidates(session, search=search, party=party, office=office, limit=limit, offset=offset)
    )


@router.get("/candidates/{candidate_id}", summary="Candidate profile and career")
def candidate(candidate_id: int, session: SessionDep) -> ApiJSON:
    return respond(people.candidate_profile(session, candidate_id))
