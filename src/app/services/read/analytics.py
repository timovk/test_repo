"""Per-election analytics read model (reported elections only), computed with
:mod:`app.analytics.metrics` over the standard results frame: partisan lean, elasticity,
competitiveness, efficiency gap, seat–vote relationship, Electoral College efficiency and the
tipping-point margin (EC bias).  Descriptive statistics of SIMULATED results — not predictions."""

from __future__ import annotations

from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from app.analytics.metrics import (
    competitiveness,
    competitiveness_summary,
    efficiency_gap,
    elasticity,
    electoral_college_tally,
    margin_lean,
    partisan_lean,
    party_efficiency,
    tipping_point_from_frame,
)
from app.analytics.results import ResultsFrameError
from app.core.constitution import RaceType
from app.core.logging import get_logger
from app.services.read._base import ElectionRef, cached, reported_refs
from app.services.read.elections import _frame
from app.services.read.history import _has_types, _party, _records, _val

log = get_logger(__name__)

PRES_TYPES = (RaceType.PRESIDENT, RaceType.PRESIDENT_PROVINCE)
ALL_TYPES = tuple(RaceType)


def _safe(label: str, fn: Any) -> Any:
    try:
        return fn()
    except (ResultsFrameError, ValueError, KeyError, IndexError, ZeroDivisionError) as exc:
        log.info("analytics %s unavailable: %s", label, exc)
        return None


def _lean(frame: pd.DataFrame) -> dict[str, Any] | None:
    prov = frame[
        (frame["level"] == "province").to_numpy()
        & (frame["race_type"] == RaceType.PRESIDENT.value).to_numpy()
    ]
    if prov.empty:
        return None
    lean = partisan_lean(prov, level="province", by="race")
    by_geo: dict[str, dict[str, Any]] = {}
    for r in lean.itertuples(index=False):
        g = by_geo.setdefault(
            str(r.geo_code), {"code": str(r.geo_code), "name": _val(r.geo_name), "lean_pp": {}}
        )
        g["lean_pp"][_party(r.party)] = round(float(r.lean_pp), 3)
    two = _safe("margin lean", lambda: margin_lean(prov, level="province", by="race"))
    if two is not None and not two.empty:
        for r in two.itertuples(index=False):
            g = by_geo.get(str(r.geo_code))
            if g is not None:
                g["two_party_lean_pp"] = _val(r.lean_pp, 3)
                g["leans"] = (
                    _party(r.leans) if isinstance(r.leans, str) and r.leans != "even" else _val(r.leans)
                )
        pair = [_party(two["party_a"].iloc[0]), _party(two["party_b"].iloc[0])] if "party_a" in two else None
    else:
        pair = None
    return {"level": "province", "race": "PRES", "pair": pair, "provinces": list(by_geo.values())}


def _elasticity(session: Session, ref: ElectionRef) -> dict[str, Any] | None:
    refs = [
        r
        for r in reported_refs(session)
        if r.election_date <= ref.election_date and _has_types(session, r, PRES_TYPES)
    ]
    if len(refs) < 2:
        return {
            "available": False,
            "reason": "needs at least two reported presidential elections",
            "provinces": [],
        }
    frames = [_frame(session, r, ("province", "national"), (RaceType.PRESIDENT,)) for r in refs]
    df = pd.concat(frames, ignore_index=True)
    el = _safe("elasticity", lambda: elasticity(df, level="province", by="race"))
    if el is None or el.empty:
        return {"available": False, "reason": "national shares did not move enough", "provinces": []}
    by_geo: dict[str, dict[str, Any]] = {}
    for r in el.itertuples(index=False):
        g = by_geo.setdefault(
            str(r.geo_code), {"code": str(r.geo_code), "name": _val(r.geo_name), "elasticity": {}}
        )
        g["elasticity"][_party(r.party)] = _val(r.elasticity, 3)
        g["method"] = _val(r.method)
        g["n_obs"] = _val(r.n_obs)
    return {"available": True, "elections": [r.id for r in refs], "provinces": list(by_geo.values())}


