"""Forecast read models: stored Monte Carlo runs (SIMULATED model estimates, never
predictions) rendered for the UI — win and majority probabilities, EV / seat distributions,
province and district win frequencies, tipping-point frequencies and the most common Electoral
College maps."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_constitution
from app.core.errors import NotFoundError
from app.forecasting.result import DISCLAIMER
from app.forecasting.service import RUN_KIND, list_forecasts
from app.models import BallotCandidate, Election, Race, SimulationRun
from app.services.read._base import (
    SIMULATED,
    ElectionRef,
    cached,
    iso,
    party_index,
    province_maps,
    ref_of,
    rnd,
)

TOP_COMBINATIONS = 10


def _ticket_info(session: Session, election_id: int) -> dict[str, dict[str, Any]]:
    rows = session.execute(
        select(BallotCandidate)
        .join(Race, Race.id == BallotCandidate.race_id)
        .where(Race.election_id == election_id, Race.code == "PRES")
    ).scalars()
    return {
        (b.line_key or str(b.id)): {
            "name": b.ballot_name,
            "party": b.party_code_snapshot,
            "color": b.party_color_snapshot,
        }
        for b in rows
    }


def _chamber(ch: dict[str, Any] | None, colors: dict[str, str], *, races: bool) -> dict[str, Any] | None:
    if not ch:
        return None
    parties = [
        {
            "party": k,
            "color": colors.get(k),
            "seats": v.get("seats"),
            "histogram": v.get("histogram"),
            "prob_majority": v.get("prob_majority"),
            "prob_plurality": v.get("prob_plurality"),
            "holdover": v.get("holdover", 0),
        }
        for k, v in (ch.get("parties") or {}).items()
    ]
    parties.sort(key=lambda p: -((p["seats"] or {}).get("mean") or 0.0))
    out = {
        "chamber": ch.get("chamber"),
        "seats_total": ch.get("seats_total"),
        "seats_up": ch.get("seats_up"),
        "majority": ch.get("majority"),
        "prob_no_majority": ch.get("prob_no_majority"),
        "parties": parties,
        "compositions": ch.get("compositions") or [],
    }
    if races:
        out["races"] = [
            {"race_code": code, "win": win, "favourite": max(win, key=win.get) if win else None}
            for code, win in sorted((ch.get("race_win") or {}).items())
        ]
    return out


def forecast_view(
    session: Session, run_id: int, *, include_races: bool = False, include_municipalities: bool = False
) -> dict[str, Any]:
    """``GET /api/forecast/runs/{run_id}`` — one stored forecast run."""
    run = session.get(SimulationRun, run_id)
    if run is None or run.kind != RUN_KIND:
        raise NotFoundError(f"forecast run {run_id} not found")

    def build() -> dict[str, Any]:
        res = json.loads(run.summary_json) if run.summary_json else {}
        el = session.get(Election, run.election_id) if run.election_id is not None else None
        ref = ref_of(el) if el is not None else None
        tickets_meta = _ticket_info(session, run.election_id) if run.election_id is not None else {}
        colors = {c: p["color"] for c, p in party_index(session).items()}
        _ids, pmap = province_maps(session)
        pres = res.get("president")
        president = None
        if pres:
            president = {
                "method": pres.get("method"),
                "total_ev": pres.get("total_ev"),
                "majority": pres.get("majority"),
                "label": f"{pres.get('majority')} TO WIN",
                "prob_contingent": pres.get("prob_contingent"),
                "prob_ev_tie": pres.get("prob_ev_tie"),
                "prob_pv_ev_divergence": pres.get("prob_pv_ev_divergence"),
                "tickets": sorted(
                    (
                        {
                            "key": t["key"],
                            "name": (tickets_meta.get(t["key"]) or {}).get("name", t.get("label")),
                            "party": t.get("party_code"),
                            "color": (tickets_meta.get(t["key"]) or {}).get("color")
                            or colors.get(t.get("party_code")),
                            "prob_win": t.get("prob_majority"),
                            "prob_plurality": t.get("prob_plurality"),
                            "ev": t.get("ev"),
                            "ev_histogram": t.get("ev_histogram"),
                            "pv_share": t.get("pv_share"),
                            "prob_pv_plurality": t.get("prob_pv_plurality"),
                            "pv_histogram": t.get("pv_histogram"),
                            "expected_pv_share": t.get("expected_pv_share"),
                        }
                        for t in pres.get("tickets") or []
                    ),
                    key=lambda t: -(t["prob_win"] or 0.0),
                ),
                "provinces": [
                    {
                        "code": code,
                        "name": pmap.get(code, {}).get("name"),
                        "ev": p.get("electoral_votes"),
                        "win": p.get("win"),
                        "favourite": max(p["win"], key=p["win"].get) if p.get("win") else None,
                        "share_mean": p.get("share_mean"),
                        "tipping_point": p.get("tipping_point"),
                    }
                    for code, p in sorted(
                        (pres.get("provinces") or {}).items(),
                        key=lambda kv: pmap.get(kv[0], {}).get("sort_order", 99),
                    )
                ],
                "tipping_point": pres.get("tipping_point"),
                "tipping_point_by_ticket": pres.get("tipping_point_by_ticket"),
                "combinations": (pres.get("combinations") or [])[:TOP_COMBINATIONS],
            }
            if include_municipalities:
                president["municipalities"] = pres.get("municipalities")
        out = {
            "data_category": SIMULATED,
            "disclaimer": res.get("disclaimer", DISCLAIMER),
            "run": {
                "id": run.id,
                "election_id": run.election_id,
                "status": run.status,
                "seed": int(run.seed),
                "n_simulations": run.n_simulations,
                "config_hash": run.config_hash,
                "started_at": iso(run.started_at),
                "finished_at": iso(run.finished_at),
                "duration_s": rnd(run.duration_s, 3),
                "code_version": run.code_version,
                "polls": (res.get("metadata") or {}).get("polls"),
                "race_types": (res.get("metadata") or {}).get("race_types"),
                "model_fingerprint": (res.get("metadata") or {}).get("model_fingerprint"),
            },
            "election": None if ref is None else ref.brief(),
            "constitution": {
                "electoral_votes": get_constitution().electoral_votes,
                "presidential_majority": get_constitution().presidential_majority,
            },
            "turnout": res.get("turnout"),
            "president": president,
            "house": _chamber(res.get("house"), colors, races=True),
            "senate": _chamber(res.get("senate"), colors, races=True),
            "governors": _chamber(res.get("governors"), colors, races=True),
        }
        if include_races:
            out["races"] = res.get("races")
        return out

    return cached(session, "forecast", (run.id, include_races, include_municipalities), build)


def latest_view(
    session: Session, ref: ElectionRef, *, include_races: bool = False, include_municipalities: bool = False
) -> dict[str, Any]:
    """``GET /api/forecast/{id}/latest`` — the newest completed forecast of an election."""
    run_id = session.scalar(
        select(SimulationRun.id)
        .where(
            SimulationRun.kind == RUN_KIND,
            SimulationRun.election_id == ref.id,
            SimulationRun.status == "completed",
        )
        .order_by(SimulationRun.id.desc())
        .limit(1)
    )
    if run_id is None:
        raise NotFoundError(f"election {ref.id} has no forecast yet (POST /api/forecast/{ref.id}/run)")
    return forecast_view(
        session, int(run_id), include_races=include_races, include_municipalities=include_municipalities
    )


def runs(session: Session, ref: ElectionRef) -> dict[str, Any]:
    """``GET /api/forecast/{id}/runs`` — the election's forecast runs (metadata only)."""
    return ref.envelope(runs=list_forecasts(session, ref.id))
