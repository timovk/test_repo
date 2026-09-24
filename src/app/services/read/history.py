"""Cross-election history read models (reported elections only), computed with
:mod:`app.analytics` over the standard results frame: summaries, lineage-aware comparisons with
swings and flips, records (closest races, landslides, EC/PV divergence) and series per
municipality / province / district.  Everything is SIMULATED data about FICTIONAL races."""

from __future__ import annotations

from itertools import pairwise
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.history import (
    closest_races,
    compare_elections,
    district_history,
    ec_pv_divergence,
    largest_landslides,
    remap_lineage,
)
from app.analytics.metrics import flip_summary, flips, national_swing
from app.analytics.results import INDEPENDENT_KEY, ResultsFrameError
from app.core.constitution import RaceType
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models import Election, Race
from app.services import elections as election_service
from app.services.read._base import (
    SIMULATED,
    ElectionRef,
    cached,
    reported_refs,
    require_reported,
    resolve_election,
    rnd,
)
from app.services.read._races import FAMILIES, party_key
from app.services.read.elections import (
    family_code,
    geo_party_table,
    lineage_between,
    president,
    previous_with,
    stored_frame,
)
from app.services.results import results_frame

log = get_logger(__name__)

LEVELS: tuple[str, ...] = ("municipality", "district", "province", "national")


def _types(family: str) -> tuple[RaceType, ...]:
    """Race types analysed for a family (the national PRES race stands for the presidency: it
    is stored at every level, so pooling its province contests would count ballots twice)."""
    return (RaceType.PRESIDENT,) if family == "PRES" else FAMILIES[family]


def _state_key(session: Session) -> tuple:
    return tuple(r.cache_key() for r in reported_refs(session))


def party_label(p: Any) -> str:
    return party_key(
        None if p is None or p == INDEPENDENT_KEY or (isinstance(p, float) and pd.isna(p)) else str(p)
    )


#: Columns rendered as integers by :func:`_records`.
INT_COLUMNS: frozenset[str] = frozenset(
    {
        "rank",
        "election_id",
        "year",
        "margin_votes",
        "valid_votes",
        "votes",
        "votes_prev",
        "votes_curr",
        "n_obs",
        "seats",
        "seats_total",
        "electoral_votes",
        "popular_votes",
        "provinces_won",
        "total_ev",
        "majority",
        "ev_leader_votes",
        "n_contests",
        "wins_prev",
        "wins_curr",
        "gains",
        "losses",
        "holds",
        "net",
    }
)


def json_value(x: Any, nd: int = 4, *, as_int: bool = False) -> Any:
    """JSON-ready scalar: NaN / NA → None, NumPy → Python, floats rounded to ``nd``."""
    if x is None:
        return None
    if isinstance(x, bool | np.bool_):
        return bool(x)
    if isinstance(x, int | np.integer):
        return int(x)
    if isinstance(x, float | np.floating):
        f = float(x)
        if not np.isfinite(f):
            return None
        return round(f) if as_int else round(f, nd)
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        pass
    return x.item() if hasattr(x, "item") else x


def records_of(df: pd.DataFrame, nd: int = 4) -> list[dict[str, Any]]:
    return [
        {k: json_value(v, nd, as_int=k in INT_COLUMNS) for k, v in row.items()}
        for row in df.to_dict(orient="records")
    ]


# =========================================================================== summary
def summary(session: Session) -> dict[str, Any]:
    """``GET /api/history/summary`` — per reported election: President (winner, EV, popular
    vote), House and Senate composition and control, governors, turnout, flips."""

    def build() -> dict[str, Any]:
        rows = []
        for ref in reported_refs(session):
            res = election_service.election_summary(session, ref.id).get("results") or {}
            pres = res.get("president") or None
            pv = None
            if pres:
                page = president(session, ref)
                w = page.get("winner") or {}
                t = next((t for t in page["tickets"] if t["key"] == w.get("key")), None)
                pv = {
                    "winner_pct": None if t is None else t["pct"],
                    "margin_pp": (page.get("popular_vote") or {}).get("margin_pp"),
                    "leader": (page.get("popular_vote") or {}).get("leader"),
                }
            rows.append(
                {
                    "election": ref.brief(),
                    "president": None
                    if not pres
                    else {
                        "winner": pres.get("winner"),
                        "winner_name": pres.get("winner_name"),
                        "party": pres.get("winner_party"),
                        "electoral_votes": pres.get("electoral_votes"),
                        "decided_by": pres.get("decided_by"),
                        "contingent": pres.get("contingent"),
                        "popular_vote": pv,
                    },
                    "house": res.get("house"),
                    "senate": res.get("senate"),
                    "governors": res.get("governors"),
                    "legislature_seats": res.get("legislature_seats"),
                    "turnout_pct": rnd(res.get("turnout_pct"), 3),
                    "flips": res.get("flips"),
                }
            )
        return {"data_category": SIMULATED, "count": len(rows), "elections": rows}

    return cached(session, "history-summary", _state_key(session), build)


