"""Election night (SIMULATED): live state, playback control, Server-Sent Events, live maps and
race details, manual call overrides — all served by the night service (``app.services.night``)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

import orjson
from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import ApiJSON, ElectionDep, SessionDep, respond
from app.core.logging import get_logger
from app.services.read._base import clear_read_cache
from app.services.read.live import manager_for, manager_for_url

log = get_logger(__name__)

router = APIRouter(prefix="/night", tags=["election night"])

#: Seconds between state pushes while a night runs / is idle, and between heartbeats.
STREAM_INTERVAL_RUNNING = 1.0
STREAM_INTERVAL_IDLE = 2.0
HEARTBEAT_S = 15.0


class Control(BaseModel):
    action: Literal["start", "pause", "resume", "speed", "step", "finish", "reset"]
    speed: float | None = Field(None, gt=0, description="playback speed (one of the configured speeds)")


class Override(BaseModel):
    status: str = Field(..., description="LEAN | PROJECTED_WINNER | CALLED | TOO_CLOSE_TO_CALL | … or CLEAR")
    line_key: str | None = Field(None, description="line to name (LEAN / PROJECTED_WINNER / CALLED)")
    reason: str = Field(..., min_length=1, description="why the producer overrides the model")


@router.get("/{election_id}/state", summary="Live snapshot and clock")
def state(ref: ElectionDep, session: SessionDep, detail: Literal["summary", "full"] = "summary") -> ApiJSON:
    return respond(manager_for(session).state(ref.id, detail=detail))


@router.post("/{election_id}/control", summary="Playback control")
def control(ref: ElectionDep, body: Control, session: SessionDep) -> ApiJSON:
    """``start`` (the election becomes LIVE), ``pause``, ``resume``, ``speed`` (with ``speed``),
    ``step``, ``finish`` (applies every remaining event and finalizes the election) and
    ``reset``.  Returns the new state."""
    out = manager_for(session).control(ref.id, body.action, speed=body.speed)
    clear_read_cache()
    return respond(out)


@router.get("/{election_id}/municipalities", summary="Live municipality map rows of one race")
def municipalities(ref: ElectionDep, session: SessionDep, race: str = "PRES") -> ApiJSON:
    rows = manager_for(session).municipality_rows(ref.id, race.upper())
    return respond(
        {
            "election_id": ref.id,
            "race": race.upper(),
            "data_category": "SIMULATED",
            "count": len(rows),
            "municipalities": rows,
        }
    )


@router.get("/{election_id}/municipalities/{code}", summary="Live municipality detail")
def municipality(ref: ElectionDep, code: str, session: SessionDep) -> ApiJSON:
    return respond(manager_for(session).municipality_detail(ref.id, code.upper()))


@router.get("/{election_id}/races/{race}", summary="Live race detail with call evidence")
def race(ref: ElectionDep, race: str, session: SessionDep) -> ApiJSON:
    return respond(manager_for(session).race_detail(ref.id, race.upper()))


@router.post("/{election_id}/calls/{race}/override", summary="Manual race-call override")
def override(ref: ElectionDep, race: str, body: Override, session: SessionDep) -> ApiJSON:
    out = manager_for(session).override_call(ref.id, race.upper(), body.status, body.line_key, body.reason)
    clear_read_cache()
    return respond(out)


def _sse(data: dict[str, Any], event_id: Any = None) -> bytes:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return (
        head.encode()
        + b"data: "
        + orjson.dumps(data, option=orjson.OPT_SERIALIZE_NUMPY | orjson.OPT_NON_STR_KEYS)
        + b"\n\n"
    )


def _marker(st: dict[str, Any]) -> tuple:
    clk = st.get("clock") or {}
    snap = st.get("snapshot") or {}
    return (
        clk.get("status"),
        clk.get("speed"),
        clk.get("seq"),
        snap.get("seq"),
        clk.get("clock"),
        st.get("election_status"),
    )


@router.get("/{election_id}/stream", summary="Server-Sent Events: the summary state whenever it changes")
async def stream(
    ref: ElectionDep,
    request: Request,
    max_events: int | None = Query(
        None, ge=1, description="close after this many state events (tests / tools)"
    ),
    timeout_s: float | None = Query(None, gt=0, description="close after this many seconds"),
) -> StreamingResponse:
    """``text/event-stream``: an unnamed ``message`` event carrying the same JSON as
    ``GET /state`` (summary) immediately and then whenever the state changes (checked every
    second while the night runs, every 2 s otherwise), ``: heartbeat`` comments every 15 s and a
    ``retry: 3000`` hint.  The stream ends when the client disconnects (or after ``max_events`` /
    ``timeout_s``)."""
    manager = manager_for_url(request.app.state.db.url)
    election_id = ref.id

    async def events() -> AsyncIterator[bytes]:
        yield b"retry: 3000\n\n"
        sent = 0
        last: tuple | None = None
        start = last_beat = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            try:
                st = await run_in_threadpool(manager.state, election_id, "summary")
            except Exception as exc:
                yield b"event: error\ndata: " + orjson.dumps({"detail": str(exc), "code": "error"}) + b"\n\n"
                break
            mark = _marker(st)
            now = time.monotonic()
            if mark != last:
                last = mark
                sent += 1
                yield _sse(st, (st.get("clock") or {}).get("seq"))
                last_beat = now
                if max_events is not None and sent >= max_events:
                    break
            elif now - last_beat >= HEARTBEAT_S:
                last_beat = now
                yield b": heartbeat\n\n"
            if timeout_s is not None and now - start >= timeout_s:
                break
            running = (st.get("clock") or {}).get("status") == "running"
            await asyncio.sleep(STREAM_INTERVAL_RUNNING if running else STREAM_INTERVAL_IDLE)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
