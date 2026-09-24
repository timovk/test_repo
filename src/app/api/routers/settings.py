"""UI settings persisted in ``app_meta``."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import ApiJSON, SessionDep, respond
from app.services.read import actions
from app.services.read import meta as meta_read

router = APIRouter(tags=["system"])


class SettingsPatch(BaseModel):
    """Partial UI settings; omitted fields keep their stored value."""

    theme: Literal["dark", "light"] | None = None
    playback_speed: float | None = Field(None, gt=0)
    party_colors: dict[str, str] | None = None
    map_metric: Literal["margin", "share", "turnout", "swing", "reporting"] | None = None
    show_provenance_badges: bool | None = None
    default_election_id: int | None = None


@router.get("/settings", summary="UI settings (theme, playback speed, party colour overrides)")
def get_settings(session: SessionDep) -> ApiJSON:
    return respond(meta_read.settings_payload(session))


@router.put("/settings", summary="Update UI settings")
def put_settings(body: SettingsPatch, session: SessionDep) -> ApiJSON:
    return respond(actions.update_settings(session, body.model_dump(exclude_unset=True)))
