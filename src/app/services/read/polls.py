"""Polling read models: FICTIONAL polls by invented pollsters and their SIMULATED averages
(recency / sample / rating weights, house effects, trend) via :mod:`app.polling.aggregate`.

Polls are generated from the model's pre-election *expectation* and never carry audit data about
the realised result (true shares, industry error); the stored rows are therefore safe to show
before an election.  For reported elections the average is compared with the actual result
(``accuracy``)."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.polling.aggregate import aggregate_polls
from app.polling.service import load_pollster_configs, polls_frames
from app.polling.types import NATIONAL_GEO, POLL_TYPES
from app.services.read._base import FICTIONAL, SIMULATED, ElectionRef, cached, iso, party_index, rnd

log = get_logger(__name__)

MAX_LIMIT = 2000


def _frames(session: Session, ref: ElectionRef, poll_type: str | None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Poll frames of an election (cached per election state; do not mutate)."""
    n = _poll_count(session, ref)
    return cached(session, "poll-frames", (ref.id, poll_type, n), lambda: polls_frames(session, ref.id, poll_type))


def _poll_count(session: Session, ref: ElectionRef) -> tuple[int, int]:
    from sqlalchemy import func, select

    from app.models import Poll

    row = session.execute(
        select(func.count(Poll.id), func.max(Poll.id)).where(Poll.election_id == ref.id)
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


def _check_type(poll_type: str | None) -> str | None:
    if poll_type is not None and poll_type not in POLL_TYPES:
        raise ValidationError(f"unknown poll type {poll_type!r}; expected one of {list(POLL_TYPES)}")
    return poll_type


def _pollsters(session: Session) -> list[dict[str, Any]]:
    return [
        {
            "name": p.name,
            "rating": rnd(p.rating, 3),
            "rating_label": p.rating_label,
            "method": p.method,
            "house_effects": {k: rnd(v, 3) for k, v in dict(p.house_effects).items()},
            "data_category": FICTIONAL,
        }
        for p in load_pollster_configs(session)
    ]


def polls(
    session: Session,
    ref: ElectionRef,
    *,
    poll_type: str | None = None,
    geo: str | None = None,
    pollster: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    """``GET /api/polls/{id}?type=&geo=&pollster=`` — the election's polls (newest first) with
    their results, the poll groups and the pollsters (ratings, prior house effects)."""
    _check_type(poll_type)
    if limit < 1 or limit > MAX_LIMIT:
        raise ValidationError(f"limit must be between 1 and {MAX_LIMIT}")
    pdf, rdf = _frames(session, ref, poll_type)
    groups: list[dict[str, Any]] = []
    if not pdf.empty:
        g = pdf.groupby(["poll_type", "geo_code"], sort=True).agg(
            polls=("poll_id", "size"), latest=("end_date", "max"), first=("end_date", "min")
        )
        groups = [
            {
                "poll_type": str(t),
                "geo_code": str(gc),
                "polls": int(r["polls"]),
                "first_end_date": iso(r["first"].date()),
                "latest_end_date": iso(r["latest"].date()),
            }
            for (t, gc), r in g.iterrows()
        ]
    sel = pdf
    if geo is not None:
        sel = sel[sel["geo_code"] == geo.upper()]
    if pollster is not None:
        sel = sel[sel["pollster"] == pollster]
    sel = sel.sort_values(["end_date", "poll_id"], ascending=[False, False])
    total = len(sel)
    page = sel.iloc[offset : offset + limit]
    res: dict[int, dict[str, float]] = {}
    if not rdf.empty and not page.empty:
        sub = rdf[rdf["poll_id"].isin(page["poll_id"])]
        for pid, key, val in sub[["poll_id", "key", "value_pct"]].itertuples(index=False):
            res.setdefault(int(pid), {})[str(key)] = round(float(val), 2)
    rows = [
        {
            "id": int(r.poll_id),
            "pollster": r.pollster,
            "pollster_rating": rnd(r.pollster_rating, 3),
            "poll_type": r.poll_type,
            "geo_code": r.geo_code,
            "start_date": iso(r.start_date.date()) if pd.notna(r.start_date) else None,
            "end_date": iso(r.end_date.date()) if pd.notna(r.end_date) else None,
            "sample_size": int(r.sample_size),
            "population": r.population,
            "method": r.method,
            "margin_of_error": rnd(r.margin_of_error, 2),
            "undecided_pct": rnd(r.undecided_pct, 2),
            "results": res.get(int(r.poll_id), {}),
        }
        for r in page.itertuples(index=False)
    ]
    colors = {c: p["color"] for c, p in party_index(session).items()}
    return ref.envelope(
        data_category=SIMULATED,
        provenance={"pollsters": FICTIONAL, "polls": SIMULATED},
        total=total,
        limit=limit,
        offset=offset,
        colors=colors,
        groups=groups,
        pollsters=_pollsters(session),
        polls=rows,
    )


def _default_type(pdf: pd.DataFrame) -> str:
    types = set(pdf["poll_type"]) if not pdf.empty else set()
    for t in ("national_president", "generic_house"):
        if t in types:
            return t
    return sorted(types)[0] if types else "national_president"


def poll_average(
    session: Session,
    ref: ElectionRef,
    *,
    poll_type: str | None = None,
    geo: str | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """``GET /api/polls/{id}/average?type=&geo=&as_of=`` — weighted polling average of one poll
    group (default: the national presidential or generic House ballot, as of the day before the
    election): mean, standard error, interval, trend line, house effects and the per-poll weight
    table; for reported elections the ``accuracy`` against the actual result."""
    _check_type(poll_type)
    pdf_all, _ = _frames(session, ref, None)
    ptype = poll_type or _default_type(pdf_all)
    geo_code = (geo or NATIONAL_GEO).upper()
    day = as_of or (ref.election_date - timedelta(days=1))

    def build() -> dict[str, Any]:
        pdf, rdf = _frames(session, ref, ptype)
        groups = sorted({(str(t), str(g)) for t, g in pdf[["poll_type", "geo_code"]].itertuples(index=False)}) if not pdf.empty else []
        averages = aggregate_polls(pdf, rdf, None, day, pollsters=load_pollster_configs(session))
        avg = averages.get((ptype, geo_code))
        if avg is None:
            raise NotFoundError(
                f"no polling average for {ptype}/{geo_code} as of {day.isoformat()} "
                f"(groups: {[f'{t}/{g}' for t, g in groups][:20]})"
            )
        d = avg.to_dict(include_polls=True, include_trend=True)
        d["groups"] = [{"poll_type": t, "geo_code": g} for t, g in groups]
        d["accuracy"] = _accuracy(session, ref, ptype, geo_code, d["mean"]) if ref.reported else None
        return d

    payload = cached(session, "poll-average", (*ref.cache_key(), _poll_count(session, ref), ptype, geo_code, day.isoformat()), build)
    colors = {c: p["color"] for c, p in party_index(session).items()}
    return ref.envelope(
        data_category=SIMULATED,
        poll_type=ptype,
        geo_code=geo_code,
        as_of=day.isoformat(),
        colors=colors,
        average=payload,
    )


def _accuracy(
    session: Session, ref: ElectionRef, poll_type: str, geo: str, mean: dict[str, float]
) -> dict[str, Any] | None:
    """Final polling average minus the actual result (pp) per key — reported elections only."""
    from app.services.read.elections import house, president, provinces_results

    actual: dict[str, float] = {}
    try:
        if poll_type == "national_president":
            for t in president(session, ref)["tickets"]:
                if t["party"] and t["pct"] is not None:
                    actual[t["party"]] = actual.get(t["party"], 0.0) + float(t["pct"])
        elif poll_type == "generic_house":
            for p in house(session, ref)["by_party"]:
                if p["vote_pct"] is not None:
                    actual[p["party"]] = float(p["vote_pct"])
        elif poll_type == "province_president":
            party_of = {t["key"]: t["party"] for t in president(session, ref)["tickets"]}
            row = next((r for r in provinces_results(session, ref, "PRES")["provinces"] if r["code"] == geo), None)
            for k, v in ((row or {}).get("pct") or {}).items():
                if party_of.get(k):
                    actual[party_of[k]] = actual.get(party_of[k], 0.0) + float(v)
        else:
            return None
    except NotFoundError:
        return None
    if not actual:
        return None
    errors = {k: round(float(mean[k]) - actual.get(k, 0.0), 3) for k in mean}
    return {
        "actual_pct": {k: round(v, 3) for k, v in actual.items()},
        "error_pp": errors,
        "mean_abs_error_pp": round(sum(abs(v) for v in errors.values()) / len(errors), 3) if errors else None,
        "data_category": SIMULATED,
    }
