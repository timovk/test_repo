"""Scenario editor (FICTIONAL political assumptions)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import ApiJSON, SessionDep, respond
from app.services.read import scenarios as sc

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


class ScenarioInput(BaseModel):
    yaml: str | None = Field(None, description="scenario YAML text")
    document: dict[str, Any] | None = Field(None, description="scenario document as JSON")
    slug: str | None = Field(None, description="override the document's slug")


class ScenarioUpdate(BaseModel):
    yaml: str | None = None
    document: dict[str, Any] | None = None
    edits: list[dict[str, Any]] | None = Field(None, description="editor operations (see GET /api/scenarios)")


class Duplicate(BaseModel):
    new_slug: str
    seed: int | None = Field(None, ge=0, lt=2**62)
    name: str | None = None


class ValidateInput(BaseModel):
    yaml: str | None = None
    document: dict[str, Any] | None = None
    base: str | None = Field(None, description="slug of the scenario the edits apply to")
    edits: list[dict[str, Any]] | None = None


def _dir(request: Request) -> Any:
    return request.app.state.user_scenarios_dir


@router.get("", summary="Built-in and user scenarios")
def list_scenarios(request: Request, session: SessionDep) -> ApiJSON:
    return respond(sc.list_all(session, _dir(request)))


@router.post("", status_code=201, summary="Create / import a user scenario")
def create(body: ScenarioInput, request: Request, session: SessionDep) -> ApiJSON:
    return respond(
        sc.create(
            session, yaml_text=body.yaml, document=body.document, slug=body.slug, user_dir=_dir(request)
        ),
        status_code=201,
    )


@router.post("/validate", summary="Validate a document or edits (never fails on invalid input)")
def validate(body: ValidateInput, request: Request, session: SessionDep) -> ApiJSON:
    return respond(
        sc.validate(
            session,
            yaml_text=body.yaml,
            document=body.document,
            base=body.base,
            edits=body.edits,
            user_dir=_dir(request),
        )
    )


@router.get("/{slug}", summary="A scenario: YAML, parsed document, validation")
def get(slug: str, request: Request, session: SessionDep) -> ApiJSON:
    return respond(sc.get(session, slug, _dir(request)))


@router.put("/{slug}", summary="Update a user scenario (document and/or edits)")
def update(slug: str, body: ScenarioUpdate, request: Request, session: SessionDep) -> ApiJSON:
    return respond(
        sc.update(
            session,
            slug,
            yaml_text=body.yaml,
            document=body.document,
            edits=body.edits,
            user_dir=_dir(request),
        )
    )


@router.delete("/{slug}", summary="Delete an unused user scenario")
def delete(slug: str, request: Request, session: SessionDep) -> ApiJSON:
    return respond(sc.delete(session, slug, _dir(request)))


@router.post("/{slug}/duplicate", status_code=201, summary="Duplicate a scenario as a user scenario")
def duplicate(slug: str, body: Duplicate, request: Request, session: SessionDep) -> ApiJSON:
    return respond(
        sc.duplicate(session, slug, body.new_slug, seed=body.seed, name=body.name, user_dir=_dir(request)),
        status_code=201,
    )


@router.get("/{slug}/export", summary="Download the fully merged YAML", response_class=Response)
def export(slug: str, request: Request, session: SessionDep) -> Response:
    filename, text = sc.export_yaml(session, slug, _dir(request))
    return Response(
        text,
        media_type="application/x-yaml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