def election_analytics(session: Session, ref: ElectionRef) -> dict[str, Any]:
    """``GET /api/analytics/{id}`` — lean, elasticity, competitiveness, efficiency gap, seat–vote,
    EC efficiency and tipping-point margin of a reported election (hidden elections: an empty
    envelope with ``available: false``)."""
    if not ref.reported:
        return ref.envelope(available=False)

    def build() -> dict[str, Any]:
        out: dict[str, Any] = {"available": True}
        has_pres = _has_types(session, ref, PRES_TYPES)
        if has_pres:
            pf = _frame(session, ref, ("province", "national"), PRES_TYPES)
            out["lean"] = _safe("lean", lambda: _lean(pf))
            out["elasticity"] = _elasticity(session, ref)
            ev = _ev_by_province(session, ref)
            tally = _safe("ec tally", lambda: electoral_college_tally(pf, ev, by="party"))
            out["ec_efficiency"] = (
                None
                if tally is None
                else [
                    {
                        "party": _party(r["key"]),
                        "label": _val(r["label"]),
                        "popular_votes": _val(r["popular_votes"]),
                        "pv_share": _val(r["pv_share"], 6),
                        "electoral_votes": _val(r["electoral_votes"]),
                        "ev_share": _val(r["ev_share"], 6),
                        "provinces_won": _val(r["provinces_won"]),
                        "efficiency_pp": _val(r["efficiency_pp"], 4),
                    }
                    for r in tally.to_dict(orient="records")
                ]
            )
            bias = _safe("tipping point", lambda: tipping_point_from_frame(pf, ev, by="party"))
            out["tipping_point"] = (
                None
                if bias is None
                else {
                    "party": _party(bias.key),
                    "province_code": bias.tipping_province,
                    "margin_pp": round(bias.tipping_margin_pp, 4),
                    "national_margin_pp": round(bias.national_margin_pp, 4),
                    "ec_bias_pp": round(bias.bias_pp, 4),
                }
            )
        all_frame = _frame(session, ref, ("municipality", "district", "province", "national"), ALL_TYPES)
        comp = _safe("competitiveness", lambda: competitiveness(all_frame))
        if comp is not None and not comp.empty:
            summ = competitiveness_summary(comp, by="race_type")
            top = comp.sort_values(
                ["competitiveness", "margin_pp"], ascending=[False, True], kind="mergesort"
            ).head(15)
            out["competitiveness"] = {
                "by_race_type": _records(summ.reset_index() if "race_type" not in summ.columns else summ),
                "most_competitive": [
                    {
                        "race_code": r["race_code"],
                        "race_type": r["race_type"],
                        "geo_code": _val(r["geo_code"]),
                        "winner_party": _party(r["winner_party"]),
                        "runner_up_party": _party(r["runner_up_party"]),
                        "margin_pp": _val(r["margin_pp"], 4),
                        "enc": _val(r["enc"], 3),
                        "competitiveness": _val(r["competitiveness"], 4),
                        "rating": _val(r["rating"]),
                    }
                    for r in top.to_dict(orient="records")
                ],
            }
        house = all_frame[(all_frame["race_type"] == RaceType.HOUSE.value).to_numpy()]
        if not house.empty:
            eg = _safe("efficiency gap", lambda: efficiency_gap(house))
            pe = _safe("party efficiency", lambda: party_efficiency(house))
            out["efficiency_gap"] = None if eg is None else _records(eg, 6)
            out["seat_vote"] = (
                None
                if pe is None
                else [
                    {
                        "party": _party(r["party"]),
                        "votes": _val(r["votes"]),
                        "vote_share": _val(r["vote_share"], 6),
                        "seats": _val(r["seats"]),
                        "seat_share": _val(r["seat_share"], 6),
                        "seat_bonus_pp": _val(r["seat_bonus_pp"], 4),
                        "waste_rate": _val(r["waste_rate"], 6),
                        "efficiency_gap": _val(r["efficiency_gap"], 6),
                        "votes_per_seat": _val(r["votes_per_seat"], 1),
                    }
                    for r in pe.to_dict(orient="records")
                ]
            )
        out["notes"] = [
            "Lean: province share minus national share (pp) in the presidential race.",
            "Elasticity: slope of a province's share on the national share across reported elections.",
            "Efficiency gap: positive values favour the named party (it wastes fewer votes).",
        ]
        return ref.envelope(**out)

    return cached(session, "analytics", ref.cache_key(), build)


def _ev_by_province(session: Session, ref: ElectionRef) -> dict[str, int]:
    """Province code → electoral votes of the election's province contests."""
    from sqlalchemy import select

    from app.models import Province, Race

    return {
        str(c): int(v or 0)
        for c, v in session.execute(
            select(Province.code, Race.electoral_votes)
            .join(Province, Province.id == Race.province_id)
            .where(Race.election_id == ref.id, Race.race_type == RaceType.PRESIDENT_PROVINCE.value)
        ).all()
    }


__all__ = ["election_analytics"]
