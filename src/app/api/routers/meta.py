"""Health and application meta (constitution, active system, elections, legend)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import ApiJSON, SessionDep, respond
from app.services.read import meta as meta_read

router = APIRouter(tags=["system"])


@router.get("/health", summary="Liveness plus database / geography readiness")
def health(request: Request) -> ApiJSON:
    """Never fails: reports whether the database schema, the loaded geography, the night service
    and the UI build are available (``ready`` = database and geography loaded)."""
    return respond(meta_read.health(request.app.state.db.url, request.app.state.ui_index))


@router.get("/meta", summary="Constitution, active system, elections, legend")
def get_meta(session: SessionDep) -> ApiJSON:
    return respond(meta_read.meta(session))
