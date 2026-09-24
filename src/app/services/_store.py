"""Persistence of SIMULATED results and reporting timelines (private to :mod:`app.services`).

Results are stored at every level (unit → municipality → [district] → province → national) from
:func:`app.elections.tabulation.aggregate_levels`; each race's levels must reconcile exactly before
anything is written.  :func:`check_levels` verifies exactly the invariants of
:func:`app.elections.tabulation.reconcile` with vectorised NumPy (``reconcile`` builds several
pandas group-bys per level, ≈75 ms per race, which dominated the persistence of 800-race
elections); ``strict_reconcile=True`` additionally runs the reference implementation.

``geo_key`` convention: ``U:<geo_unit_id>``, ``M:<municipality_id>``, ``D:<house_district_id>``,
``P:<province_id>``, ``N`` (national).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import bindparam, delete, select, update
from sqlalchemy.orm import Session

from app.core.constitution import RaceType
from app.core.errors import ElectionError
from app.core.logging import get_logger
from app.elections.tabulation import aggregate_levels, reconcile
from app.elections.types import RaceSpec, RaceVotes
from app.models import (
    ElectionResult,
    NightSession,
    RaceCall,
    ReportingEvent,
    ReportingEventUnit,
    TurnoutResult,
)
from app.reporting.timeline import Timeline
from app.services._common import CHUNK_ROWS, bulk_insert, table_of
from app.services.runtime import DISTRICT_LEVEL_TYPES, ElectionInputs

log = get_logger(__name__)


# =========================================================================== aggregation
_COUNTS = ("valid_votes", "ballots_cast", "eligible", "blank", "invalid")
_PARENT = (
    ("unit", "municipality", "municipality_code"),
    ("municipality", "province", "province_code"),
    ("unit", "district", "district_code"),
    ("district", "province", "province_code"),
    ("province", "national", None),
)


def _level_arrays(df: pd.DataFrame, n_lines: int) -> tuple[np.ndarray, dict[str, np.ndarray], pd.DataFrame]:
    """(votes (G, L), per-geo counts, one row per geo) of an ``aggregate_levels`` level frame."""
    G = len(df) // n_lines if n_lines else 0
    votes = df["votes"].to_numpy(dtype=np.int64).reshape(G, n_lines)
    geos = df.iloc[::n_lines] if n_lines else df.iloc[:0]
    counts = {c: geos[c].to_numpy(dtype=np.int64) for c in _COUNTS}
    return votes, counts, geos


def check_levels(levels: Mapping[str, pd.DataFrame], n_lines: int) -> list[str]:
    """Problems with the exact reconciliation of one race's levels (empty = OK).

    Same invariants as :func:`app.elections.tabulation.reconcile`: per geography, line votes sum
    to the valid votes, valid + blank + invalid = ballots cast ≤ eligible, no negative count; and
    every level sums exactly (votes per line and every count) to its parent level: unit →
    municipality → province → national, unit → district → province.
    """
    problems: list[str] = []
    for name in ("unit", "municipality", "province", "national"):
        if name not in levels:
            problems.append(f"missing level '{name}'")
    if problems:
        return problems
    arrays = {name: _level_arrays(df, n_lines) for name, df in levels.items()}
    for name, (votes, counts, _geos) in arrays.items():
        if (votes < 0).any() or any((v < 0).any() for v in counts.values()):
            problems.append(f"{name}: negative counts")
        if not np.array_equal(votes.sum(axis=1), counts["valid_votes"]):
            problems.append(f"{name}: line votes do not sum to valid_votes")
        if not np.array_equal(
            counts["valid_votes"] + counts["blank"] + counts["invalid"], counts["ballots_cast"]
        ):
            problems.append(f"{name}: valid + blank + invalid != ballots_cast")
        if (counts["ballots_cast"] > counts["eligible"]).any():
            problems.append(f"{name}: ballots_cast exceeds eligible")
    for child, parent, key in _PARENT:
        if child not in arrays or parent not in arrays:
            continue
        c_votes, c_counts, c_geos = arrays[child]
        p_votes, p_counts, p_geos = arrays[parent]
        if key is None:
            idx = np.zeros(len(c_geos), dtype=np.int64)
        else:
            idx = pd.Index(p_geos["geo_code"].to_numpy()).get_indexer(c_geos[key].to_numpy())
            if (idx < 0).any():
                problems.append(f"{child}→{parent}: child geographies without a parent row")
                continue
        agg_votes = np.zeros_like(p_votes)
        np.add.at(agg_votes, idx, c_votes)
        if len(np.unique(idx)) != len(p_geos):
            problems.append(f"{child}→{parent}: parent geographies without child rows")
        if not np.array_equal(agg_votes, p_votes):
            problems.append(f"{child}→{parent}: votes do not sum exactly")
        for c in _COUNTS:
            agg = np.zeros_like(p_counts[c])
            np.add.at(agg, idx, c_counts[c])
            if not np.array_equal(agg, p_counts[c]):
                problems.append(f"{child}→{parent}: {c} does not sum exactly")
    return problems


def race_levels(
    inputs: ElectionInputs, code: str, rv: RaceVotes, *, strict_reconcile: bool = False
) -> dict[str, pd.DataFrame]:
    """All levels of one race with exact reconciliation (raises :class:`ElectionError` otherwise)."""
    spec = inputs.races[code]
    with_district = RaceType(spec.race_type) in DISTRICT_LEVEL_TYPES and len(inputs.district_codes) > 0
    levels = aggregate_levels(
        rv,
        inputs.frame,
        unit_district=inputs.unit_district if with_district else None,
        district_codes=inputs.district_codes if with_district else None,
    )
    problems = check_levels(levels, len(rv.line_keys))
    if strict_reconcile:
        problems += reconcile(levels)
    if problems:
        raise ElectionError(f"{code}: results do not reconcile: {problems[:3]}")
    return levels


class _Ids:
    """Code → database id lookups for one election's geography."""

    def __init__(self, inputs: ElectionInputs) -> None:
        f = inputs.frame
        if f.unit_ids is None or f.muni_ids is None or f.province_ids is None:
            raise ElectionError("the frame carries no database ids")
        self.frame = f
        self.muni = dict(zip(f.muni_codes, f.muni_ids.tolist(), strict=True))
        self.prov = dict(zip(f.province_codes, f.province_ids.tolist(), strict=True))
        self.dist = dict(zip(inputs.district_codes, inputs.district_ids, strict=True))
        self.unit_district_id = np.full(f.n_units, 0, dtype=np.int64)
        if inputs.district_ids:
            dids = np.asarray(inputs.district_ids, dtype=np.int64)
            ok = inputs.unit_district >= 0
            self.unit_district_id[ok] = dids[inputs.unit_district[ok]]


