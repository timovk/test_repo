"""Stable-schema CSV / JSON exports."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter
from fastapi.responses import Response

from app.api.deps import ApiJSON, ElectionDep, SessionDep, respond
from app.core.errors import NotFoundError
from app.services.read import exports

router = APIRouter(prefix="/export", tags=["export"])


@router.get("/schemas", summary="Export datasets and their schemas")
def schemas() -> ApiJSON:
    return respond(exports.datasets())


@router.get(
    "/{election_id}/{filename}",
    summary="Export a dataset: <dataset>.csv or <dataset>.json",
    response_model=None,
)
def export(
    ref: ElectionDep,
    filename: str,
    session: SessionDep,
    race: str | None = None,
    race_type: str | None = None,
    level: str | None = None,
    run_id: int | None = None,
    as_of: date | None = None,
) -> Response:
    """Datasets: ``national``, ``provinces``, ``municipalities``, ``units``, ``district_lines``,
    ``house``, ``senate``, ``governors``, ``electoral_votes``, ``swing``, ``timeline``, ``calls``,
    ``montecarlo_summary``, ``montecarlo_distribution``, ``polling_averages``, ``polls``,
    ``districts``, ``apportionment`` (canonical schema names work too)."""
    if "." not in filename:
        raise NotFoundError("expected <dataset>.csv or <dataset>.json")
    dataset, fmt = filename.rsplit(".", 1)
    content, media, name = exports.export(
        session, ref, dataset, fmt, race=race, race_type=race_type, level=level, run_id=run_id, as_of=as_of
    )
    headers = {"Content-Disposition": f'attachment; filename="{name}"'}
    if isinstance(content, str):
        return Response(content, media_type=media, headers=headers)
    return respond(content, headers=headers)
