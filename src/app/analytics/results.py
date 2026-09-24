"""Helpers for the **standard results frame** (docs/ARCHITECTURE.md §7).

Every analytics function and exporter operates on a :class:`pandas.DataFrame` with one row per
race × geographic unit × ballot line and the columns in :data:`RESULTS_COLUMNS`.  This module
provides the shared vocabulary used by :mod:`app.analytics.metrics`,
:mod:`app.analytics.history` and :mod:`app.export`:

* validation (:func:`validate_results_frame`, :func:`require_columns`),
* a builder that fills derived columns (:func:`build_results_frame`),
* level/contest selection (:func:`contest_rows`, :func:`seat_contests`),
* party keys (independents grouped under :data:`INDEPENDENT_KEY`) and race *families*
  (``PRESIDENT_PROVINCE`` contests belong to the ``PRESIDENT`` family),
* line ranking (:func:`ranked_lines`) and party aggregation (:func:`party_shares`,
  :func:`national_party_totals`).

All functions are pure: they never mutate their inputs and never touch the database.
All vote data handled here is SIMULATED.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd

from app.core.constitution import RaceType
from app.core.errors import ValidationError
from app.core.logging import get_logger

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

#: Race types that are *not* seats (the national presidential parent race; its electoral votes
#: are won in the ``PRESIDENT_PROVINCE`` child contests).
NON_SEAT_RACE_TYPES: frozenset[str] = frozenset({RaceType.PRESIDENT.value})

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
    ballots_cast ≤ eligible``, shares in [0, 1], unique (race, level, geo, line) rows and that
    ``valid_votes`` equals the sum of line votes for every contest-geo.
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
    sums = frame.groupby(list(GEO_KEY), sort=False)["votes"].transform("sum").to_numpy(dtype=float)
    if not np.allclose(sums, valid):
        problems.append("valid_votes differ from the sum of line votes in some contest-geos")
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
    df["votes"] = df["votes"].astype(np.int64)
    if "valid_votes" not in df:
        df["valid_votes"] = df.groupby(list(GEO_KEY), sort=False)["votes"].transform("sum")
    if "ballots_cast" not in df:
        df["ballots_cast"] = df["valid_votes"]
    if "eligible" not in df:
        df["eligible"] = df["ballots_cast"]
    for col in _INT_COLUMNS:
        df[col] = df[col].astype(np.int64)
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


# --------------------------------------------------------------------------- selection
def contest_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows at each race's jurisdiction level (the coarsest level present for that race).

    ``HOUSE-NB-07`` → its ``district`` rows; ``PRES-NB`` → its ``province`` rows; ``PRES`` → the
    ``national`` row set.  If a frame was pre-filtered to a finer level only, that level is used.
    """
    if frame.empty:
        return frame
    rank = frame["level"].map(LEVEL_RANK)
    if rank.isna().any():
        bad = sorted(set(frame.loc[rank.isna(), "level"].astype(str)))
        raise ResultsFrameError(f"unknown levels: {bad}")
    top = rank.groupby([frame["election_id"], frame["race_code"]], sort=False).transform("max")
    return frame[(rank == top).to_numpy()]


def seat_contests(frame: pd.DataFrame) -> pd.DataFrame:
    """Contest rows of races that award a seat (excludes the national ``PRESIDENT`` parent race;
    its electoral votes are won in the ``PRESIDENT_PROVINCE`` contests)."""
    rows = contest_rows(frame)
    return rows[~rows["race_type"].astype(str).isin(NON_SEAT_RACE_TYPES).to_numpy()]


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

    The original index is preserved so results can be written back.
    """
    df = frame.copy()
    df["_w"] = as_bool(df["winner"]) if "winner" in df else False
    df = df.sort_values(
        [*GEO_KEY, "votes", "_w", "line_key"],
        ascending=[True, True, True, True, False, False, True],
        kind="mergesort",
    )
    grouped = df.groupby(list(GEO_KEY), sort=False)
    df["_rank"] = grouped.cumcount()
    df["_n"] = grouped["votes"].transform("size")
    return df


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
    across two House districts), each race-geo counted once.
    """
    df = at_level(frame, level)
    df = df if "race_family" in df else with_keys(df)
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
    return out


def party_shares(frame: pd.DataFrame, *, level: str | None = None, by: GroupBy = "race") -> pd.DataFrame:
    """Party (not line) votes and shares per contest-geo or family-geo.

    Columns: ``election_id, year, race_code, race_family, race_type, level, geo_code, geo_name,
    province_code, party, votes, valid_votes, eligible, ballots_cast, share, won``.

    ``won`` — race mode: the party of the winning line (see :func:`ranked_lines`); family mode:
    plurality of the aggregated party votes (ties broken by a flagged winner line, then party key).
    ``share`` is NaN when a geo has no valid votes.  ``race_code`` is null in family mode.
    """
    df = with_keys(at_level(frame, level))
    cols = _geo_cols(by)
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

    Uses the ``national`` rows of a family when present (the ``PRES`` parent race), otherwise the
    sum of every race's contest rows (e.g. all 150 House districts).  Columns: ``election_id,
    year, race_family, party, votes, valid_votes, share``.
    """
    cols = ["election_id", "year", "race_family", "party", "votes", "valid_votes", "share"]
    if frame.empty:
        return pd.DataFrame(columns=cols)
    df = with_keys(frame)
    nat = df[(df["level"] == "national").to_numpy()]
    rest = contest_rows(df)
    if not nat.empty:
        nat_keys = pd.MultiIndex.from_frame(nat[["election_id", "race_family"]].drop_duplicates())
        has_nat = pd.MultiIndex.from_frame(rest[["election_id", "race_family"]]).isin(nat_keys)
        rest = rest[~has_nat]
    base = pd.concat([nat, rest], ignore_index=True)
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
