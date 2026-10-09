"""Read payloads of the in-between local elections (docs/LOCAL_ELECTIONS.md): the calendar of
local election days with what each ballot holds, and the list of local elections."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.constitution import ElectionType
from app.core.errors import ValidationError
from app.models import Election, Province, Race
from app.services.read._base import SIMULATED, cached, db_token, iso


def calendar(
    session: Session,
    *,
    start: date | None = None,
    end: date | None = None,
    province: str | None = None,
) -> dict[str, Any]:
    """``GET /api/local/calendar`` — local election days between ``start`` and ``end``
    (default: the next twelve months after the latest reported election) with what is on each
    ballot and the stored election (id, status) when it exists."""
    from app.services import local as local_service

    lo = start or local_service.latest_reported_date(session) or date.today()
    hi = end or lo + timedelta(days=365)
    if hi < lo:
        raise ValidationError("end must not be before start")
    if (hi - lo).days > 3 * 366:
        raise ValidationError("the calendar covers at most three years at a time")
    pv = province.upper() if province else None

    def build() -> dict[str, Any]:
        stored = {
            (p, d): (int(i), s)
            for i, p, d, s in session.execute(
                select(Election.id, Province.code, Election.election_date, Election.status)
                .join(Province, Province.id == Election.province_id)
                .where(Election.election_type == ElectionType.LOCAL.value)
            ).all()
        }
        cal = local_service.get_local_calendar(session)
        days = []
        for plan in local_service.plan_local_elections(session, lo - timedelta(days=1), hi):
            if pv and plan.day.province_code != pv:
                continue
            d = plan.to_dict()
            hit = stored.get((plan.day.province_code, plan.day.date))
            d["election_id"], d["status"] = hit if hit is not None else (None, None)
            days.append(d)
        provinces = {
            p: [{"month": r.month, "occurrence": r.occurrence} for r in rules]
            for p, rules in cal.province_slots.items()
        }
        return {"days": days, "province_days": provinces}

    payload = cached(session, "local_calendar", (lo, hi, pv, db_token(session)), build)
    return {
        "data_category": "FICTIONAL",
        "start": lo.isoformat(),
        "end": hi.isoformat(),
        "count": len(payload["days"]),
        **payload,
    }


def elections(session: Session, *, year: int | None = None, province: str | None = None) -> dict[str, Any]:
    """``GET /api/local/elections`` — the stored local elections (newest first) with their race
    counts by type."""
    q = (
        select(Election, Province.code, Province.name)
        .join(Province, Province.id == Election.province_id)
        .where(Election.election_type == ElectionType.LOCAL.value)
    )
    if year is not None:
        q = q.where(Election.year == int(year))
    if province:
        q = q.where(Province.code == province.upper())
    rows = session.execute(q.order_by(Election.election_date.desc(), Province.code)).all()
    ids = [int(e.id) for e, _, _ in rows]
    counts: dict[int, dict[str, int]] = {}
    if ids:
        for eid, rt, n in session.execute(
            select(Race.election_id, Race.race_type, func.count())
            .where(Race.election_id.in_(ids))
            .group_by(Race.election_id, Race.race_type)
        ).all():
            counts.setdefault(int(eid), {})[str(rt)] = int(n)
    items = [
        {
            "id": int(e.id),
            "name": e.name,
            "date": e.election_date.isoformat(),
            "year": e.year,
            "province_code": pc,
            "province_name": pn,
            "status": e.status,
            "races": sum(counts.get(int(e.id), {}).values()),
            "race_counts": counts.get(int(e.id), {}),
            "finalized_at": iso(e.finalized_at),
        }
        for e, pc, pn in rows
    ]
    return {"data_category": SIMULATED, "count": len(items), "elections": items}
