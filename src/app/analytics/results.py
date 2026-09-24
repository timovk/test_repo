"""Helpers for the **standard results frame** (docs/ARCHITECTURE.md §7).

Every analytics function and exporter operates on a :class:`pandas.DataFrame` with one row per
race × geographic unit × ballot line and the columns in :data:`RESULTS_COLUMNS`.  This module
provides the shared vocabulary used by :mod:`app.analytics.metrics`,
:mod:`app.analytics.history` and :mod:`app.export`:

* validation (:func:`validate_results_frame`, :func:`require_columns`),
* builders: :func:`build_results_frame` (fills derived columns) and
  :func:`results_frame_from_draw` (a simulated draw, DB-free, in the services layout),
* level/contest selection by race-type jurisdiction (:data:`JURISDICTION_LEVELS`,
  :func:`contest_rows`, :func:`seat_contests`) — services store every race at every level, so the
  levels present never decide where a race is contested,
* party keys (independents grouped under :data:`INDEPENDENT_KEY`) and race *families*
  (``PRESIDENT_PROVINCE`` contests belong to the ``PRESIDENT`` family; pooling never counts the
  same presidential ballot twice, :func:`dedupe_family_rows`),
* line ranking (:func:`ranked_lines`) and party aggregation (:func:`party_shares`,
  :func:`national_party_totals`).

All functions are pure: they never mutate their inputs and never touch the database.
All vote data handled here is SIMULATED.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

import numpy as np
import pandas as pd

from app.core.constitution import RaceType
from app.core.errors import ValidationError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.elections.types import ElectionDraw, RaceSpec
    from app.geography.frame import GeographyFrame

log = get_logger(__name__)

#: Column order of the standard results frame (docs/ARCHITECTURE.md §7).
RESULTS_COLUMNS: tuple[str, ...] = (
    "election_id",
    "year",
    "race_code",
    "race_type",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "line_key",
    "candidate",
    "party_code",
    "votes",
    "share",
    "valid_votes",
    "eligible",
    "ballots_cast",
    "winner",
)

#: Geographic levels from finest to coarsest.
LEVELS: tuple[str, ...] = ("unit", "municipality", "district", "province", "national")
LEVEL_RANK: dict[str, int] = {lvl: i for i, lvl in enumerate(LEVELS)}

#: Columns identifying one race result at one geographic unit ("contest-geo").
GEO_KEY: tuple[str, ...] = ("election_id", "race_code", "level", "geo_code")

#: Party key used for lines without a party (independents).  It cannot collide with a real
#: party code, which must match ``^[A-Z][A-Z0-9]{1,11}$`` (see ``app.scenarios.schema.PartySpec``).
INDEPENDENT_KEY: str = "_IND"

#: Code of the national geographic unit.
NATIONAL_CODE: str = "NL"
#: Name of the national level.
NATIONAL_LEVEL: str = "national"

#: Race types that are *not* seats (the national presidential parent race; its electoral votes
#: are won in the ``PRESIDENT_PROVINCE`` child contests).
NON_SEAT_RACE_TYPES: frozenset[str] = frozenset({RaceType.PRESIDENT.value})

#: Party-list races decided by proportional representation (D'Hondt, several seats per race).
#: They have a plurality list but no single-winner "seat"; FPTP seat analytics (wasted votes,
#: efficiency gap, uniform-swing seat projections, seat totals) exclude them.
PROPORTIONAL_RACE_TYPES: frozenset[str] = frozenset(
    {RaceType.PROVINCIAL_LEGISLATURE.value, RaceType.MUNICIPAL_COUNCIL.value}
)

#: Jurisdiction level of every race type: the level at which the race is decided (its *contest*
#: rows).  Services store every race at every level (unit → national, docs/ARCHITECTURE.md §3),
#: so the jurisdiction cannot be inferred from the levels present in a frame.
JURISDICTION_LEVELS: dict[str, str] = {
    RaceType.PRESIDENT.value: "national",
    RaceType.PRESIDENT_PROVINCE.value: "province",
    RaceType.HOUSE.value: "district",
    RaceType.SENATE.value: "province",
    RaceType.GOVERNOR.value: "province",
    RaceType.PROVINCIAL_LEGISLATURE.value: "province",
    RaceType.MAYOR.value: "municipality",
    RaceType.MUNICIPAL_COUNCIL.value: "municipality",
}

#: Levels whose geos each wholly contain a geo of the key level, finest first (municipalities and
#: House districts cross each other, so the level order is not a strict hierarchy).
CONTAINING_LEVELS: dict[str, tuple[str, ...]] = {
    "unit": ("municipality", "district", "province", "national"),
    "municipality": ("province", "national"),
    "district": ("province", "national"),
    "province": ("national",),
    "national": (),
}

GroupBy = Literal["race", "family"]

_INT_COLUMNS = ("election_id", "year", "votes", "valid_votes", "eligible", "ballots_cast")
_STR_COLUMNS = (
    "race_code",
    "race_type",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "line_key",
    "candidate",
    "party_code",
)


class ResultsFrameError(ValidationError):
    """A frame does not satisfy the standard results-frame contract."""


# --------------------------------------------------------------------------- validation
def require_columns(frame: pd.DataFrame, columns: Iterable[str], context: str = "results frame") -> None:
    """Raise :class:`ResultsFrameError` if any of ``columns`` is missing from ``frame``."""
    missing = [c for c in columns if c not in frame.columns]
    if missing:
        raise ResultsFrameError(f"{context}: missing required columns {missing}")


def validate_results_frame(frame: pd.DataFrame) -> list[str]:
    """Check a standard results frame; returns a list of problems (empty = OK).

    Checks: required columns, known levels, non-negative votes, ``votes ≤ valid_votes ≤
    ballots_cast ≤ eligible``, shares in [0, 1] and equal to ``votes / valid_votes``, unique
    (race, level, geo, line) rows, that ``valid_votes`` equals the sum of line votes for every
    contest-geo and that at most one line per contest-geo is flagged ``winner`` — a line with the
    most votes there (plurality; a tie may be flagged for the line that won it by lot).
    """
    problems: list[str] = []
    missing = [c for c in RESULTS_COLUMNS if c not in frame.columns]
    if missing:
        return [f"missing columns: {missing}"]
    if frame.empty:
        return problems
    bad_levels = sorted(set(frame["level"].dropna().astype(str)) - set(LEVELS))
    if bad_levels:
        problems.append(f"unknown levels: {bad_levels}")
    votes = frame["votes"].to_numpy(dtype=float)
    valid = frame["valid_votes"].to_numpy(dtype=float)
    ballots = frame["ballots_cast"].to_numpy(dtype=float)
    eligible = frame["eligible"].to_numpy(dtype=float)
    if (votes < 0).any():
        problems.append("negative votes")
    if (votes > valid).any():
        problems.append("votes exceed valid_votes")
    if (valid > ballots).any():
        problems.append("valid_votes exceed ballots_cast")
    if (ballots > eligible).any():
        problems.append("ballots_cast exceed eligible")
    share = frame["share"].to_numpy(dtype=float)
    if ((share < -1e-9) | (share > 1 + 1e-9)).any():
        problems.append("share outside [0, 1]")
    key = [*GEO_KEY, "line_key"]
    if frame.duplicated(key).any():
        problems.append("duplicate (election_id, race_code, level, geo_code, line_key) rows")
    has_valid = valid > 0
    expected = np.divide(votes, valid, out=np.zeros(len(frame)), where=has_valid)
    if (has_valid & ~(np.abs(share - expected) <= 1e-6)).any():
        problems.append("share differs from votes / valid_votes")
    grouped = frame.groupby(list(GEO_KEY), sort=False)["votes"]
    sums = grouped.transform("sum").to_numpy(dtype=float)
    if not np.allclose(sums, valid):
        problems.append("valid_votes differ from the sum of line votes in some contest-geos")
    flagged = as_bool(frame["winner"])
    if flagged.any():
        n_flagged = flagged.groupby([frame[c] for c in GEO_KEY], sort=False).transform("sum").to_numpy()
        if (n_flagged > 1).any():
            problems.append("more than one winner flagged in some contest-geos")
        top = grouped.transform("max").to_numpy(dtype=float)
        if (flagged.to_numpy() & (votes < top)).any():
            problems.append("winner flagged on a line without the most votes")
    return problems


# --------------------------------------------------------------------------- builders
def build_results_frame(rows: pd.DataFrame | Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """Build a standard results frame, filling derived columns.

    Required input columns: ``election_id, year, race_code, race_type, level, geo_code,
    line_key, votes``.  Missing optional columns are derived:

    * ``geo_name`` ← ``geo_code``; ``candidate`` ← ``line_key``; ``party_code`` ← null;
    * ``province_code`` ← ``geo_code`` for province rows, null otherwise;
    * ``valid_votes`` ← sum of line votes per contest-geo; ``ballots_cast`` ← ``valid_votes``;
      ``eligible`` ← ``ballots_cast``;
    * ``share`` ← ``votes / valid_votes`` (0 when there are no valid votes);
    * ``winner`` ← the plurality line (exact ties: lowest ``line_key``; no winner without votes).

    Returns a new frame with exactly :data:`RESULTS_COLUMNS` in contract order.
    """
    df = pd.DataFrame(rows).copy() if not isinstance(rows, pd.DataFrame) else rows.copy()
    require_columns(
        df,
        ("election_id", "year", "race_code", "race_type", "level", "geo_code", "line_key", "votes"),
        "build_results_frame",
    )
    df = df.reset_index(drop=True)
    if "geo_name" not in df:
        df["geo_name"] = df["geo_code"]
    if "candidate" not in df:
        df["candidate"] = df["line_key"]
    if "party_code" not in df:
        df["party_code"] = None
    if "province_code" not in df:
        df["province_code"] = np.where(df["level"] == "province", df["geo_code"], None)
    df["votes"] = _counts(df["votes"], "votes")
    if "valid_votes" not in df:
        df["valid_votes"] = df.groupby(list(GEO_KEY), sort=False)["votes"].transform("sum")
    if "ballots_cast" not in df:
        df["ballots_cast"] = df["valid_votes"]
    if "eligible" not in df:
        df["eligible"] = df["ballots_cast"]
    for col in _INT_COLUMNS:
        df[col] = _counts(df[col], col)
    if "share" not in df:
        valid = df["valid_votes"].to_numpy(dtype=float)
        df["share"] = np.divide(
            df["votes"].to_numpy(dtype=float), valid, out=np.zeros(len(df)), where=valid > 0
        )
    df["share"] = df["share"].astype(float)
    if "winner" not in df:
        ranked = ranked_lines(df.assign(winner=False))
        df["winner"] = False
        top = ranked.index[(ranked["_rank"] == 0).to_numpy() & (ranked["votes"] > 0).to_numpy()]
        df.loc[top, "winner"] = True
    df["winner"] = as_bool(df["winner"])
    return normalize_dtypes(df)


#: Race types aggregated to House districts when a unit → district mapping is given (the
#: presidential contests are reported by district as well, as services store them).
DISTRICT_LEVEL_RACE_TYPES: frozenset[str] = frozenset(
    {RaceType.HOUSE.value, RaceType.PRESIDENT.value, RaceType.PRESIDENT_PROVINCE.value}
)


def results_frame_from_draw(
    draw: ElectionDraw,
    races: Mapping[str, RaceSpec] | Iterable[RaceSpec],
    geography: GeographyFrame,
    *,
    election_id: int,
    year: int,
    unit_district: np.ndarray | Sequence[Any] | None = None,
    district_codes: Sequence[str] | None = None,
    levels: Iterable[str] = LEVELS,
) -> pd.DataFrame:
    """Standard results frame of a simulated :class:`~app.elections.types.ElectionDraw` without the
    database, laid out as services store results: every race at every requested level (unit →
    national, plus ``district`` for :data:`DISTRICT_LEVEL_RACE_TYPES` when ``unit_district`` is
    given) with exact integer sums (:func:`app.elections.tabulation.aggregate_levels`).

    ``winner`` flags the unique plurality leader at each geo (no flag on exact ties or without
    votes); ``candidate`` is the line label (else its key); ``party_code`` the line's party.
    Races are emitted in ``draw.races`` order; every race of the draw needs a spec in ``races``.
    """
    from app.elections.tabulation import aggregate_levels

    specs = dict(races) if isinstance(races, Mapping) else {r.key: r for r in races}
    wanted = [lvl for lvl in LEVELS if lvl in set(levels)]
    unknown = sorted(set(levels) - set(LEVELS))
    if unknown:
        raise ResultsFrameError(f"unknown levels {unknown}; expected a subset of {LEVELS}")
    missing = sorted(set(draw.races) - set(specs))
    if missing:
        raise ResultsFrameError(f"results_frame_from_draw: no race spec for {missing}")
    parts: list[pd.DataFrame] = []
    for code, rv in draw.races.items():
        spec = specs[code]
        rtype = str(spec.race_type.value if isinstance(spec.race_type, RaceType) else spec.race_type)
        by_district = unit_district is not None and rtype in DISTRICT_LEVEL_RACE_TYPES
        agg = aggregate_levels(
            rv,
            geography,
            unit_district=unit_district if by_district else None,
            district_codes=district_codes if by_district else None,
        )
        lines = {ln.key: ln for ln in spec.lines}
        label = {k: (lines[k].label or k) if k in lines else k for k in rv.line_keys}
        party = {k: lines[k].party_code if k in lines else None for k in rv.line_keys}
        for lvl in wanted:
            if lvl not in agg:
                continue
            df = agg[lvl]
            keys = df["line_key"]
            parts.append(
                pd.DataFrame(
                    {
                        "race_code": df["race_code"].to_numpy(dtype=object),
                        "race_type": rtype,
                        "level": lvl,
                        "geo_code": df["geo_code"].to_numpy(dtype=object),
                        "geo_name": df["geo_name"].to_numpy(dtype=object),
                        "province_code": df["province_code"].to_numpy(dtype=object)
                        if lvl != NATIONAL_LEVEL
                        else None,
                        "line_key": keys.to_numpy(dtype=object),
                        "candidate": keys.map(label).to_numpy(dtype=object),
                        "party_code": keys.map(party).to_numpy(dtype=object),
                        "votes": df["votes"].to_numpy(dtype=np.int64),
                        "share": df["share"].to_numpy(dtype=float),
                        "valid_votes": df["valid_votes"].to_numpy(dtype=np.int64),
                        "eligible": df["eligible"].to_numpy(dtype=np.int64),
                        "ballots_cast": df["ballots_cast"].to_numpy(dtype=np.int64),
                        "winner": df["winner"].to_numpy(dtype=bool),
                    }
                )
            )
    if not parts:
        return normalize_dtypes(pd.DataFrame(columns=list(RESULTS_COLUMNS)))
    out = pd.concat(parts, ignore_index=True)
    out.insert(0, "year", int(year))
    out.insert(0, "election_id", int(election_id))
    return normalize_dtypes(out)


def _counts(series: pd.Series, name: str) -> pd.Series:
    """``int64`` column; non-integral, missing or non-finite values raise (no silent truncation)."""
    if pd.api.types.is_integer_dtype(series.dtype) and not series.isna().any():
        return series.astype(np.int64)
    values = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float, na_value=np.nan)
    bad = ~np.isfinite(values) | (np.abs(values - np.round(values)) > 1e-9)
    if bad.any():
        raise ResultsFrameError(
            f"build_results_frame: {name} must be finite integers ({int(bad.sum())} rows)"
        )
    return pd.Series(np.round(values).astype(np.int64), index=series.index)


def normalize_dtypes(frame: pd.DataFrame, *, integer_counts: bool = True) -> pd.DataFrame:
    """Return :data:`RESULTS_COLUMNS` of ``frame`` with canonical dtypes: int64 ids and counts
    (float counts with ``integer_counts=False``, e.g. unrounded lineage-weighted votes), float
    shares, bool ``winner`` and object strings with ``None`` (not NaN) for nulls."""
    df = frame.loc[:, list(RESULTS_COLUMNS)].copy()
    for col in _INT_COLUMNS:
        keep_int = integer_counts or col in ("election_id", "year")
        df[col] = df[col].astype(np.int64 if keep_int else float)
    df["share"] = df["share"].astype(float)
    df["winner"] = as_bool(df["winner"])
    for col in _STR_COLUMNS:
        df[col] = df[col].astype(object).where(df[col].notna(), None)
    return df


# --------------------------------------------------------------------------- small helpers
def as_bool(series: pd.Series) -> pd.Series:
    """Coerce a possibly nullable / object column to plain ``bool`` (null → False)."""
    if series.dtype == bool:
        return series
    return series.astype("boolean").fillna(False).astype(bool)


def race_family(race_type: str | RaceType | None) -> str | None:
    """Race family: ``PRESIDENT_PROVINCE`` contests belong to ``PRESIDENT``; others to themselves."""
    if race_type is None or (isinstance(race_type, float) and np.isnan(race_type)):
        return None
    value = str(race_type.value if isinstance(race_type, RaceType) else race_type)
    return RaceType.PRESIDENT.value if value == RaceType.PRESIDENT_PROVINCE.value else value


def family_series(race_types: pd.Series) -> pd.Series:
    """Vectorised :func:`race_family`."""
    rt = race_types.astype(object)
    return rt.where(rt != RaceType.PRESIDENT_PROVINCE.value, RaceType.PRESIDENT.value)


def party_key_series(frame: pd.DataFrame) -> pd.Series:
    """Party key per row: ``party_code`` or :data:`INDEPENDENT_KEY` for independents."""
    pc = frame["party_code"].astype(object)
    missing = pc.isna() | (pc == "")
    return pc.where(~missing, INDEPENDENT_KEY)


def with_keys(frame: pd.DataFrame) -> pd.DataFrame:
    """Copy of ``frame`` with ``party`` (party key) and ``race_family`` columns added."""
    df = frame.copy()
    df["party"] = party_key_series(df)
    df["race_family"] = family_series(df["race_type"])
    return df


def as_frame(frames: pd.DataFrame | Iterable[pd.DataFrame]) -> pd.DataFrame:
    """Concatenate a sequence of results frames (or pass a single frame through)."""
    if isinstance(frames, pd.DataFrame):
        return frames
    items = [f for f in frames if f is not None and len(f)]
    if not items:
        return pd.DataFrame(columns=list(RESULTS_COLUMNS))
    return pd.concat(items, ignore_index=True)


def single_election(frame: pd.DataFrame, context: str) -> tuple[int, int]:
    """Return ``(election_id, year)`` of a frame that must contain exactly one election."""
    ids = frame["election_id"].dropna().unique()
    if len(ids) != 1:
        raise ResultsFrameError(f"{context}: expected exactly one election, found {len(ids)}")
    year = frame.loc[frame["election_id"] == ids[0], "year"].iloc[0]
    return int(ids[0]), int(year)


def normalize_values(values: Any) -> list[str]:
    """Scalar-or-iterable of strings/enums → list of plain strings."""
    if values is None:
        return []
    if isinstance(values, str | RaceType) or not isinstance(values, Iterable):
        values = [values]
    return [str(v.value) if hasattr(v, "value") else str(v) for v in values]


def filter_family(frame: pd.DataFrame, race_type: str | RaceType | Iterable[str] | None) -> pd.DataFrame:
    """Rows whose race *family* matches ``race_type`` (``PRESIDENT`` includes its province contests)."""
    if race_type is None:
        return frame
    fams = {race_family(v) for v in normalize_values(race_type)}
    return frame[family_series(frame["race_type"]).isin(fams).to_numpy()]


def jurisdiction_level(race_type: str | RaceType | None) -> str | None:
    """Level at which a race of ``race_type`` is decided (:data:`JURISDICTION_LEVELS`); None for
    unknown types."""
    if race_type is None or (isinstance(race_type, float) and np.isnan(race_type)):
        return None
    value = str(race_type.value if isinstance(race_type, RaceType) else race_type)
    return JURISDICTION_LEVELS.get(value)


def dedupe_family_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows of ``frame`` without double counting when a family's races are pooled.

    The national ``PRESIDENT`` parent race and its ``PRESIDENT_PROVINCE`` contests count the same
    ballots.  Services store both at every level, so pooling the family at, say, a municipality
    would count every presidential ballot twice.  ``PRESIDENT_PROVINCE`` rows at an
    ``(election_id, level, geo_code)`` where the parent race has rows are dropped (the parent's
    rows hold identical counts); every other row is kept.
    """
    if frame.empty:
        return frame
    rt = frame["race_type"].astype(str).to_numpy()
    is_parent = rt == RaceType.PRESIDENT.value
    is_child = rt == RaceType.PRESIDENT_PROVINCE.value
    if not is_parent.any() or not is_child.any():
        return frame
    keys = ["election_id", "level", "geo_code"]
    parent = pd.MultiIndex.from_frame(frame.loc[is_parent, keys].drop_duplicates())
    child = np.flatnonzero(is_child)
    drop = np.zeros(len(frame), dtype=bool)
    drop[child] = pd.MultiIndex.from_frame(frame.iloc[child][keys]).isin(parent)
    return frame.take(np.flatnonzero(~drop)) if drop.any() else frame


