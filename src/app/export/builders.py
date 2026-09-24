"""Builders that shape standard results frames into export-schema tables.

Services pass the standard results frame (docs/ARCHITECTURE.md §7) and receive a DataFrame
already conformed to the named schema, ready for :mod:`app.export.writers`.  Datasets that come
straight from database rows (timeline, calls, forecasts, polls, districts, apportionment) only
need :func:`app.export.schemas.conform`.

Party columns in exports use ``null`` for independents (the analytics party key
``_IND`` is translated back).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import numpy as np
import pandas as pd

from app.analytics.history import compare_elections
from app.analytics.metrics import race_summaries
from app.analytics.results import INDEPENDENT_KEY, at_level, contest_rows, filter_family
from app.core.constitution import RaceType
from app.core.logging import get_logger
from app.export.schemas import conform, get_schema

log = get_logger(__name__)

_LEVEL_SCHEMA: dict[str, str] = {
    "national": "national_results",
    "province": "province_results",
    "municipality": "municipality_results",
    "unit": "unit_results",
    "district": "district_results",
}

_PARTY_COLUMNS = ("winner_party", "runner_up_party", "previous_winner_party", "incumbent_party", "party_code")


def _parties_to_codes(df: pd.DataFrame) -> pd.DataFrame:
    for c in _PARTY_COLUMNS:
        if c in df.columns:
            df[c] = df[c].astype(object).where(df[c].notna() & (df[c] != INDEPENDENT_KEY), None)
    return df


def _turnout(ballots: pd.Series, eligible: pd.Series) -> np.ndarray:
    b = ballots.to_numpy(dtype=float)
    e = eligible.to_numpy(dtype=float)
    return np.divide(b, e, out=np.full(len(b), np.nan), where=e > 0)


def results_export(
    frame: pd.DataFrame,
    level: str,
    *,
    unit_municipality: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """Rows of ``frame`` at ``level`` conformed to ``<level>_results`` (adds ``turnout``; for
    units, ``municipality_code`` from ``unit_municipality`` (buurt code → CBS municipality code))."""
    if level not in _LEVEL_SCHEMA:
        raise ValueError(f"no results schema for level {level!r}")
    rows = at_level(frame, level).copy()
    rows["turnout"] = _turnout(rows["ballots_cast"], rows["eligible"])
    if level == "unit":
        mapping = dict(unit_municipality or {})
        rows["municipality_code"] = rows["geo_code"].map(mapping) if mapping else None
    return conform(_parties_to_codes(rows), _LEVEL_SCHEMA[level])


def _summary(
    frame: pd.DataFrame,
    race_type: RaceType,
    prev: pd.DataFrame | None,
    incumbents: pd.DataFrame | None,
) -> pd.DataFrame:
    rows = frame[(frame["race_type"].astype(str) == race_type.value).to_numpy()]
    prev_rows = None if prev is None else prev[(prev["race_type"].astype(str) == race_type.value).to_numpy()]
    out = race_summaries(contest_rows(rows), prev=prev_rows, incumbents=incumbents)
    return _parties_to_codes(out)


def _code_part(race_codes: pd.Series, index: int) -> pd.Series:
    """``index``-th ``-``-separated part of each race code (``SEN-NB-2`` → ``NB`` for 1)."""
    return race_codes.astype(str).str.split("-").str[index]


def _at_jurisdiction(summary: pd.DataFrame, level: str) -> np.ndarray:
    return (summary["level"] == level).to_numpy()


def house_results_export(
    frame: pd.DataFrame,
    prev: pd.DataFrame | None = None,
    incumbents: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per House race conformed to ``house_results`` (flip vs ``prev`` / incumbents).

    ``district_code`` is the race's district-level geo code; for a frame without district rows
    it is taken from the race code (``HOUSE-NB-07`` → ``NB-07``) and ``district_name`` is null.
    """
    s = _summary(frame, RaceType.HOUSE, prev, incumbents)
    at = _at_jurisdiction(s, "district")
    from_code = s["race_code"].astype(str).str.removeprefix(f"{RaceType.HOUSE.value}-")
    s["district_code"] = np.where(at, s["geo_code"].to_numpy(dtype=object), from_code.to_numpy(dtype=object))
    s["district_name"] = np.where(at, s["geo_name"].to_numpy(dtype=object), None)
    s["province_code"] = s["province_code"].where(s["province_code"].notna(), _code_part(s["race_code"], 1))
    s = s.drop(columns=["geo_code", "geo_name"])
    return conform(s, "house_results", allow_unknown=True)