def _geo_columns(
    ids: _Ids, level: str, geos: pd.DataFrame, unit_index: np.ndarray | None, with_district: bool
) -> dict[str, list[Any]]:
    """Per-geo id columns (``geo_key``, ``geo_unit_id``, ``municipality_id``, ``district_id``,
    ``province_id``) of a level frame reduced to one row per geography."""
    n = len(geos)
    none = [None] * n
    f = ids.frame
    if level == "unit":
        u = np.asarray(unit_index, dtype=np.int64)
        uid = f.unit_ids[u]
        return {
            "geo_key": [f"U:{i}" for i in uid.tolist()],
            "geo_unit_id": uid.tolist(),
            "municipality_id": f.muni_ids[f.unit_muni[u]].tolist(),
            "district_id": ids.unit_district_id[u].tolist() if with_district else none,
            "province_id": f.province_ids[f.unit_province[u]].tolist(),
        }
    if level == "municipality":
        mid = [ids.muni[c] for c in geos["geo_code"]]
        return {
            "geo_key": [f"M:{i}" for i in mid],
            "geo_unit_id": none,
            "municipality_id": mid,
            "district_id": none,
            "province_id": [ids.prov[c] for c in geos["province_code"]],
        }
    if level == "district":
        did = [ids.dist[c] for c in geos["geo_code"]]
        return {
            "geo_key": [f"D:{i}" for i in did],
            "geo_unit_id": none,
            "municipality_id": none,
            "district_id": did,
            "province_id": [ids.prov[c] for c in geos["province_code"]],
        }
    if level == "province":
        pid = [ids.prov[c] for c in geos["geo_code"]]
        return {
            "geo_key": [f"P:{i}" for i in pid],
            "geo_unit_id": none,
            "municipality_id": none,
            "district_id": none,
            "province_id": pid,
        }
    return {
        "geo_key": ["N"] * n,
        "geo_unit_id": none,
        "municipality_id": none,
        "district_id": none,
        "province_id": none,
    }