# =========================================================================== compare
def _default_pair(session: Session, types: tuple[RaceType, ...]) -> tuple[ElectionRef, ElectionRef]:
    reps = [r for r in reported_refs(session) if has_types(session, r, types)]
    if len(reps) < 2:
        raise NotFoundError("fewer than two reported elections hold these races")
    return reps[-2], reps[-1]


def has_types(session: Session, ref: ElectionRef, types: tuple[RaceType, ...]) -> bool:
    return (
        session.scalar(
            select(Race.id)
            .where(Race.election_id == ref.id, Race.race_type.in_([t.value for t in types]))
            .limit(1)
        )
        is not None
    )


def compare(
    session: Session,
    a: str | int | None = None,
    b: str | int | None = None,
    race: str | None = None,
    level: str = "municipality",
) -> dict[str, Any]:
    """``GET /api/history/compare?a=&b=&race=&level=`` — lineage-aware swing table between two
    reported elections (``a`` earlier, ``b`` later; default: the last two holding the family):
    national swing, per-geo winners, flip status, turnout change and party swings, and the flip
    summary per party (:func:`app.analytics.history.compare_elections`)."""
    family = family_code(race)
    if level not in LEVELS:
        raise ValidationError(f"level must be one of {list(LEVELS)}")
    types = _types(family)
    if a is None and b is None:
        ra, rb = _default_pair(session, types)
    elif a is None or b is None:
        rb = resolve_election(session, b if b is not None else a)  # type: ignore[arg-type]
        prev = previous_with(session, rb, types)
        if prev is None:
            raise NotFoundError(f"no reported election with {family} races before election {rb.id}")
        ra = prev
    else:
        ra, rb = resolve_election(session, a), resolve_election(session, b)
    require_reported(ra)
    require_reported(rb)
    if ra.id == rb.id:
        raise ValidationError(f"compare needs two different elections (a = b = {ra.id})")
    if ra.election_date > rb.election_date:
        ra, rb = rb, ra

    def build() -> dict[str, Any]:
        p = stored_frame(session, ra, (level,), types)
        c = stored_frame(session, rb, (level,), types)
        if p.empty or c.empty:
            raise NotFoundError(f"no {family} results at level {level} in one of the elections")
        lineage = lineage_between(session, ra, rb)
        try:
            table = compare_elections(p, c, level, race_type=types[0], lineage=lineage, by="family")
            pn = stored_frame(session, ra, ("national",), types)
            cn = stored_frame(session, rb, ("national",), types)
            nat = national_swing(pn, cn, race_type=types[0])
            prev_l = (
                remap_lineage(p, lineage, level=level)
                if lineage is not None and level == "municipality"
                else p
            )
            fl = flip_summary(flips(prev_l, c, level=level, by="family"))
        except ResultsFrameError as exc:
            raise ValidationError(f"cannot compare these elections: {exc}") from exc
        pa = geo_party_table(pn).get("NL", {})
        pb = geo_party_table(cn).get("NL", {})
        va, vb = pa.get("votes", {}), pb.get("votes", {})
        ta, tb = sum(va.values()) or 1, sum(vb.values()) or 1
        national = [
            {
                "party": party_label(party),
                "share_a": rnd(va.get(party_label(party), 0) / ta, 6),
                "share_b": rnd(vb.get(party_label(party), 0) / tb, 6),
                "swing_pp": rnd(val, 4),
            }
            for party, val in nat.items()
        ]
        rows: dict[str, dict[str, Any]] = {}
        for r in table.itertuples(index=False):
            g = rows.setdefault(
                str(r.geo_code),
                {
                    "geo_code": str(r.geo_code),
                    "geo_name": json_value(r.geo_name),
                    "province_code": json_value(r.province_code),
                    "winner_a": json_value(r.winner_prev),
                    "winner_b": json_value(r.winner_curr),
                    "flip_status": json_value(r.flip_status),
                    "turnout_a": rnd(r.turnout_prev, 6),
                    "turnout_b": rnd(r.turnout_curr, 6),
                    "turnout_change_pp": rnd(r.turnout_change_pp, 4),
                    "swing": {},
                    "share_b": {},
                    "status": {},
                },
            )
            party = party_label(r.party)
            g["swing"][party] = rnd(r.swing_pp, 4)
            g["share_b"][party] = rnd(r.share_curr, 6)
            g["status"][party] = json_value(r.status)
        counts: dict[str, int] = {}
        for g in rows.values():
            k = g["flip_status"] or "undecided"
            counts[k] = counts.get(k, 0) + 1
        return {
            "data_category": SIMULATED,
            "a": ra.brief(),
            "b": rb.brief(),
            "race": family,
            "level": level,
            "lineage_applied": lineage is not None,
            "national": national,
            "flip_counts": counts,
            "flips_summary": [
                {"party": party_label(x["party"]), **{k: json_value(v) for k, v in x.items() if k != "party"}}
                for x in fl.to_dict(orient="records")
            ],
            "rows": sorted(rows.values(), key=lambda g: g["geo_code"]),
        }

    return cached(session, "compare", (ra.cache_key(), rb.cache_key(), family, level), build)


