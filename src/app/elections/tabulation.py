"""Tabulation: from unit-level :class:`~app.elections.types.RaceVotes` to official race results.

Two responsibilities:

* :func:`tabulate` — the *official* count of one race: totals, shares, a deterministic ranking,
  the plurality winner, the margin and the tie status.  An exact first-place tie is resolved by
  a seeded drawing of lots (:func:`app.core.rng.stable_choice_order`) when ``resolve_ties`` is
  set; the result records how the race was decided (``decided_by``: ``popular_vote`` | ``lot``).
* :func:`aggregate_levels` — exact integer aggregation of a race to every geographic level
  (unit → municipality → [district] → province → national) as long DataFrames compatible with
  the standard results frame (docs/ARCHITECTURE.md §7), and :func:`reconcile`, which proves
  that every level sums exactly to the next one.

Withdrawn ballot lines are *not* special here: ballots printed with a withdrawn candidate still
count, are tabulated and can even win (as with candidates who withdraw or die after the ballots
are printed).  Party disappearance (a line with ``party_code=None``) is equally irrelevant to
tabulation, which only sees line keys.

All numbers produced here are SIMULATED (see :class:`app.core.constitution.DataCategory`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from app.core.errors import ElectionError
from app.core.logging import get_logger
from app.core.rng import stable_choice_order
from app.elections.types import RaceVotes, TabulatedRace
from app.geography.frame import GeographyFrame, group_sum

log = get_logger(__name__)

#: ``decided_by`` values produced by tabulation.
DECIDED_POPULAR_VOTE = "popular_vote"
DECIDED_LOT = "lot"

#: Geographic levels in aggregation order (``district`` only when a district mapping is given).
LEVELS: tuple[str, ...] = ("unit", "municipality", "district", "province", "national")

#: ``geo_code`` of the national level.
NATIONAL_CODE = "NL"
NATIONAL_NAME = "Nederland"

#: Columns of every level DataFrame returned by :func:`aggregate_levels`.
LEVEL_COLUMNS: tuple[str, ...] = (
    "race_code",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "municipality_code",
    "district_code",
    "line_key",
    "line_index",
    "votes",
    "share",
    "valid_votes",
    "ballots_cast",
    "eligible",
    "blank",
    "invalid",
    "winner",
)

_COUNT_COLUMNS: tuple[str, ...] = ("valid_votes", "ballots_cast", "eligible", "blank", "invalid")


@dataclass
class TabulatedRaceResult(TabulatedRace):
    """:class:`TabulatedRace` plus how the winner was determined.

    ``decided_by`` is ``'popular_vote'`` for an ordinary plurality, ``'lot'`` when an exact
    first-place tie was resolved by seeded lot, and ``None`` when a tie was left unresolved
    (``resolve_ties=False``).  ``tied_lines`` lists the line indices sharing first place when
    ``tied`` is true (in ballot order).
    """

    decided_by: str | None = DECIDED_POPULAR_VOTE
    tied_lines: tuple[int, ...] = field(default_factory=tuple)
    lot_seed: int | None = None

    @property
    def winner_key(self) -> str | None:
        """Line key of the winner (``None`` for an unresolved tie or an empty race)."""
        return None if self.winner is None else self.line_keys[self.winner]

    @property
    def runner_up(self) -> int | None:
        """Line index ranked second (``None`` for single-line races)."""
        return self.ranking[1] if len(self.ranking) > 1 else None


def _counts(arr: np.ndarray | Sequence, name: str, key: str) -> np.ndarray:
    """``arr`` as int64 counts; refuses non-integral or non-finite values instead of truncating."""
    a = np.asarray(arr)
    if a.dtype.kind in "iu":
        return a.astype(np.int64, copy=False)
    if a.dtype.kind == "b":
        return a.astype(np.int64)
    if a.dtype.kind == "f":
        if not np.isfinite(a).all() or not np.array_equal(a, np.round(a)):
            raise ElectionError(f"{key}: {name} must hold whole ballot counts (got non-integral values)")
        return a.astype(np.int64)
    if a.size == 0:
        return a.astype(np.int64)
    raise ElectionError(f"{key}: {name} must be an integer array (dtype {a.dtype})")


def _check_line_keys(line_keys: Sequence[str], key: str) -> None:
    if len(set(line_keys)) != len(line_keys):
        dup = sorted({k for k in line_keys if list(line_keys).count(k) > 1})
        raise ElectionError(f"{key}: duplicate ballot line keys {dup}")


def decided_by(tab: TabulatedRace) -> str | None:
    """``decided_by`` of any :class:`TabulatedRace` (plain instances count as popular vote)."""
    if isinstance(tab, TabulatedRaceResult):
        return tab.decided_by
    if tab.winner is None:
        return None
    return DECIDED_LOT if tab.tied else DECIDED_POPULAR_VOTE


def rank_lines(totals: np.ndarray) -> list[int]:
    """Line indices sorted by votes descending; equal totals keep ballot (index) order."""
    totals = np.asarray(totals)
    # np.lexsort sorts by the last key first: primary −votes, secondary index (stable ballot order).
    return [int(i) for i in np.lexsort((np.arange(len(totals)), -totals.astype(np.int64)))]


def tabulate(
    race_votes: RaceVotes,
    race_key: str | int | None = None,
    tie_seed: int | None = None,
    resolve_ties: bool = True,
) -> TabulatedRaceResult:
    """Official count of one race.

    Parameters
    ----------
    race_votes:
        Unit-level counts (validated with :meth:`RaceVotes.check`).
    race_key:
        Overrides ``race_votes.race_key`` in the result and in the lot stream.  For compatibility
        with the ``tabulate(race_votes, tie_seed)`` form of docs/ARCHITECTURE.md §6 an ``int``
        passed here is interpreted as ``tie_seed``.
    tie_seed:
        Root seed of the drawing of lots for an exact first-place tie (``0`` when omitted, so the
        lot is still deterministic).  The lot stream is keyed by the race key.
    resolve_ties:
        When false an exact first-place tie leaves ``winner=None`` (``decided_by=None``); the
        caller (e.g. the recount engine) decides what happens next.  A race with lines but no
        valid votes at all is an exact tie of every line (pass ``resolve_ties=False`` when
        tabulating partial election-night counts so that such a race has no winner).

    Raises
    ------
    ElectionError
        On duplicate line keys, negative or non-integral counts, or a votes matrix that does
        not match ``line_keys``.

    Returns
    -------
    TabulatedRaceResult
        ``ranking`` sorts lines by votes descending; lines with equal votes keep ballot order,
        except that a first-place tie resolved by lot puts the lot order first.  ``margin_votes``
        is winner (or leader) minus runner-up (the leader's total in a single-line race);
        ``margin_pct`` is that margin in percentage points of valid votes.
    """
    if isinstance(race_key, int | np.integer) and not isinstance(race_key, bool):
        if tie_seed is not None:
            raise TypeError("race_key must be a string when tie_seed is also given")
        race_key, tie_seed = None, int(race_key)
    key = race_key or race_votes.race_key
    _check_line_keys(race_votes.line_keys, key)
    votes = _counts(race_votes.votes, "votes", key)
    if votes.ndim != 2 or votes.shape[1] != len(race_votes.line_keys):
        raise ElectionError(f"{key}: votes must be (units, lines) matching line_keys")
    if (votes < 0).any():
        raise ElectionError(f"{key}: negative vote counts")
    totals = votes.sum(axis=0)
    valid = int(totals.sum())
    n_lines = len(totals)
    shares = totals / valid if valid > 0 else np.zeros(n_lines, dtype=float)
    ranking = rank_lines(totals)

    winner: int | None = None
    tied = False
    tied_lines: tuple[int, ...] = ()
    how: str | None = DECIDED_POPULAR_VOTE
    lot_seed: int | None = None
    if n_lines == 1:
        winner = 0
    elif n_lines > 1:
        top = totals[ranking[0]]
        tied_lines = tuple(sorted(int(i) for i in np.flatnonzero(totals == top)))
        if len(tied_lines) > 1:
            tied = True
            if resolve_ties:
                lot_seed = 0 if tie_seed is None else int(tie_seed)
                keys = [race_votes.line_keys[i] for i in tied_lines]
                order = stable_choice_order(keys, lot_seed, "tabulation-lot", key)
                by_key = {race_votes.line_keys[i]: i for i in tied_lines}
                lot_ranked = [by_key[k] for k in order]
                ranking = lot_ranked + [i for i in ranking if i not in by_key.values()]
                winner = ranking[0]
                how = DECIDED_LOT
                log.info(
                    "exact tie resolved by lot",
                    extra={"ctx": {"race": key, "winner": race_votes.line_keys[winner]}},
                )
            else:
                how = None
        else:
            tied_lines = ()
            winner = ranking[0]

    if n_lines == 0:
        margin_votes = 0
        how = None
    elif n_lines == 1:
        margin_votes = int(totals[0])
    else:
        margin_votes = int(totals[ranking[0]] - totals[ranking[1]])
    margin_pct = 100.0 * margin_votes / valid if valid > 0 else 0.0

    return TabulatedRaceResult(
        race_key=key,
        line_keys=list(race_votes.line_keys),
        totals=totals,
        valid=valid,
        ballots_cast=int(_counts(race_votes.ballots_cast, "ballots_cast", key).sum()),
        eligible=int(_counts(race_votes.eligible, "eligible", key).sum()),
        blank=int(_counts(race_votes.blank, "blank", key).sum()),
        invalid=int(_counts(race_votes.invalid, "invalid", key).sum()),
        ranking=ranking,
        winner=winner,
        tied=tied,
        margin_votes=margin_votes,
        margin_pct=float(margin_pct),
        shares=shares,
        decided_by=how,
        tied_lines=tied_lines,
        lot_seed=lot_seed,
    )


def tabulate_totals(
    race_key: str,
    line_keys: Sequence[str],
    totals: Sequence[int] | np.ndarray,
    *,
    ballots_cast: int | None = None,
    eligible: int | None = None,
    blank: int = 0,
    invalid: int = 0,
    tie_seed: int | None = None,
    resolve_ties: bool = True,
) -> TabulatedRaceResult:
    """Tabulate a race from already-aggregated line totals (single pseudo-unit).

    Convenience for callers that only hold totals (e.g. forecast draws or hand-built maps).
    ``ballots_cast`` defaults to valid + blank + invalid and ``eligible`` to ``ballots_cast``.
    """
    t = np.asarray(totals, dtype=np.int64).reshape(1, -1)
    valid = int(t.sum())
    cast = valid + blank + invalid if ballots_cast is None else int(ballots_cast)
    elig = cast if eligible is None else int(eligible)
    rv = RaceVotes(
        race_key=race_key,
        line_keys=list(line_keys),
        unit_index=np.zeros(1, dtype=np.int64),
        votes=t,
        ballots_cast=np.array([cast], dtype=np.int64),
        blank=np.array([blank], dtype=np.int64),
        invalid=np.array([invalid], dtype=np.int64),
        eligible=np.array([elig], dtype=np.int64),
    )
    return tabulate(rv, race_key, tie_seed=tie_seed, resolve_ties=resolve_ties)


# --------------------------------------------------------------------------- aggregation
def _unique_winner_mask(votes: np.ndarray) -> np.ndarray:
    """(G, L) bool: True for the unique plurality leader of each row (all False on exact ties or 0 votes)."""
    if votes.shape[1] == 0:
        return np.zeros_like(votes, dtype=bool)
    top = votes.max(axis=1, keepdims=True)
    is_top = (votes == top) & (top > 0)
    unique = is_top.sum(axis=1, keepdims=True) == 1
    return is_top & unique


def _level_frame(
    race_code: str,
    level: str,
    geo_codes: Sequence[str],
    geo_names: Sequence[str],
    province_codes: Sequence[str | None],
    municipality_codes: Sequence[str | None],
    district_codes: Sequence[str | None],
    line_keys: Sequence[str],
    votes: np.ndarray,
    counts: Mapping[str, np.ndarray],
) -> pd.DataFrame:
    G, L = votes.shape
    valid = counts["valid_votes"]
    with np.errstate(invalid="ignore", divide="ignore"):
        share = np.where(valid[:, None] > 0, votes / np.maximum(valid, 1)[:, None], 0.0)
    data: dict[str, object] = {
        "race_code": np.full(G * L, race_code, dtype=object),
        "level": np.full(G * L, level, dtype=object),
        "geo_code": np.repeat(np.asarray(geo_codes, dtype=object), L),
        "geo_name": np.repeat(np.asarray(geo_names, dtype=object), L),
        "province_code": np.repeat(np.asarray(province_codes, dtype=object), L),
        "municipality_code": np.repeat(np.asarray(municipality_codes, dtype=object), L),
        "district_code": np.repeat(np.asarray(district_codes, dtype=object), L),
        "line_key": np.tile(np.asarray(line_keys, dtype=object), G),
        "line_index": np.tile(np.arange(L, dtype=np.int64), G),
        "votes": votes.reshape(-1).astype(np.int64),
        "share": share.reshape(-1).astype(float),
    }
    for col in _COUNT_COLUMNS:
        data[col] = np.repeat(counts[col].astype(np.int64), L)
    data["winner"] = _unique_winner_mask(votes).reshape(-1)
    return pd.DataFrame(data, columns=list(LEVEL_COLUMNS))


def _resolve_unit_district(
    race_votes: RaceVotes,
    frame: GeographyFrame,
    unit_district: np.ndarray | Sequence,
    district_codes: Sequence[str] | None,
) -> tuple[np.ndarray, list[str]]:
    """Return (n,) district index per race unit and the district code list."""
    arr = np.asarray(unit_district)
    n = len(race_votes.unit_index)
    if len(arr) == frame.n_units:
        arr = arr[np.asarray(race_votes.unit_index, dtype=np.int64)]
    elif len(arr) != n:
        raise ElectionError(
            f"unit_district has length {len(arr)}; expected {frame.n_units} (frame units) or {n} (race units)"
        )
    if arr.dtype.kind == "f" and len(arr) and np.isfinite(arr).all() and np.array_equal(arr, np.round(arr)):
        arr = arr.astype(np.int64)  # integral floats (e.g. from a pandas merge) are indices
    if arr.dtype.kind in "iu":
        idx = arr.astype(np.int64)
        if (idx < 0).any():
            raise ElectionError(f"{race_votes.race_key}: {int((idx < 0).sum())} race units have no district")
        if district_codes is None:
            codes = [f"D{i:03d}" for i in range(int(idx.max()) + 1 if len(idx) else 0)]
        else:
            codes = [str(c) for c in district_codes]
            if len(idx) and idx.max() >= len(codes):
                raise ElectionError("district index out of range of district_codes")
        return idx, codes
    labels = arr.astype(object)
    if any(lab is None or (isinstance(lab, float) and np.isnan(lab)) or lab == "" for lab in labels):
        raise ElectionError(f"{race_votes.race_key}: some race units have no district")
    codes = (
        [str(c) for c in district_codes] if district_codes is not None else sorted({str(x) for x in labels})
    )
    lookup = {c: i for i, c in enumerate(codes)}
    try:
        idx = np.fromiter((lookup[str(x)] for x in labels), dtype=np.int64, count=len(labels))
    except KeyError as exc:
        raise ElectionError(f"district code {exc} not in district_codes") from exc
    return idx, codes


def aggregate_levels(
    race_votes: RaceVotes,
    frame: GeographyFrame,
    unit_district: np.ndarray | Sequence | None = None,
    district_codes: Sequence[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """Aggregate a race to every geographic level with exact integer sums.

    Parameters
    ----------
    race_votes:
        Unit-level counts; ``unit_index`` indexes ``frame`` units.
    frame:
        The geography (REAL CBS or synthetic).
    unit_district:
        Optional House-district assignment, either aligned with ``frame`` units (length U) or
        with the race's units (length n).  Integer indices into ``district_codes`` or district
        code strings.  Every race unit must have a district and every district must lie within
        a single province.
    district_codes:
        District codes for integer ``unit_district`` (default ``D000``, ``D001``, …); for string
        labels it fixes the output order (default: sorted).

    Returns
    -------
    dict[str, DataFrame]
        Keys ``unit``, ``municipality``, [``district``], ``province``, ``national`` (only
        geographies containing race units).  One row per geo × ballot line with the columns in
        :data:`LEVEL_COLUMNS`; ``share`` is of valid votes at that level and ``winner`` flags the
        *unique* plurality leader at that level (no row is flagged on an exact tie or with zero
        votes — the race's official winner, including lots, comes from :func:`tabulate`).
    """
    code = race_votes.race_key
    _check_line_keys(race_votes.line_keys, code)
    try:
        race_votes.check()
    except AssertionError as exc:
        raise ElectionError(f"{code}: inconsistent RaceVotes (RaceVotes.check failed)") from exc
    uidx = _counts(race_votes.unit_index, "unit_index", code)
    if len(uidx) and (uidx.min() < 0 or uidx.max() >= frame.n_units):
        raise ElectionError(f"{code}: unit_index out of range of the geography frame")
    if len(np.unique(uidx)) != len(uidx):
        raise ElectionError(f"{code}: unit_index lists some units more than once")
    votes = _counts(race_votes.votes, "votes", code).reshape(len(uidx), len(race_votes.line_keys))
    line_keys = list(race_votes.line_keys)
    unit_counts = {
        "valid_votes": votes.sum(axis=1),
        "ballots_cast": _counts(race_votes.ballots_cast, "ballots_cast", code),
        "eligible": _counts(race_votes.eligible, "eligible", code),
        "blank": _counts(race_votes.blank, "blank", code),
        "invalid": _counts(race_votes.invalid, "invalid", code),
    }
    unit_muni = frame.unit_muni[uidx]
    unit_prov = frame.unit_province[uidx]
    pcodes = np.asarray(frame.province_codes, dtype=object)
    mcodes = np.asarray(frame.muni_codes, dtype=object)
    ucodes = np.asarray(frame.unit_codes, dtype=object)
    unames = np.asarray(frame.unit_names, dtype=object)

    district_idx: np.ndarray | None = None
    dcodes: list[str] = []
    if unit_district is not None:
        district_idx, dcodes = _resolve_unit_district(race_votes, frame, unit_district, district_codes)

    n = len(uidx)
    levels: dict[str, pd.DataFrame] = {}
    levels["unit"] = _level_frame(
        code,
        "unit",
        ucodes[uidx],
        unames[uidx],
        pcodes[unit_prov],
        mcodes[unit_muni],
        np.asarray(dcodes, dtype=object)[district_idx] if district_idx is not None else [None] * n,
        line_keys,
        votes,
        unit_counts,
    )

    def grouped(index: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray]]:
        present = np.unique(index)
        g_votes = (
            group_sum(index, votes, size)[present]
            if len(line_keys)
            else np.zeros((len(present), 0), np.int64)
        )
        g_counts = {k: group_sum(index, v, size)[present] for k, v in unit_counts.items()}
        return present, g_votes.astype(np.int64), g_counts

    m_present, m_votes, m_counts = grouped(unit_muni, frame.n_munis)
    levels["municipality"] = _level_frame(
        code,
        "municipality",
        mcodes[m_present],
        np.asarray(frame.muni_names, dtype=object)[m_present],
        pcodes[frame.muni_province[m_present]],
        mcodes[m_present],
        [None] * len(m_present),
        line_keys,
        m_votes,
        m_counts,
    )

    if district_idx is not None:
        d_present, d_votes, d_counts = grouped(district_idx, len(dcodes))
        d_prov = np.full(len(dcodes), -1, dtype=np.int64)
        for d in d_present:
            provs = np.unique(unit_prov[district_idx == d])
            if len(provs) != 1:
                raise ElectionError(f"district {dcodes[d]} spans {len(provs)} provinces")
            d_prov[d] = provs[0]
        levels["district"] = _level_frame(
            code,
            "district",
            np.asarray(dcodes, dtype=object)[d_present],
            np.asarray(dcodes, dtype=object)[d_present],
            pcodes[d_prov[d_present]],
            [None] * len(d_present),
            np.asarray(dcodes, dtype=object)[d_present],
            line_keys,
            d_votes,
            d_counts,
        )

    p_present, p_votes, p_counts = grouped(unit_prov, frame.n_provinces)
    levels["province"] = _level_frame(
        code,
        "province",
        pcodes[p_present],
        np.asarray(frame.province_names, dtype=object)[p_present],
        pcodes[p_present],
        [None] * len(p_present),
        [None] * len(p_present),
        line_keys,
        p_votes,
        p_counts,
    )

    nat_votes = votes.sum(axis=0, keepdims=True)
    nat_counts = {k: np.array([int(v.sum())], dtype=np.int64) for k, v in unit_counts.items()}
    levels["national"] = _level_frame(
        code,
        "national",
        [NATIONAL_CODE],
        [NATIONAL_NAME],
        [None],
        [None],
        [None],
        line_keys,
        nat_votes,
        nat_counts,
    )
    return levels


def _check_rows(level: str, df: pd.DataFrame) -> list[str]:
    problems: list[str] = []
    if df.empty:
        return problems
    per_geo = df.groupby("geo_code", sort=False).agg(
        votes=("votes", "sum"),
        valid=("valid_votes", "first"),
        cast=("ballots_cast", "first"),
        blank=("blank", "first"),
        invalid=("invalid", "first"),
        eligible=("eligible", "first"),
    )
    bad = per_geo.index[per_geo["votes"] != per_geo["valid"]]
    if len(bad):
        problems.append(
            f"{level}: line votes do not sum to valid_votes in {len(bad)} geographies (e.g. {bad[0]})"
        )
    bad = per_geo.index[per_geo["valid"] + per_geo["blank"] + per_geo["invalid"] != per_geo["cast"]]
    if len(bad):
        problems.append(
            f"{level}: valid + blank + invalid != ballots_cast in {len(bad)} geographies (e.g. {bad[0]})"
        )
    bad = per_geo.index[per_geo["cast"] > per_geo["eligible"]]
    if len(bad):
        problems.append(f"{level}: ballots_cast exceeds eligible in {len(bad)} geographies (e.g. {bad[0]})")
    if (df["votes"] < 0).any():
        problems.append(f"{level}: negative vote counts")
    return problems


def _check_sum(child: pd.DataFrame, parent: pd.DataFrame, key: str, label: str) -> list[str]:
    """Child rows grouped by ``key`` (+ line) must equal the parent rows exactly."""
    if child.empty:
        # a race without units: the parent level may only carry all-zero rows (e.g. national)
        if parent.empty or not parent[["votes", *_COUNT_COLUMNS]].to_numpy().any():
            return []
        return [f"{label}: parent has counts but the child level is empty"]
    cols = ["votes", *_COUNT_COLUMNS]
    if child[key].isna().any():
        return [f"{label}: child rows without {key}"]
    line_sum = child.groupby([key, "line_key"], sort=True)["votes"].sum()
    parent_key = "geo_code"
    p_lines = parent.set_index([parent_key, "line_key"])["votes"].sort_index()
    line_sum.index = line_sum.index.set_names([parent_key, "line_key"])
    problems: list[str] = []
    if not line_sum.index.equals(p_lines.index):
        missing = p_lines.index.difference(line_sum.index)
        extra = line_sum.index.difference(p_lines.index)
        problems.append(f"{label}: geography/line sets differ (missing {len(missing)}, extra {len(extra)})")
        return problems
    diff = line_sum.to_numpy() != p_lines.to_numpy()
    if diff.any():
        problems.append(
            f"{label}: votes differ for {int(diff.sum())} geo×line rows (e.g. {p_lines.index[np.argmax(diff)]})"
        )
    child_geo = child.drop_duplicates("geo_code").groupby(key, sort=True)[list(_COUNT_COLUMNS)].sum()
    parent_geo = parent.drop_duplicates("geo_code").set_index(parent_key)[list(_COUNT_COLUMNS)].sort_index()
    child_geo.index = child_geo.index.set_names(parent_key)
    if not child_geo.index.equals(parent_geo.index):
        problems.append(f"{label}: geography sets differ")
        return problems
    for col in cols[1:]:
        if not np.array_equal(child_geo[col].to_numpy(), parent_geo[col].to_numpy()):
            problems.append(f"{label}: {col} does not sum exactly")
    return problems


def reconcile(levels: Mapping[str, pd.DataFrame]) -> list[str]:
    """Verify that every level of :func:`aggregate_levels` sums exactly to the next.

    Checks, for votes per line and valid/ballots_cast/eligible/blank/invalid:
    unit → municipality, municipality → province, unit → district and district → province
    (when present), province → national; plus per-row identities (line votes sum to valid votes,
    valid + blank + invalid = ballots cast ≤ eligible, no negative counts).

    Returns a list of human-readable problems (empty = the levels reconcile exactly).
    """
    problems: list[str] = []
    for name in ("unit", "municipality", "province", "national"):
        if name not in levels:
            problems.append(f"missing level '{name}'")
    if problems:
        return problems
    for name, df in levels.items():
        problems += _check_rows(name, df)
    problems += _check_sum(levels["unit"], levels["municipality"], "municipality_code", "unit→municipality")
    problems += _check_sum(
        levels["municipality"], levels["province"], "province_code", "municipality→province"
    )
    if "district" in levels:
        problems += _check_sum(levels["unit"], levels["district"], "district_code", "unit→district")
        problems += _check_sum(levels["district"], levels["province"], "province_code", "district→province")
    prov = levels["province"].assign(_nat=NATIONAL_CODE)
    problems += _check_sum(prov, levels["national"], "_nat", "province→national")
    return problems


def results_frame(levels: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Concatenate the level frames (in :data:`LEVELS` order) into one long DataFrame."""
    frames = [levels[name] for name in LEVELS if name in levels]
    if not frames:
        return pd.DataFrame(columns=list(LEVEL_COLUMNS))
    return pd.concat(frames, ignore_index=True)