def level_rows(
    inputs: ElectionInputs,
    ids: _Ids,
    election_id: int,
    code: str,
    rv: RaceVotes,
    levels: Mapping[str, pd.DataFrame],
    *,
    include: Sequence[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``election_result`` and ``turnout_result`` rows of one race for the given levels."""
    race_id = inputs.race_ids[code]
    line_ids = np.asarray([inputs.line_ids[code][k] for k in rv.line_keys], dtype=np.int64)
    L = len(rv.line_keys)
    with_district = "district" in levels
    res_rows: list[dict[str, Any]] = []
    turn_rows: list[dict[str, Any]] = []
    for level in ("unit", "municipality", "district", "province", "national"):
        if level not in levels or (include is not None and level not in include):
            continue
        df = levels[level]
        if df.empty:
            continue
        geos = df.iloc[::L].reset_index(drop=True) if L else df
        cols = _geo_columns(ids, level, geos, rv.unit_index if level == "unit" else None, with_district)
        G = len(geos)
        rep = {k: np.repeat(np.asarray(v, dtype=object), L).tolist() for k, v in cols.items()}
        votes = df["votes"].to_numpy(dtype=np.int64).tolist()
        share = df["share"].to_numpy(dtype=float).tolist()
        bids = np.tile(line_ids, G).tolist()
        res_rows.extend(
            {
                "race_id": race_id,
                "ballot_candidate_id": b,
                "level": level,
                "geo_key": gk,
                "geo_unit_id": gu,
                "municipality_id": mu,
                "district_id": di,
                "province_id": pr,
                "votes": v,
                "share": s,
            }
            for b, gk, gu, mu, di, pr, v, s in zip(
                bids,
                rep["geo_key"],
                rep["geo_unit_id"],
                rep["municipality_id"],
                rep["district_id"],
                rep["province_id"],
                votes,
                share,
                strict=True,
            )
        )
        eligible = geos["eligible"].to_numpy(dtype=np.int64)
        cast = geos["ballots_cast"].to_numpy(dtype=np.int64)
        pct = np.where(eligible > 0, 100.0 * cast / np.maximum(eligible, 1), 0.0)
        turn_rows.extend(
            {
                "election_id": election_id,
                "race_id": race_id,
                "level": level,
                "geo_key": gk,
                "geo_unit_id": gu,
                "municipality_id": mu,
                "district_id": di,
                "province_id": pr,
                "eligible_voters": el,
                "ballots_cast": bc,
                "valid_votes": va,
                "blank_votes": bl,
                "invalid_votes": iv,
                "turnout_pct": tp,
            }
            for gk, gu, mu, di, pr, el, bc, va, bl, iv, tp in zip(
                cols["geo_key"],
                cols["geo_unit_id"],
                cols["municipality_id"],
                cols["district_id"],
                cols["province_id"],
                eligible.tolist(),
                cast.tolist(),
                geos["valid_votes"].to_numpy(dtype=np.int64).tolist(),
                geos["blank"].to_numpy(dtype=np.int64).tolist(),
                geos["invalid"].to_numpy(dtype=np.int64).tolist(),
                pct.tolist(),
                strict=True,
            )
        )
    return res_rows, turn_rows


def store_results(session: Session, inputs: ElectionInputs, races: Mapping[str, RaceVotes]) -> dict[str, int]:
    """Persist every race at every level (bulk inserts, chunked).  Returns row counts."""
    ids = _Ids(inputs)
    n_res = n_turn = 0
    pending_res: list[dict[str, Any]] = []
    pending_turn: list[dict[str, Any]] = []
    for code in inputs.races:
        if code not in races:
            raise ElectionError(f"no simulated votes for race {code}")
        rv = races[code]
        levels = race_levels(inputs, code, rv)
        res, turn = level_rows(inputs, ids, inputs.election_id, code, rv, levels)
        pending_res += res
        pending_turn += turn
        if len(pending_res) >= 4 * CHUNK_ROWS:
            n_res += bulk_insert(session, ElectionResult, pending_res)
            pending_res = []
        if len(pending_turn) >= 4 * CHUNK_ROWS:
            n_turn += bulk_insert(session, TurnoutResult, pending_turn)
            pending_turn = []
    n_res += bulk_insert(session, ElectionResult, pending_res)
    n_turn += bulk_insert(session, TurnoutResult, pending_turn)
    session.flush()
    return {"election_result": n_res, "turnout_result": n_turn}


def clear_simulation(session: Session, election_id: int, race_ids: Sequence[int]) -> None:
    """Remove a previous (unreported) simulation of an election: results, timeline, calls and the
    night session state."""
    ids = list(race_ids)
    for i in range(0, len(ids), 500):
        part = ids[i : i + 500]
        session.execute(delete(ElectionResult).where(ElectionResult.race_id.in_(part)))
    session.execute(delete(TurnoutResult).where(TurnoutResult.election_id == election_id))
    ev_ids = select(ReportingEvent.id).where(ReportingEvent.election_id == election_id)
    session.execute(delete(ReportingEventUnit).where(ReportingEventUnit.event_id.in_(ev_ids)))
    session.execute(delete(ReportingEvent).where(ReportingEvent.election_id == election_id))
    session.execute(delete(RaceCall).where(RaceCall.election_id == election_id))
    session.execute(delete(NightSession).where(NightSession.election_id == election_id))
    session.flush()


# =========================================================================== recount updates
def replace_race_results(
    session: Session, inputs: ElectionInputs, code: str, before: RaceVotes, after: RaceVotes
) -> dict[str, int]:
    """Apply corrected counts of one race to the stored rows: changed unit rows are updated in
    place, every aggregate level is recomputed from the corrected units (exact reconciliation)
    and replaced.  Never re-simulates anything."""
    if not np.array_equal(before.unit_index, after.unit_index) or before.line_keys != after.line_keys:
        raise ElectionError(f"{code}: corrected counts do not match the stored race")
    ids = _Ids(inputs)
    race_id = inputs.race_ids[code]
    line_ids = [inputs.line_ids[code][k] for k in after.line_keys]
    changed = np.flatnonzero(
        (before.votes != after.votes).any(axis=1)
        | (before.ballots_cast != after.ballots_cast)
        | (before.blank != after.blank)
        | (before.invalid != after.invalid)
    )
    f = inputs.frame
    res_updates = []
    turn_updates = []
    for r in changed.tolist():
        u = int(after.unit_index[r])
        gk = f"U:{int(f.unit_ids[u])}"
        valid = int(after.votes[r].sum())
        for j, bid in enumerate(line_ids):
            v = int(after.votes[r, j])
            res_updates.append(
                {"r": race_id, "b": bid, "g": gk, "v": v, "s": v / valid if valid > 0 else 0.0}
            )
        el = int(after.eligible[r])
        cast = int(after.ballots_cast[r])
        turn_updates.append(
            {
                "r": race_id,
                "g": gk,
                "c": cast,
                "va": valid,
                "bl": int(after.blank[r]),
                "iv": int(after.invalid[r]),
                "tp": 100.0 * cast / el if el > 0 else 0.0,
            }
        )
    er, tr = table_of(ElectionResult), table_of(TurnoutResult)
    if res_updates:
        session.execute(
            update(er)
            .where(
                er.c.race_id == bindparam("r"),
                er.c.ballot_candidate_id == bindparam("b"),
                er.c.geo_key == bindparam("g"),
            )
            .values(votes=bindparam("v"), share=bindparam("s")),
            res_updates,
        )
    if turn_updates:
        session.execute(
            update(tr)
            .where(tr.c.race_id == bindparam("r"), tr.c.geo_key == bindparam("g"))
            .values(
                ballots_cast=bindparam("c"),
                valid_votes=bindparam("va"),
                blank_votes=bindparam("bl"),
                invalid_votes=bindparam("iv"),
                turnout_pct=bindparam("tp"),
            ),
            turn_updates,
        )
    levels = race_levels(inputs, code, after)
    session.execute(
        delete(ElectionResult).where(ElectionResult.race_id == race_id, ElectionResult.level != "unit")
    )
    session.execute(
        delete(TurnoutResult).where(TurnoutResult.race_id == race_id, TurnoutResult.level != "unit")
    )
    aggregate = [lv for lv in levels if lv != "unit"]
    res, turn = level_rows(inputs, ids, inputs.election_id, code, after, levels, include=aggregate)
    bulk_insert(session, ElectionResult, res)
    bulk_insert(session, TurnoutResult, turn)
    session.flush()
    return {"units_changed": len(changed), "aggregate_rows": len(res)}


def stitch_parent(parent: RaceSpec, children: Sequence[RaceVotes]) -> RaceVotes:
    """The national ``PRES`` race as the union of its province contests (line keys aligned)."""
    keys = parent.line_keys
    col = {k: i for i, k in enumerate(keys)}
    units = np.concatenate([c.unit_index for c in children])
    order = np.argsort(units, kind="stable")
    n = len(units)
    votes = np.zeros((n, len(keys)), dtype=np.int64)
    row = 0
    for c in children:
        k = len(c.unit_index)
        for j, lk in enumerate(c.line_keys):
            if lk in col:
                votes[row : row + k, col[lk]] = c.votes[:, j]
            elif c.votes[:, j].any():
                raise ElectionError(f"{parent.key}: line {lk} of {c.race_key} is not on the national ballot")
        row += k

    def cat(name: str) -> np.ndarray:
        return np.concatenate([getattr(c, name) for c in children])[order]

    return RaceVotes(
        race_key=parent.key,
        line_keys=list(keys),
        unit_index=units[order],
        votes=votes[order],
        ballots_cast=cat("ballots_cast"),
        blank=cat("blank"),
        invalid=cat("invalid"),
        eligible=cat("eligible"),
    )


# =========================================================================== timeline
def store_timeline(session: Session, inputs: ElectionInputs, tl: Timeline) -> dict[str, Any]:
    """Persist ``reporting_event`` + ``reporting_event_unit`` rows; returns the timeline metadata
    needed to rebuild it exactly (stored in the ``election`` simulation run)."""
    f = inputs.frame
    ids = _Ids(inputs)
    ev_rows = tl.to_event_rows(f)
    rows = [
        {
            "election_id": inputs.election_id,
            "seq": r["seq"],
            "sim_time_s": r["sim_time_s"],
            "timestamp": r["timestamp"].replace(tzinfo=None),
            "municipality_id": ids.muni[r["municipality_code"]],
            "province_id": ids.prov[r["province_code"]],
            "kind": r["kind"],
            "ballots_in_batch": r["ballots_in_batch"],
            "municipality_fraction_after": r["municipality_fraction_after"],
            "description": str(r["description"])[:200],
        }
        for r in ev_rows
    ]
    bulk_insert(session, ReportingEvent, rows)
    session.flush()
    seq_to_id = dict(
        session.execute(
            select(ReportingEvent.seq, ReportingEvent.id).where(
                ReportingEvent.election_id == inputs.election_id
            )
        )
        .tuples()
        .all()
    )
    ev_ids = np.asarray([seq_to_id[s] for s in range(1, tl.n_events + 1)], dtype=np.int64)
    per_event = np.diff(tl.unit_ptr)
    event_col = np.repeat(ev_ids, per_event).tolist()
    unit_col = f.unit_ids[tl.units].tolist()
    frac = tl.increments.tolist()
    bulk_insert(
        session,
        ReportingEventUnit,
        [
            {"event_id": e, "geo_unit_id": u, "fraction": x}
            for e, u, x in zip(event_col, unit_col, frac, strict=True)
        ],
    )
    session.flush()
    return {
        "seed": int(tl.seed),
        "reference_close": tl.reference_close.isoformat(),
        "timezone": tl.timezone,
        "config_fingerprint": tl.config_fingerprint,
        "checkpoint_every": int(tl.checkpoint_every),
        "events": int(tl.n_events),
        "unit_rows": len(unit_col),
        "end_clock": tl.local_clock(tl.end_time_s),
        "muni_close_offset_s": [float(x) for x in tl.muni_close_offset_s],
    }