# =========================================================================== records
def _records_frame(session: Session, race_types: list[RaceType] | None) -> pd.DataFrame:
    """Contest-level rows of every reported election (jurisdiction levels only)."""
    refs = reported_refs(session)
    if not refs:
        return pd.DataFrame()
    ids = [r.id for r in refs]
    wanted = set(race_types) if race_types else set(RaceType)

    def build() -> pd.DataFrame:
        local = [t for t in (RaceType.MAYOR, RaceType.MUNICIPAL_COUNCIL) if t in wanted]
        other = [t for t in wanted if t not in local]
        parts = []
        if other:
            parts.append(
                results_frame(session, ids, levels=["district", "province", "national"], race_types=other)
            )
        if local:
            parts.append(results_frame(session, ids, levels=["municipality"], race_types=local))
        parts = [p for p in parts if not p.empty]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    return cached(
        session,
        "records-frame",
        (_state_key(session), tuple(sorted(t.value for t in wanted))),
        build,
        big=True,
    )


def _race_types_arg(race_type: str | None) -> list[RaceType] | None:
    if race_type is None:
        return None
    try:
        return [RaceType(race_type.upper())]
    except ValueError:
        raise ValidationError(
            f"unknown race_type {race_type!r}; expected one of {[t.value for t in RaceType]}"
        ) from None


def records(session: Session, kind: str, n: int = 10, race_type: str | None = None) -> dict[str, Any]:
    """``GET /api/history/closest`` / ``/landslides`` — the ``n`` closest races or largest
    landslides of all reported elections (at each race's jurisdiction level)."""
    if n < 1 or n > 500:
        raise ValidationError("n must be between 1 and 500")
    types = _race_types_arg(race_type)
    frame = _records_frame(session, types)
    if frame.empty:
        return {"data_category": SIMULATED, "kind": kind, "count": 0, "races": []}
    fn = closest_races if kind == "closest" else largest_landslides
    df = fn(frame, n=n, race_types=[t.value for t in types] if types else None)
    rows = records_of(df)
    for r in rows:
        r["winner_party"] = party_label(r.get("winner_party")) if r.get("winner_candidate") else None
        r["runner_up_party"] = party_label(r.get("runner_up_party")) if r.get("runner_up_candidate") else None
    return {"data_category": SIMULATED, "kind": kind, "count": len(rows), "races": rows}


def divergence(session: Session) -> dict[str, Any]:
    """``GET /api/history/divergence`` — per reported presidential election: popular-vote leader
    vs Electoral College leader (descriptive)."""
    long = []
    for ref in reported_refs(session):
        if not has_types(session, ref, (RaceType.PRESIDENT,)):
            continue
        page = president(session, ref)
        for t in page["tickets"]:
            long.append(
                {
                    "election_id": ref.id,
                    "year": ref.year,
                    "key": t["key"],
                    "popular_votes": int(t["votes"] or 0),
                    "electoral_votes": int(t["electoral_votes"] or 0),
                }
            )
    if not long:
        return {"data_category": SIMULATED, "count": 0, "elections": []}
    df = ec_pv_divergence(pd.DataFrame(long))
    return {"data_category": SIMULATED, "count": len(df), "elections": records_of(df, 6)}


