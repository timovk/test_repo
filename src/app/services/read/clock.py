"""Read payloads and actions of the world clock (docs/CLOCK.md): today, the agenda, the news,
moving to the next election day, and counting / skipping as background jobs (an instant count of
a big election or a skip over years takes from seconds to minutes)."""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import ElectionError, ValidationError
from app.core.logging import get_logger
from app.services import clock
from app.services.read._base import FICTIONAL, clear_read_cache, ref_of

log = get_logger(__name__)


def _brief(session: Session, el: Any) -> dict[str, Any]:
    return {**ref_of(el).brief(), "provinces": [p for p in (el.provinces or "").split(",") if p]}


def _label(d: date) -> str:
    from app.elections.local_calendar import LocalCalendar

    return f"{d.strftime('%A')} {LocalCalendar.format_date(d)}"


# --------------------------------------------------------------------------- reads
def state(session: Session) -> dict[str, Any]:
    """``GET /api/clock`` — today, today's elections (with whether they are finished), the next
    election day and the running job."""
    t = clock.today(session)
    todays = clock.elections_on(session, t)
    pending = clock.unfinished_through(session, t)
    nxt = clock.next_election_day(session, t)
    return {
        "data_category": FICTIONAL,
        "today": t.isoformat(),
        "today_label": _label(t),
        "clock_set": clock.stored_today(session) is not None,
        "elections_today": [_brief(session, e) for e in todays],
        "pending": [_brief(session, e) for e in pending],
        "can_advance": not pending,
        "next": None if nxt is None else {**nxt.to_dict(), "label": _label(nxt.date)},
        "job": job_status(),
    }


def agenda(session: Session, *, start: date | None = None, end: date | None = None) -> dict[str, Any]:
    """``GET /api/clock/agenda`` — election days between ``start`` (default: 120 days before
    today) and ``end`` (default: a year after today): finished, today's and planned ones."""
    t = clock.today(session)
    lo = start or t - timedelta(days=120)
    hi = end or t + timedelta(days=365)
    if hi < lo:
        raise ValidationError("end must not be before start")
    if (hi - lo).days > 3 * 366:
        raise ValidationError("the agenda covers at most three years at a time")
    days = clock.agenda(session, lo - timedelta(days=1), hi)
    out = []
    for d in days:
        item = {**d.to_dict(), "label": _label(d.date)}
        item["when"] = "past" if d.date < t else ("today" if d.date == t else "upcoming")
        out.append(item)
    return {
        "data_category": FICTIONAL,
        "today": t.isoformat(),
        "start": lo.isoformat(),
        "end": hi.isoformat(),
        "count": len(out),
        "days": out,
    }


def news(session: Session, *, since: date | None = None, until: date | None = None) -> dict[str, Any]:
    """``GET /api/clock/news`` — what happened between ``since`` (default: 180 days before today)
    and ``until`` (default: today), newest first."""
    t = clock.today(session)
    hi = until or t
    lo = since or hi - timedelta(days=180)
    items = clock.news(session, lo, hi)
    items.reverse()
    return {
        "data_category": FICTIONAL,
        "today": t.isoformat(),
        "since": lo.isoformat(),
        "until": hi.isoformat(),
        "count": len(items),
        "items": items,
    }


# --------------------------------------------------------------------------- actions
def advance(session: Session) -> dict[str, Any]:
    """``POST /api/clock/next`` — move to the next election day and create its election."""
    _require_idle()
    try:
        out = clock.advance(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        clear_read_cache()
    from app.models import Election

    el = session.get(Election, out["election_id"])
    return {**state(session), "arrived": {**out, "election": _brief(session, el) if el is not None else None}}


def watch(session: Session) -> dict[str, Any]:
    """``POST /api/clock/watch`` — simulate today's election so its night can start (the UI then
    opens the election night)."""
    _require_idle()
    try:
        el = clock.prepare_today(session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        clear_read_cache()
    return {**state(session), "election_id": int(el.id)}


def count(session: Session) -> dict[str, Any]:
    """``POST /api/clock/count`` — finish today's election instantly (a background job)."""
    if not clock.unfinished_through(session, clock.today(session)):
        raise ElectionError("there is nothing to count today")
    _start_job(
        session,
        "count",
        lambda s, mgr, commit, progress: {
            "counted": clock.count_today(s, manager=mgr, commit=commit, progress=progress)
        },
    )
    return state(session)


def skip(session: Session, target: date) -> dict[str, Any]:
    """``POST /api/clock/skip`` — count every election day before ``target`` and move the clock
    there (a background job)."""
    t = clock.today(session)
    if target <= t:
        raise ElectionError(f"the clock only moves forward (today is {t.isoformat()})")
    if (target - t).days > 12 * 366:
        raise ValidationError("skip at most twelve years at a time")
    _start_job(
        session,
        "skip",
        lambda s, mgr, commit, progress: clock.skip_to(
            s, target, manager=mgr, commit=commit, progress=progress
        ),
    )
    return state(session)


# --------------------------------------------------------------------------- background job
@dataclass
class ClockJob:
    id: int
    kind: str
    status: str = "running"  # running | done | error
    started: float = field(default_factory=time.time)
    finished: float | None = None
    done: int = 0
    total: int = 0
    current: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "running": self.status == "running",
            "done": self.done,
            "total": self.total,
            "current": self.current,
            "seconds": round((self.finished or time.time()) - self.started, 1),
            "result": self.result,
            "error": self.error,
        }


_lock = threading.Lock()
_job: ClockJob | None = None
_seq = 0


def job_status() -> dict[str, Any] | None:
    with _lock:
        return None if _job is None else _job.to_dict()


def _require_idle() -> None:
    with _lock:
        if _job is not None and _job.status == "running":
            raise ElectionError(f"the clock is busy ({_job.kind}: {_job.current or 'starting'})")


def _start_job(session: Session, kind: str, fn: Any) -> None:
    global _job, _seq
    from app.services.read.live import manager_for, night_available, session_url

    url = session_url(session)
    manager = manager_for(session) if night_available() else None
    with _lock:
        if _job is not None and _job.status == "running":
            raise ElectionError(f"the clock is busy ({_job.kind}: {_job.current or 'starting'})")
        _seq += 1
        job = _job = ClockJob(_seq, kind)

    def progress(name: str, done: int, total: int) -> None:
        with _lock:
            job.current, job.done = name, done
            job.total = max(job.total, total)

    def run() -> None:
        from app.db.session import session_scope

        try:
            with session_scope(url) as s:

                def commit() -> None:
                    s.commit()
                    clear_read_cache()

                result = fn(s, manager, commit, progress)
            with _lock:
                job.result = result
                job.status = "done"
                job.done = max(job.done, job.total)
        except Exception as exc:
            log.error("clock job %s failed: %s\n%s", kind, exc, traceback.format_exc())
            with _lock:
                job.status, job.error = "error", str(exc)
        finally:
            clear_read_cache()
            with _lock:
                job.finished = time.time()

    threading.Thread(target=run, name=f"clock-{kind}-{job.id}", daemon=True).start()


def wait_for_job(timeout: float = 600.0) -> dict[str, Any] | None:
    """Block until the running job ends (tests and the CLI)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = job_status()
        if st is None or not st["running"]:
            return st
        time.sleep(0.1)
    return job_status()