def _province_summary(s: pd.DataFrame) -> pd.DataFrame:
    """Province columns of a province-level race summary (``province_code`` from the race code
    when the frame lacks the race's province rows)."""
    at = _at_jurisdiction(s, "province")
    s = s.copy()
    s["province_name"] = np.where(at, s["geo_name"].to_numpy(dtype=object), None)
    code = np.where(
        at, s["geo_code"].to_numpy(dtype=object), _code_part(s["race_code"], 1).to_numpy(dtype=object)
    )
    s["province_code"] = code
    return s.drop(columns=["geo_code", "geo_name"])


def senate_results_export(
    frame: pd.DataFrame,
    prev: pd.DataFrame | None = None,
    incumbents: pd.DataFrame | None = None,
    *,
    senate_classes: Mapping[str, int] | None = None,
    special_races: Collection[str] | None = None,
) -> pd.DataFrame:
    """One row per Senate race conformed to ``senate_results``.

    ``seat_number`` is parsed from the race code (``SEN-NB-2`` → 2); ``senate_classes`` maps race
    codes to their class; ``special_races`` lists race codes held as special elections.
    """
    s = _province_summary(_summary(frame, RaceType.SENATE, prev, incumbents))
    tail = s["race_code"].astype(str).str.rsplit("-", n=1).str[-1]
    s["seat_number"] = pd.to_numeric(tail, errors="coerce")
    s["senate_class"] = s["race_code"].map(dict(senate_classes)) if senate_classes else None
    s["is_special"] = s["race_code"].isin(set(special_races)) if special_races is not None else None
    return conform(s, "senate_results", allow_unknown=True)


def governor_results_export(
    frame: pd.DataFrame,
    prev: pd.DataFrame | None = None,
    incumbents: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per governor race conformed to ``governor_results``."""
    s = _province_summary(_summary(frame, RaceType.GOVERNOR, prev, incumbents))
    return conform(s, "governor_results", allow_unknown=True)


def electoral_votes_export(
    frame: pd.DataFrame,
    ev_by_province: Mapping[str, int],
    *,
    decided_by: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """One row per province contest (``PRESIDENT_PROVINCE`` at level province) with its
    electoral votes, winner and margin, conformed to ``electoral_votes``.  ``decided_by`` maps
    province codes to how the contest was decided (default ``popular_vote``; ``lot`` for ties)."""
    rows = frame[
        (frame["race_type"].astype(str) == RaceType.PRESIDENT_PROVINCE.value).to_numpy()
        & (frame["level"] == "province").to_numpy()
    ]
    s = _parties_to_codes(race_summaries(rows))
    missing = sorted(set(s["geo_code"]) - set(ev_by_province))
    if missing:
        raise ValueError(f"electoral_votes_export: no electoral votes for provinces {missing}")
    s = s.drop(columns="province_code").rename(
        columns={"geo_code": "province_code", "geo_name": "province_name"}
    )
    s["electoral_votes"] = s["province_code"].map(dict(ev_by_province))
    default = np.where(s["tied"].to_numpy(dtype=bool), "lot", "popular_vote")
    given = s["province_code"].map(dict(decided_by or {}))
    s["decided_by"] = given.where(given.notna(), pd.Series(default, index=s.index))
    s.loc[s["winner_party"].isna() & s["winner_line_key"].isna(), "decided_by"] = None
    return conform(s, "electoral_votes", allow_unknown=True)


def swing_export(
    prev: pd.DataFrame,
    curr: pd.DataFrame,
    level: str,
    *,
    race_type: str | RaceType | None = None,
    lineage: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """:func:`app.analytics.history.compare_elections` conformed to the ``swing`` schema."""
    table = compare_elections(
        filter_family(prev, race_type), filter_family(curr, race_type), level, lineage=lineage
    )
    return conform(table, "swing")


def build_all_results(
    frame: pd.DataFrame,
    *,
    unit_municipality: Mapping[str, str] | None = None,
    levels: Collection[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """``{schema_name: frame}`` for every level present in ``frame`` (or the given ``levels``)."""
    present = [lvl for lvl in _LEVEL_SCHEMA if lvl in set(frame["level"])]
    use = [lvl for lvl in present if levels is None or lvl in set(levels)]
    return {
        get_schema(_LEVEL_SCHEMA[lvl]).name: results_export(frame, lvl, unit_municipality=unit_municipality)
        for lvl in use
    }