# --------------------------------------------------------------------------- selection
_UNKNOWN_J = len(LEVELS)


def _contest_scores() -> np.ndarray:
    """Preference of a level (column) as the contest level of a race whose jurisdiction is the row
    (last row: unknown race type).  Higher is better; see :func:`contest_rows`."""
    scores = np.zeros((len(LEVELS) + 1, len(LEVELS)), dtype=np.int64)
    for j, jur in enumerate(LEVELS):
        for li, lvl in enumerate(LEVELS):
            if lvl == jur:
                scores[j, li] = 100
            elif lvl in CONTAINING_LEVELS[jur]:
                scores[j, li] = 50 - CONTAINING_LEVELS[jur].index(lvl)
            else:
                scores[j, li] = li
    scores[_UNKNOWN_J] = np.arange(len(LEVELS))
    return scores


_CONTEST_SCORES = _contest_scores()


def contest_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows at each race's contest level: its jurisdiction level (:data:`JURISDICTION_LEVELS`).

    ``HOUSE-NB-07`` → its ``district`` rows; ``PRES-NB`` → its ``province`` rows; ``PRES`` → the
    ``national`` row set — whatever other levels the frame holds for the race (services store
    every race at every level).  Fallbacks, per election × race:

    * jurisdiction level absent, a level wholly containing it present (``PRES-NB`` at
      ``national`` only): the finest such level — it holds the race's full totals;
    * only finer levels present (a frame pre-filtered to ``municipality``): the coarsest of those;
    * unknown race type: the coarsest level present.
    """
    if frame.empty:
        return frame
    lvl = frame["level"].map(LEVEL_RANK)
    if lvl.isna().any():
        bad = sorted(set(frame.loc[lvl.isna(), "level"].astype(str)))
        raise ResultsFrameError(f"unknown levels: {bad}")
    jur = frame["race_type"].astype(str).map(JURISDICTION_LEVELS).map(LEVEL_RANK)
    j = jur.fillna(_UNKNOWN_J).to_numpy(dtype=np.int64)
    score = pd.Series(_CONTEST_SCORES[j, lvl.to_numpy(dtype=np.int64)], index=frame.index)
    top = score.groupby([frame["election_id"], frame["race_code"]], sort=False).transform("max")
    return frame[(score == top).to_numpy()]


def seat_contests(frame: pd.DataFrame) -> pd.DataFrame:
    """Contest rows of single-winner races that award a seat.

    Excludes the national ``PRESIDENT`` parent race (its electoral votes are won in the
    ``PRESIDENT_PROVINCE`` contests) and the proportional party-list races of
    :data:`PROPORTIONAL_RACE_TYPES` (several seats by D'Hondt; the frame does not carry the seat
    counts needed to analyse them as seats).
    """
    rows = contest_rows(frame)
    rt = rows["race_type"].astype(str)
    excluded = NON_SEAT_RACE_TYPES | PROPORTIONAL_RACE_TYPES
    return rows[~rt.isin(excluded).to_numpy()]


def at_level(frame: pd.DataFrame, level: str | None) -> pd.DataFrame:
    """Rows at ``level`` (validated), or the whole frame when ``level`` is None."""
    if level is None:
        return frame
    if level not in LEVEL_RANK:
        raise ResultsFrameError(f"unknown level {level!r}; expected one of {LEVELS}")
    return frame[(frame["level"] == level).to_numpy()]


# --------------------------------------------------------------------------- ranking
def ranked_lines(frame: pd.DataFrame) -> pd.DataFrame:
    """Lines sorted within each contest-geo by votes (desc), then the ``winner`` flag (a line
    flagged as winner — e.g. after a tie resolved by lot — ranks first among equals), then
    ``line_key`` (asc).  Adds ``_rank`` (0 = leader) and ``_n`` (lines in the contest-geo).

    The original index is preserved so results can be written back.  Rows are ordered by
    ``GEO_KEY`` (nulls last), then rank.
    """
    n = len(frame)
    w = as_bool(frame["winner"]).to_numpy() if "winner" in frame else np.zeros(n, dtype=bool)
    keys = [_sort_codes(frame[c]) for c in GEO_KEY]
    order = np.lexsort(
        (_sort_codes(frame["line_key"]), ~w, -frame["votes"].to_numpy(dtype=float), *reversed(keys))
    )
    df = frame.take(order)
    df["_w"] = w[order]
    sorted_keys = [k[order] for k in keys]
    new_group = np.zeros(n, dtype=bool)
    if n:
        new_group[0] = True
        for k in sorted_keys:
            new_group[1:] |= k[1:] != k[:-1]
    starts = np.flatnonzero(new_group)
    sizes = np.diff(np.r_[starts, n])
    df["_rank"] = np.arange(n) - np.repeat(starts, sizes)
    df["_n"] = np.repeat(sizes, sizes)
    return df


def _sort_codes(values: pd.Series) -> np.ndarray:
    """Integer codes whose order is the sort order of ``values`` (nulls last)."""
    codes, uniques = pd.factorize(values, sort=True)
    codes = codes.astype(np.int64, copy=False)
    codes[codes < 0] = len(uniques)
    return codes


# --------------------------------------------------------------------------- aggregation
def _geo_cols(by: GroupBy) -> list[str]:
    if by == "race":
        return ["election_id", "race_code", "level", "geo_code"]
    if by == "family":
        return ["election_id", "race_family", "level", "geo_code"]
    raise ValueError(f"by must be 'race' or 'family', got {by!r}")


def geo_totals(frame: pd.DataFrame, *, level: str | None = None, by: GroupBy = "race") -> pd.DataFrame:
    """One row per contest-geo (``by='race'``) or family-geo (``by='family'``) with ``year,
    race_family, race_type, geo_name, province_code, valid_votes, eligible, ballots_cast``.

    In family mode the geo totals of the family's races are summed (e.g. a municipality split
    across two House districts), each race-geo counted once and without counting the same
    ballots twice (:func:`dedupe_family_rows`).
    """
    df = at_level(frame, level)
    df = df if "race_family" in df else with_keys(df)
    if by == "family":
        df = dedupe_family_rows(df)
    per_race = df.drop_duplicates(list(GEO_KEY))
    cols = _geo_cols(by)
    agg: dict[str, tuple[str, str]] = {
        "year": ("year", "first"),
        "race_type": ("race_type", "first"),
        "geo_name": ("geo_name", "first"),
        "province_code": ("province_code", "first"),
        "valid_votes": ("valid_votes", "sum"),
        "eligible": ("eligible", "sum"),
        "ballots_cast": ("ballots_cast", "sum"),
    }
    if by == "race":
        agg["race_family"] = ("race_family", "first")
    out = per_race.groupby(cols, sort=False).agg(**agg).reset_index()
    if by == "family":
        out["race_code"] = None
    for col in ("valid_votes", "eligible", "ballots_cast"):
        out[col] = out[col].astype(np.int64)
    return out.sort_values(cols, kind="mergesort").reset_index(drop=True)


def party_shares(frame: pd.DataFrame, *, level: str | None = None, by: GroupBy = "race") -> pd.DataFrame:
    """Party (not line) votes and shares per contest-geo or family-geo.

    Columns: ``election_id, year, race_code, race_family, race_type, level, geo_code, geo_name,
    province_code, party, votes, valid_votes, eligible, ballots_cast, share, won``.

    ``won`` — race mode: the party of the winning line (see :func:`ranked_lines`); family mode:
    plurality of the aggregated party votes (ties broken by a flagged winner line, then party key).
    ``share`` is NaN when a geo has no valid votes.  ``race_code`` is null in family mode, which
    pools the family's races without counting the same ballots twice (:func:`dedupe_family_rows`).
    """
    df = with_keys(at_level(frame, level))
    cols = _geo_cols(by)
    if by == "family":
        df = dedupe_family_rows(df)
    totals = geo_totals(df, by=by)
    df["_w"] = as_bool(df["winner"])
    pv = (
        df.groupby([*cols, "party"], sort=False, dropna=False)
        .agg(votes=("votes", "sum"), _w=("_w", "any"))
        .reset_index()
    )
    out = pv.merge(totals, on=cols, how="left", validate="many_to_one")
    valid = out["valid_votes"].to_numpy(dtype=float)
    out["votes"] = out["votes"].astype(np.int64)
    out["share"] = np.divide(
        out["votes"].to_numpy(dtype=float), valid, out=np.full(len(out), np.nan), where=valid > 0
    )
    if by == "race":
        ranked = ranked_lines(df)
        lead = ranked[(ranked["_rank"] == 0).to_numpy() & ((ranked["votes"] > 0) | ranked["_w"]).to_numpy()]
        win = lead[[*cols, "party"]].assign(won=True)
        out = out.merge(win, on=[*cols, "party"], how="left")
        out["won"] = as_bool(out["won"])
    else:
        out = out.sort_values(
            [*cols, "votes", "_w", "party"],
            ascending=[True] * len(cols) + [False, False, True],
            kind="mergesort",
        )
        first = out.groupby(cols, sort=False).cumcount() == 0
        out["won"] = (first & ((out["votes"] > 0) | out["_w"])).to_numpy()
    out = out.drop(columns="_w")
    order = [
        "election_id",
        "year",
        "race_code",
        "race_family",
        "race_type",
        "level",
        "geo_code",
        "geo_name",
        "province_code",
        "party",
        "votes",
        "valid_votes",
        "eligible",
        "ballots_cast",
        "share",
        "won",
    ]
    out = out.sort_values([*cols, "party"], kind="mergesort").reset_index(drop=True)
    return out.loc[:, order]


def national_party_totals(frame: pd.DataFrame) -> pd.DataFrame:
    """National votes and shares per election × race family × party.

    Uses the contest rows of a national race of the family when there is one (the ``PRES``
    parent race), otherwise the sum of every race's contest rows (e.g. all 150 House districts,
    or the 12 ``PRES-<PV>`` contests).  Rows of a race below the national level — e.g. the
    ``national`` rows services store for ``HOUSE-NB-07`` or ``PRES-NB`` — never count as national
    totals of the family.  Columns: ``election_id, year, race_family, party, votes,
    valid_votes, share``.
    """
    cols = ["election_id", "year", "race_family", "party", "votes", "valid_votes", "share"]
    if frame.empty:
        return pd.DataFrame(columns=cols)
    rows = contest_rows(dedupe_family_rows(with_keys(frame)))
    jur = rows["race_type"].astype(str).map(JURISDICTION_LEVELS)
    national = (rows["level"] == NATIONAL_LEVEL).to_numpy() & (
        jur.isna() | (jur == NATIONAL_LEVEL)
    ).to_numpy()
    if national.any():
        nat_keys = pd.MultiIndex.from_frame(
            rows.loc[national, ["election_id", "race_family"]].drop_duplicates()
        )
        in_nat_family = pd.MultiIndex.from_frame(rows[["election_id", "race_family"]]).isin(nat_keys)
        rows = rows[national | ~in_nat_family]
    base = rows
    grp = ["election_id", "race_family"]
    valid = (
        base.drop_duplicates(list(GEO_KEY))
        .groupby(grp, sort=False)
        .agg(year=("year", "first"), valid_votes=("valid_votes", "sum"))
    )
    votes = base.groupby([*grp, "party"], sort=False)["votes"].sum().rename("votes").reset_index()
    out = votes.merge(valid.reset_index(), on=grp, how="left")
    v = out["valid_votes"].to_numpy(dtype=float)
    out["share"] = np.divide(
        out["votes"].to_numpy(dtype=float), v, out=np.full(len(out), np.nan), where=v > 0
    )
    out["votes"] = out["votes"].astype(np.int64)
    out["valid_votes"] = out["valid_votes"].astype(np.int64)
    return out.sort_values(["election_id", "race_family", "party"], kind="mergesort").reset_index(drop=True)[
        cols
    ]
