"""Stored results as the **standard results frame** (docs/ARCHITECTURE.md §7) for analytics,
history and export.

One row per race × geography × ballot line with the columns of
:data:`app.analytics.results.RESULTS_COLUMNS`.  Only reported (FINAL / CERTIFIED) elections are
included unless ``include_hidden=True`` — the hidden-until-reported rule of the read layer.
Everything returned is SIMULATED data about FICTIONAL races.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.results import RESULTS_COLUMNS
from app.core.constitution import RaceType
from app.core.logging import Timer, get_logger
from app.elections.tabulation import NATIONAL_CODE, NATIONAL_NAME
from app.models import (
    BallotCandidate,
    Election,
    ElectionResult,
    GeoUnit,
    HouseDistrict,
    Municipality,
    Province,
    Race,
    TurnoutResult,
)
from app.services._common import REPORTED_STATUSES

log = get_logger(__name__)

DEFAULT_LEVELS: tuple[str, ...] = ("municipality", "province", "national", "district")
_LEVEL_ORDER = {"unit": 0, "municipality": 1, "district": 2, "province": 3, "national": 4}
_CHUNK = 400
_ID_DTYPES = {
    "geo_unit_id": "Int64",
    "municipality_id": "Int64",
    "district_id": "Int64",
    "province_id": "Int64",
}


def _empty() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype=object) for c in RESULTS_COLUMNS})


def _chunks(ids: Sequence[int]) -> Iterable[list[int]]:
    for i in range(0, len(ids), _CHUNK):
        yield list(ids[i : i + _CHUNK])


def results_frame(
    session: Session,
    election_ids: Iterable[int] | None = None,
    *,
    levels: Sequence[str] = DEFAULT_LEVELS,
    race_types: Iterable[str | RaceType] | None = None,
    include_hidden: bool = False,
) -> pd.DataFrame:
    """Results of the selected elections in the standard format.

    Args:
        election_ids: elections to include (default: every reported election).
        levels: geographic levels (``unit`` is large: one row per CBS neighbourhood × line).
        race_types: restrict to these :class:`RaceType` values.
        include_hidden: also include SIMULATED / LIVE elections (internal tools only).
    """
    levels = [str(lv) for lv in levels]
    unknown = sorted(set(levels) - set(_LEVEL_ORDER))
    if unknown:
        raise ValueError(f"unknown levels {unknown}")
    q = select(Election.id, Election.year, Election.status)
    if election_ids is not None:
        ids = [int(i) for i in election_ids]
        if not ids:
            return _empty()
        q = q.where(Election.id.in_(ids))
    if not include_hidden:
        q = q.where(Election.status.in_(list(REPORTED_STATUSES)))
    elections = {int(i): int(y) for i, y, _s in session.execute(q).all()}
    if not elections:
        return _empty()
    rq = select(Race.id, Race.election_id, Race.code, Race.race_type).where(
        Race.election_id.in_(list(elections))
    )
    if race_types is not None:
        rq = rq.where(Race.race_type.in_([RaceType(t).value for t in race_types]))
    races = pd.DataFrame(
        session.execute(rq).all(), columns=["race_id", "election_id", "race_code", "race_type"]
    )
    if races.empty:
        return _empty()
    race_ids = races["race_id"].astype(int).tolist()
    with Timer(log, f"results frame ({len(elections)} elections, {len(race_ids)} races)"):
        res_parts, turn_parts, line_parts = [], [], []
        for part in _chunks(race_ids):
            res_parts.append(
                pd.DataFrame(
                    session.execute(
                        select(
                            ElectionResult.race_id,
                            ElectionResult.ballot_candidate_id,
                            ElectionResult.level,
                            ElectionResult.geo_key,
                            ElectionResult.geo_unit_id,
                            ElectionResult.municipality_id,
                            ElectionResult.district_id,
                            ElectionResult.province_id,
                            ElectionResult.votes,
                            ElectionResult.share,
                        ).where(ElectionResult.race_id.in_(part), ElectionResult.level.in_(levels))
                    ).all(),
                    columns=[
                        "race_id",
                        "ballot_candidate_id",
                        "level",
                        "geo_key",
                        "geo_unit_id",
                        "municipality_id",
                        "district_id",
                        "province_id",
                        "votes",
                        "share",
                    ],
                ).astype(_ID_DTYPES)
            )
            turn_parts.append(
                pd.DataFrame(
                    session.execute(
                        select(
                            TurnoutResult.race_id,
                            TurnoutResult.geo_key,
                            TurnoutResult.eligible_voters,
                            TurnoutResult.ballots_cast,
                            TurnoutResult.valid_votes,
                        ).where(TurnoutResult.race_id.in_(part), TurnoutResult.level.in_(levels))
                    ).all(),
                    columns=["race_id", "geo_key", "eligible", "ballots_cast", "valid_votes"],
                )
            )
            line_parts.append(
                pd.DataFrame(
                    session.execute(
                        select(
                            BallotCandidate.id,
                            BallotCandidate.line_key,
                            BallotCandidate.ballot_name,
                            BallotCandidate.party_code_snapshot,
                        ).where(BallotCandidate.race_id.in_(part))
                    ).all(),
                    columns=["ballot_candidate_id", "line_key", "candidate", "party_code"],
                )
            )
        res_parts = [p for p in res_parts if not p.empty]
        if not res_parts:
            return _empty()
        res = pd.concat(res_parts, ignore_index=True)
        turn = pd.concat([p for p in turn_parts if not p.empty], ignore_index=True)
        lines = pd.concat([p for p in line_parts if not p.empty], ignore_index=True)
        df = res.merge(turn, on=["race_id", "geo_key"], how="left", validate="many_to_one")
        df = df.merge(lines, on="ballot_candidate_id", how="left", validate="many_to_one")
        df = df.merge(races, on="race_id", how="left", validate="many_to_one")
        df["year"] = df["election_id"].map(elections)
        df["line_key"] = df["line_key"].fillna(df["ballot_candidate_id"].astype(str))
        _attach_geography(session, df)
        # winner: unique plurality leader of each race × geography (no flag on exact ties / 0 votes)
        grp = df.groupby(["race_id", "geo_key"], sort=False)["votes"]
        top = grp.transform("max")
        is_top = (df["votes"] == top) & (top > 0)
        n_top = is_top.groupby([df["race_id"], df["geo_key"]], sort=False).transform("sum")
        df["winner"] = is_top & (n_top == 1)
        df["_lv"] = df["level"].map(_LEVEL_ORDER)
        df = df.sort_values(
            ["election_id", "race_id", "_lv", "geo_code", "ballot_candidate_id"], kind="stable"
        )
        out = df[list(RESULTS_COLUMNS)].reset_index(drop=True)
    for c in ("election_id", "year", "votes", "valid_votes", "eligible", "ballots_cast"):
        out[c] = out[c].astype(np.int64)
    out["share"] = out["share"].astype(float)
    out["winner"] = out["winner"].astype(bool)
    return out


def _attach_geography(session: Session, df: pd.DataFrame) -> None:
    """Fill ``geo_code``, ``geo_name`` and ``province_code`` from the level-specific ids."""
    prov = pd.DataFrame(
        session.execute(select(Province.id, Province.code, Province.name)).all(),
        columns=["id", "code", "name"],
    )
    p_code = dict(zip(prov["id"], prov["code"], strict=True))
    p_name = dict(zip(prov["id"], prov["name"], strict=True))
    geo_code = pd.Series([None] * len(df), index=df.index, dtype=object)
    geo_name = pd.Series([None] * len(df), index=df.index, dtype=object)
    lv = df["level"]
    m = lv == "municipality"
    if m.any():
        ids = df.loc[m, "municipality_id"].dropna().astype(int).unique().tolist()
        rows = _lookup(session, Municipality, ids)
        geo_code[m] = df.loc[m, "municipality_id"].map({i: c for i, c, _ in rows})
        geo_name[m] = df.loc[m, "municipality_id"].map({i: n for i, _, n in rows})
    d = lv == "district"
    if d.any():
        ids = df.loc[d, "district_id"].dropna().astype(int).unique().tolist()
        rows = _lookup(session, HouseDistrict, ids)
        geo_code[d] = df.loc[d, "district_id"].map({i: c for i, c, _ in rows})
        geo_name[d] = df.loc[d, "district_id"].map({i: (n or c) for i, c, n in rows})
    u = lv == "unit"
    if u.any():
        ids = df.loc[u, "geo_unit_id"].dropna().astype(int).unique().tolist()
        rows = _lookup(session, GeoUnit, ids)
        geo_code[u] = df.loc[u, "geo_unit_id"].map({i: c for i, c, _ in rows})
        geo_name[u] = df.loc[u, "geo_unit_id"].map({i: n for i, _, n in rows})
    p = lv == "province"
    if p.any():
        geo_code[p] = df.loc[p, "province_id"].map(p_code)
        geo_name[p] = df.loc[p, "province_id"].map(p_name)
    n = lv == "national"
    geo_code[n] = NATIONAL_CODE
    geo_name[n] = NATIONAL_NAME
    df["geo_code"] = geo_code
    df["geo_name"] = geo_name
    df["province_code"] = df["province_id"].map(p_code).where(~n, None)


def _lookup(session: Session, model: type, ids: list[int]) -> list[tuple[int, str, str | None]]:
    code_col = model.code if model is HouseDistrict else model.cbs_code  # type: ignore[attr-defined]
    out: list[tuple[int, str, str | None]] = []
    for part in _chunks(ids):
        out += (
            session.execute(select(model.id, code_col, model.name).where(model.id.in_(part))).tuples().all()
        )  # type: ignore[attr-defined]
    return out