# =========================================================================== series
def _series_refs(session: Session, types: tuple[RaceType, ...]) -> list[ElectionRef]:
    return [r for r in reported_refs(session) if has_types(session, r, types)]


def geo_series(session: Session, level: str, code: str, race: str | None = None) -> dict[str, Any]:
    """``GET /api/history/municipality/{code}`` / ``/province/{code}`` — per reported election of
    the family: party shares, winner, margin and turnout at the geo, plus the national share and
    lean (pp).  Municipal series are lineage-aware (older vintages remapped onto the current
    municipality codes)."""
    family = family_code(race)
    types = _types(family)
    code = code.upper()
    refs = _series_refs(session, types)

    def build() -> dict[str, Any]:
        points = []
        current_vintage = refs[-1].vintage_id if refs else None
        for ref in refs:
            frame = stored_frame(session, ref, (level,), types)
            if level == "municipality" and current_vintage is not None and ref.vintage_id != current_vintage:
                cur = next(r for r in refs if r.vintage_id == current_vintage)
                lineage = lineage_between(session, ref, cur)
                if lineage is not None and not lineage.empty:
                    frame = remap_lineage(frame, lineage, level="municipality")
            g = geo_party_table(frame).get(code)
            if g is None:
                continue
            nat = geo_party_table(stored_frame(session, ref, ("national",), types)).get("NL", {})
            nv = nat.get("votes", {})
            nt = sum(nv.values()) or 1
            tot = sum(g["votes"].values()) or 1
            shares = {p: round(100.0 * v / tot, 3) for p, v in g["votes"].items()}
            points.append(
                {
                    "election": ref.brief(),
                    "name": g["name"],
                    "winner": g["leader"],
                    "margin_pp": g["margin_pp"],
                    "turnout_pct": rnd(100.0 * g["ballots_cast"] / g["eligible"], 3)
                    if g["eligible"]
                    else None,
                    "valid_votes": g["valid_votes"],
                    "shares": shares,
                    "national_shares": {p: round(100.0 * v / nt, 3) for p, v in nv.items()},
                    "lean_pp": {p: round(shares.get(p, 0.0) - 100.0 * nv.get(p, 0) / nt, 3) for p in shares},
                }
            )
        for prev, cur in pairwise(points):
            cur["change_pp"] = {
                p: round(cur["shares"].get(p, 0.0) - prev["shares"].get(p, 0.0), 3)
                for p in sorted(set(cur["shares"]) | set(prev["shares"]))
            }
            cur["flipped"] = cur["winner"] != prev["winner"]
        if not points:
            raise NotFoundError(f"no reported {family} results for {level} {code}")
        return {
            "data_category": SIMULATED,
            "level": level,
            "code": code,
            "name": points[-1]["name"],
            "race": family,
            "points": points,
        }

    return cached(session, "geo-series", (_state_key(session), level, code, family), build)


def district_series(session: Session, code: str) -> dict[str, Any]:
    """``GET /api/history/district/{code}`` — the House race of a district code across reported
    elections (winner, margin, turnout, first / hold / flip).  After a redistricting the same code
    may cover a different territory."""
    code = code.upper().removeprefix("HOUSE-")
    refs = _series_refs(session, (RaceType.HOUSE,))

    def build() -> dict[str, Any]:
        frames = [stored_frame(session, r, ("district",), (RaceType.HOUSE,)) for r in refs]
        frames = [f for f in frames if not f.empty]
        if not frames:
            raise NotFoundError(f"no reported House results for district {code}")
        df = district_history(pd.concat(frames, ignore_index=True), code)
        if df.empty:
            raise NotFoundError(f"no reported House results for district {code}")
        rows = records_of(df)
        plans = {
            int(i): p
            for i, p in session.execute(
                select(Election.id, Election.district_plan_id).where(Election.id.in_([r.id for r in refs]))
            )
        }
        for r in rows:
            r["winner_party"] = party_label(r.get("winner_party")) if r.get("winner_candidate") else None
            r["district_plan_id"] = plans.get(int(r["election_id"]))
        return {
            "data_category": SIMULATED,
            "code": code,
            "note": "District codes are matched literally; a redistricting can change a code's territory.",
            "points": rows,
        }

    return cached(session, "district-series", (_state_key(session), code), build)


__all__ = [
    "compare",
    "district_series",
    "divergence",
    "geo_series",
    "records",
    "summary",
]
