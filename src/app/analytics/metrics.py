"""Election metrics over the standard results frame (SIMULATED data; see docs/ANALYTICS.md).

Every function is pure (pandas/NumPy in, DataFrame or frozen dataclass out) and works on the
standard results frame of docs/ARCHITECTURE.md §7.  Shares are fractions (0–1); every quantity
whose name ends in ``_pp`` is in percentage points.

Grouping modes (``by``):

* ``"race"`` (default) — one unit of analysis per race × level × geo (``PRES-NB`` at municipality
  ``GM0855``).  Comparisons between elections match on ``race_code``.
* ``"family"`` — races of one family (``PRESIDENT`` incl. its province contests, ``HOUSE``, …) are
  pooled per level × geo, e.g. a municipality split across two House districts is analysed as a
  whole.  Comparisons match on the family.

Parties are identified by their party code; independents are pooled under
:data:`~app.analytics.results.INDEPENDENT_KEY`.

Metric groups: margins; two-party share; swing & two-party swing; flips; turnout; partisan
lean & margin lean; elasticity; uniform national swing (UNS) projection; competitiveness;
wasted votes, efficiency gap and vote efficiency; seat–vote relationship, partisan bias and
responsiveness; Electoral College efficiency, tipping point and EC bias; race summaries.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from app.analytics.results import (
    GEO_KEY,
    INDEPENDENT_KEY,
    GroupBy,
    ResultsFrameError,
    as_bool,
    at_level,
    contest_rows,
    dedupe_family_rows,
    filter_family,
    geo_totals,
    national_party_totals,
    party_shares,
    ranked_lines,
    seat_contests,
    single_election,
    with_keys,
)
from app.core.constitution import RaceType, majority_of
from app.core.logging import get_logger

log = get_logger(__name__)

#: Margin (pp) at or above which the competitiveness index is 0.
COMPETITIVENESS_THRESHOLD_PP: float = 20.0
#: Rating bands: margin (pp) strictly below the bound → rating.  Anything else is ``safe``.
RATING_BANDS: tuple[tuple[float, str], ...] = ((3.0, "tossup"), (8.0, "lean"), (15.0, "likely"))
SAFE_RATING: str = "safe"
RATINGS: tuple[str, ...] = (*(r for _, r in RATING_BANDS), SAFE_RATING)

_META = ["election_id", "year", "race_code", "race_family", "level", "geo_code", "geo_name", "province_code"]


def _cmp_cols(by: GroupBy) -> list[str]:
    """Columns matching a geo between two elections."""
    if by not in ("race", "family"):
        raise ValueError(f"by must be 'race' or 'family', got {by!r}")
    return ["race_code" if by == "race" else "race_family", "level", "geo_code"]


def _group_cols(by: GroupBy) -> list[str]:
    return ["election_id", *_cmp_cols(by)]


def _safe_div(num: np.ndarray | pd.Series, den: np.ndarray | pd.Series) -> np.ndarray:
    n = np.asarray(num, dtype=float)
    d = np.asarray(den, dtype=float)
    return np.divide(n, d, out=np.full(np.broadcast(n, d).shape, np.nan), where=d > 0)


def _same(a: np.ndarray | pd.Series, b: np.ndarray | pd.Series) -> np.ndarray:
    """Element-wise equality of two object arrays; False where either side is null."""
    sa = pd.Series(np.asarray(a, dtype=object))
    sb = pd.Series(np.asarray(b, dtype=object))
    return (sa.notna() & sb.notna() & (sa == sb)).to_numpy(dtype=bool)


def _int_or_na(values: pd.Series | np.ndarray) -> pd.api.extensions.ExtensionArray:
    """Nullable integer array (``Int64``) from float values with NaN."""
    return pd.Series(np.asarray(values, dtype=float)).round().astype("Int64").array


# =========================================================================== margins
MARGIN_COLUMNS: tuple[str, ...] = (
    "election_id",
    "year",
    "race_code",
    "race_type",
    "race_family",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "n_lines",
    "valid_votes",
    "eligible",
    "ballots_cast",
    "winner_line_key",
    "winner_candidate",
    "winner_party",
    "winner_votes",
    "winner_share",
    "runner_up_line_key",
    "runner_up_candidate",
    "runner_up_party",
    "runner_up_votes",
    "runner_up_share",
    "margin_votes",
    "margin_pp",
    "tied",
    "contested",
)


def margin_table(frame: pd.DataFrame, *, level: str | None = None) -> pd.DataFrame:
    """Top-two margin of every contest-geo (race × level × geo).

    The leader is the line with most votes; among equal votes a line flagged ``winner`` (tie
    resolved by lot) ranks first, then the lowest ``line_key``.  ``margin_pp`` = (leader share −
    runner-up share) × 100 of valid votes; an uncontested race has the runner-up at 0 votes.
    ``margin_pp`` is NaN when there are no valid votes; ``winner_*`` are null when nobody
    received a vote and no winner is flagged.  ``tied`` marks exact top-two ties.
    """
    df = at_level(frame, level)
    if df.empty:
        return pd.DataFrame(columns=list(MARGIN_COLUMNS))
    r = ranked_lines(with_keys(df))
    first = r[(r["_rank"] == 0).to_numpy()]
    second = r[(r["_rank"] == 1).to_numpy()]
    out = first[
        [
            *GEO_KEY,
            "year",
            "race_type",
            "race_family",
            "geo_name",
            "province_code",
            "_n",
            "valid_votes",
            "eligible",
            "ballots_cast",
            "line_key",
            "candidate",
            "party",
            "votes",
            "_w",
        ]
    ].rename(
        columns={
            "_n": "n_lines",
            "line_key": "winner_line_key",
            "candidate": "winner_candidate",
            "party": "winner_party",
            "votes": "winner_votes",
        }
    )
    sec = second[[*GEO_KEY, "line_key", "candidate", "party", "votes"]].rename(
        columns={
            "line_key": "runner_up_line_key",
            "candidate": "runner_up_candidate",
            "party": "runner_up_party",
            "votes": "runner_up_votes",
        }
    )
    out = out.merge(sec, on=list(GEO_KEY), how="left")
    out["runner_up_votes"] = out["runner_up_votes"].fillna(0).astype(np.int64)
    out["winner_votes"] = out["winner_votes"].astype(np.int64)
    decided = (out["winner_votes"] > 0) | out["_w"]
    for col in ("winner_line_key", "winner_candidate", "winner_party"):
        out[col] = out[col].where(decided, None)
    valid = out["valid_votes"].to_numpy(dtype=float)
    out["winner_share"] = _safe_div(out["winner_votes"], valid)
    out["runner_up_share"] = _safe_div(out["runner_up_votes"], valid)
    out["margin_votes"] = (out["winner_votes"] - out["runner_up_votes"]).astype(np.int64)
    out["margin_pp"] = 100.0 * _safe_div(out["margin_votes"], valid)
    out["tied"] = ((out["n_lines"] >= 2) & (out["winner_votes"] == out["runner_up_votes"])).to_numpy()
    out["contested"] = (out["n_lines"] >= 2).to_numpy()
    out["n_lines"] = out["n_lines"].astype(np.int64)
    out = out.sort_values(list(GEO_KEY), kind="mergesort").reset_index(drop=True)
    return out.loc[:, list(MARGIN_COLUMNS)]


def top_two_margin(votes: np.ndarray | Sequence[float]) -> np.ndarray | float:
    """Top-two margin in pp of the total for a vote vector (L,) or matrix (N, L) (row-wise).

    NaN where the total is zero; a single line has margin = its full share (100 pp).
    """
    v = np.asarray(votes, dtype=float)
    scalar = v.ndim == 1
    v2 = np.atleast_2d(v)
    if v2.shape[1] == 0:  # no lines at all
        return float("nan") if scalar else np.full(v2.shape[0], np.nan)
    total = v2.sum(axis=1)
    s = -np.sort(-v2, axis=1)
    runner = s[:, 1] if s.shape[1] > 1 else np.zeros(len(s))
    out = 100.0 * _safe_div(s[:, 0] - runner, total)
    return float(out[0]) if scalar else out


# =========================================================================== two-party share
def national_top_two(frame: pd.DataFrame) -> tuple[str, str]:
    """The two parties with most national votes (independents excluded) in a frame holding a
    single election and race family.  Ties are broken by party code."""
    nat = national_party_totals(frame)
    groups = nat[["election_id", "race_family"]].drop_duplicates()
    if len(groups) != 1:
        raise ResultsFrameError(
            f"national_top_two: expected one election and race family, found {len(groups)}; "
            "filter the frame (e.g. analytics.filters) or pass the party pair explicitly"
        )
    nat = nat[(nat["party"] != INDEPENDENT_KEY).to_numpy()]
    nat = nat.sort_values(["votes", "party"], ascending=[False, True], kind="mergesort")
    if len(nat) < 2:
        raise ResultsFrameError("national_top_two: fewer than two parties in the frame")
    return str(nat["party"].iloc[0]), str(nat["party"].iloc[1])


def _pair(frame: pd.DataFrame, party_a: str | None, party_b: str | None) -> tuple[str, str]:
    if (party_a is None) != (party_b is None):
        raise ValueError("pass both party_a and party_b, or neither (national top two)")
    if party_a is None or party_b is None:
        return national_top_two(frame)
    if party_a == party_b:
        raise ValueError("party_a and party_b must differ")
    return party_a, party_b


def two_party_share(
    frame: pd.DataFrame,
    party_a: str | None = None,
    party_b: str | None = None,
    *,
    level: str | None = None,
    by: GroupBy = "race",
) -> pd.DataFrame:
    """Two-party share of ``party_a`` vs ``party_b`` per geo.

    ``two_party_share_a = a / (a + b)`` (NaN when neither received votes); ``two_party_margin_pp
    = (a − b) / (a + b) × 100``; ``margin_pp`` = (share_a − share_b) × 100 of *all* valid votes.
    Without a pair, the national top two of the (single-election, single-family) frame are used.
    """
    a, b = _pair(frame, party_a, party_b)
    ps = party_shares(frame, level=level, by=by)
    cols = _group_cols(by)
    geo = ps.drop_duplicates(cols)[[*_META, "valid_votes"]].reset_index(drop=True)
    out = geo
    for party, name in ((a, "votes_a"), (b, "votes_b")):
        pv = ps[(ps["party"] == party).to_numpy()].groupby(cols, sort=False)["votes"].sum().rename(name)
        out = out.merge(pv.reset_index(), on=cols, how="left")
    out["votes_a"] = out["votes_a"].fillna(0).astype(np.int64)
    out["votes_b"] = out["votes_b"].fillna(0).astype(np.int64)
    out["party_a"], out["party_b"] = a, b
    pair = out["votes_a"] + out["votes_b"]
    out["share_a"] = _safe_div(out["votes_a"], out["valid_votes"])
    out["share_b"] = _safe_div(out["votes_b"], out["valid_votes"])
    out["two_party_share_a"] = _safe_div(out["votes_a"], pair)
    out["two_party_margin_pp"] = 100.0 * _safe_div(out["votes_a"] - out["votes_b"], pair)
    out["margin_pp"] = 100.0 * (out["share_a"] - out["share_b"])
    order = [
        *_META,
        "party_a",
        "party_b",
        "valid_votes",
        "votes_a",
        "votes_b",
        "share_a",
        "share_b",
        "two_party_share_a",
        "two_party_margin_pp",
        "margin_pp",
    ]
    return out.sort_values(cols, kind="mergesort").reset_index(drop=True).loc[:, order]


# =========================================================================== swing
def _meta_union(curr: pd.DataFrame, prev: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    meta = ["race_code", "race_family", "level", "geo_code", "geo_name", "province_code"]
    both = pd.concat([curr[meta], prev[meta]], ignore_index=True)
    return both.drop_duplicates(cols, keep="first").reset_index(drop=True)


def _presence(p: pd.DataFrame, c: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    gp = p[cols].drop_duplicates().assign(_in_prev=True)
    gc = c[cols].drop_duplicates().assign(_in_curr=True)
    return gp.merge(gc, on=cols, how="outer")


SWING_COLUMNS: tuple[str, ...] = (
    "election_id_prev",
    "year_prev",
    "election_id_curr",
    "year_curr",
    "race_code",
    "race_family",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "party",
    "votes_prev",
    "votes_curr",
    "vote_change",
    "vote_change_pct",
    "share_prev",
    "share_curr",
    "swing_pp",
    "status",
)


def swing(
    prev: pd.DataFrame,
    curr: pd.DataFrame,
    *,
    level: str | None = None,
    by: GroupBy = "race",
    parties: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Share swing and vote change per geo × party between two elections.

    ``swing_pp = (share_curr − share_prev) × 100``; ``vote_change = votes_curr − votes_prev``;
    ``vote_change_pct`` relative to ``votes_prev``.  ``status``:

    * ``both`` — party ran at the geo in both elections;
    * ``new_party`` / ``dropped_party`` — the geo existed in both but the party ran in only one
      (its missing share counts as 0);
    * ``new_geo`` / ``dropped_geo`` — the geo exists in only one election (previous or current
      values are NaN; e.g. a municipality created by a merger — use
      :func:`app.analytics.history.remap_lineage` first).
    """
    e_prev, y_prev = single_election(prev, "swing(prev)")
    e_curr, y_curr = single_election(curr, "swing(curr)")
    p = party_shares(prev, level=level, by=by)
    c = party_shares(curr, level=level, by=by)
    cols = _cmp_cols(by)
    key = [*cols, "party"]
    m = c[[*key, "votes", "share"]].merge(
        p[[*key, "votes", "share"]], on=key, how="outer", suffixes=("_curr", "_prev")
    )
    m = m.merge(_presence(p, c, cols), on=cols, how="left")
    in_prev = as_bool(m["_in_prev"]).to_numpy()
    in_curr = as_bool(m["_in_curr"]).to_numpy()
    absent_prev = m["votes_prev"].isna().to_numpy()
    absent_curr = m["votes_curr"].isna().to_numpy()
    for side, present, absent in (("prev", in_prev, absent_prev), ("curr", in_curr, absent_curr)):
        votes = m[f"votes_{side}"].to_numpy(dtype=float)
        share = m[f"share_{side}"].to_numpy(dtype=float)
        m[f"votes_{side}"] = np.where(present, np.nan_to_num(votes, nan=0.0), np.nan)
        m[f"share_{side}"] = np.where(present, np.where(absent, 0.0, share), np.nan)
    m["status"] = np.select(
        [~in_prev, ~in_curr, absent_prev, absent_curr],
        ["new_geo", "dropped_geo", "new_party", "dropped_party"],
        default="both",
    )
    vp = m["votes_prev"].to_numpy(dtype=float)
    vc = m["votes_curr"].to_numpy(dtype=float)
    m["vote_change"] = vc - vp
    m["vote_change_pct"] = 100.0 * _safe_div(vc - vp, vp)
    m["swing_pp"] = 100.0 * (m["share_curr"].to_numpy(dtype=float) - m["share_prev"].to_numpy(dtype=float))
    for col in ("votes_prev", "votes_curr", "vote_change"):
        m[col] = _int_or_na(m[col])
    m = m.merge(_meta_union(c, p, cols), on=cols, how="left")
    m["election_id_prev"], m["year_prev"] = e_prev, y_prev
    m["election_id_curr"], m["year_curr"] = e_curr, y_curr
    if parties is not None:
        m = m[m["party"].isin(list(parties)).to_numpy()]
    m = m.sort_values(key, kind="mergesort").reset_index(drop=True)
    return m.loc[:, list(SWING_COLUMNS)]


def two_party_swing(
    prev: pd.DataFrame,
    curr: pd.DataFrame,
    party_a: str | None = None,
    party_b: str | None = None,
    *,
    level: str | None = None,
    by: GroupBy = "race",
) -> pd.DataFrame:
    """Swing of ``party_a``'s two-party share against ``party_b`` per geo (pp).

    Without a pair, the current election's national top two are used for both elections.
    Geos present in only one election get NaN swing.
    """
    a, b = _pair(curr, party_a, party_b)
    cols = _cmp_cols(by)
    tp = two_party_share(prev, a, b, level=level, by=by)
    tc = two_party_share(curr, a, b, level=level, by=by)
    keep = [*cols, "two_party_share_a", "two_party_margin_pp"]
    m = tc[keep].merge(tp[keep], on=cols, how="outer", suffixes=("_curr", "_prev"))
    m = m.merge(_meta_union(tc, tp, cols), on=cols, how="left")
    m["party_a"], m["party_b"] = a, b
    m["two_party_swing_pp"] = 100.0 * (m["two_party_share_a_curr"] - m["two_party_share_a_prev"])
    m["margin_change_pp"] = m["two_party_margin_pp_curr"] - m["two_party_margin_pp_prev"]
    order = [
        "race_code",
        "race_family",
        "level",
        "geo_code",
        "geo_name",
        "province_code",
        "party_a",
        "party_b",
        "two_party_share_a_prev",
        "two_party_share_a_curr",
        "two_party_swing_pp",
        "two_party_margin_pp_prev",
        "two_party_margin_pp_curr",
        "margin_change_pp",
    ]
    return m.sort_values(cols, kind="mergesort").reset_index(drop=True).loc[:, order]


def national_shares(frame: pd.DataFrame, *, race_type: str | RaceType | None = None) -> pd.Series:
    """National share per party (index: party) of a single-election, single-family frame."""
    nat = national_party_totals(filter_family(frame, race_type))
    if len(nat[["election_id", "race_family"]].drop_duplicates()) != 1:
        raise ResultsFrameError("national_shares: frame must hold exactly one election and race family")
    return nat.set_index("party")["share"].rename("share")


def national_swing(
    prev: pd.DataFrame, curr: pd.DataFrame, *, race_type: str | RaceType | None = None
) -> pd.Series:
    """National swing per party in pp (index: party).  A party present in only one election
    counts as 0 % in the other."""
    sp = national_shares(prev, race_type=race_type)
    sc = national_shares(curr, race_type=race_type)
    out = sc.sub(sp, fill_value=0.0) * 100.0
    return out.sort_index().rename("swing_pp")


def swing_ratio(
    geo_swing: np.ndarray | float, nat_swing: np.ndarray | float, min_national_swing: float = 1e-3
) -> np.ndarray:
    """Two-election elasticity: geo swing / national swing (same units), NaN when the national
    swing is smaller in magnitude than ``min_national_swing``."""
    g = np.asarray(geo_swing, dtype=float)
    n = np.broadcast_to(np.asarray(nat_swing, dtype=float), g.shape)
    return np.divide(g, n, out=np.full(g.shape, np.nan), where=np.abs(n) >= min_national_swing)


# =========================================================================== winners & flips
def winners(frame: pd.DataFrame, *, level: str | None = None, by: GroupBy = "race") -> pd.DataFrame:
    """Winner per geo: ``winner_party``, ``winner_candidate`` (race mode), ``runner_up_party`` and
    ``margin_pp``.  Race mode uses the line-level ranking of :func:`margin_table`; family mode the
    plurality of pooled party votes."""
    if by == "race":
        mt = margin_table(frame, level=level)
        return mt[[*_META, "winner_party", "winner_candidate", "runner_up_party", "margin_pp"]].copy()
    ps = party_shares(frame, level=level, by="family")
    cols = _group_cols("family")
    ps = ps.sort_values(
        [*cols, "won", "votes", "party"],
        ascending=[True] * len(cols) + [False, False, True],
        kind="mergesort",
    )
    rank = ps.groupby(cols, sort=False).cumcount()
    first = ps[(rank == 0).to_numpy()]
    second = ps[(rank == 1).to_numpy()][[*cols, "party", "share"]].rename(
        columns={"party": "runner_up_party", "share": "_s2"}
    )
    out = first.merge(second, on=cols, how="left")
    out["winner_party"] = out["party"].where(as_bool(out["won"]), None)
    out["winner_candidate"] = None
    s2 = out["_s2"].fillna(0.0).to_numpy(dtype=float)
    out["margin_pp"] = 100.0 * (out["share"].to_numpy(dtype=float) - s2)
    return out[[*_META, "winner_party", "winner_candidate", "runner_up_party", "margin_pp"]].reset_index(
        drop=True
    )


FLIP_COLUMNS: tuple[str, ...] = (
    "election_id_prev",
    "year_prev",
    "election_id_curr",
    "year_curr",
    "race_code",
    "race_family",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "winner_prev",
    "winner_curr",
    "candidate_prev",
    "candidate_curr",
    "margin_prev_pp",
    "margin_curr_pp",
    "status",
    "flipped",
    "gained_by",
    "lost_by",
)


def flips(
    prev: pd.DataFrame, curr: pd.DataFrame, *, level: str | None = None, by: GroupBy = "race"
) -> pd.DataFrame:
    """Hold / flip of the winning party per geo between two elections.

    ``status``: ``hold`` (same winning party), ``flip`` (different party; ``gained_by`` /
    ``lost_by`` set), ``new`` (geo only in the current election), ``dropped`` (only in the
    previous one) or ``undecided`` (no winner in one of the elections).
    """
    e_prev, y_prev = single_election(prev, "flips(prev)")
    e_curr, y_curr = single_election(curr, "flips(curr)")
    cols = _cmp_cols(by)
    wp = winners(prev, level=level, by=by)
    wc = winners(curr, level=level, by=by)
    keep = [*cols, "winner_party", "winner_candidate", "margin_pp"]
    m = wc[keep].merge(wp[keep], on=cols, how="outer", suffixes=("_curr", "_prev"), indicator=True)
    m = m.merge(_meta_union(wc, wp, cols), on=cols, how="left")
    ind = m["_merge"].astype(str).to_numpy()
    w_prev = m["winner_party_prev"].to_numpy(dtype=object)
    w_curr = m["winner_party_curr"].to_numpy(dtype=object)
    undecided = pd.isna(w_prev) | pd.isna(w_curr)
    same = _same(w_prev, w_curr)
    status = np.select(
        [ind == "left_only", ind == "right_only", undecided, same],
        ["new", "dropped", "undecided", "hold"],
        default="flip",
    )
    m["status"] = status
    m["flipped"] = status == "flip"
    m["gained_by"] = np.where(m["flipped"], w_curr, None)
    m["lost_by"] = np.where(m["flipped"], w_prev, None)
    m = m.rename(
        columns={
            "winner_party_prev": "winner_prev",
            "winner_party_curr": "winner_curr",
            "winner_candidate_prev": "candidate_prev",
            "winner_candidate_curr": "candidate_curr",
            "margin_pp_prev": "margin_prev_pp",
            "margin_pp_curr": "margin_curr_pp",
        }
    )
    m["election_id_prev"], m["year_prev"] = e_prev, y_prev
    m["election_id_curr"], m["year_curr"] = e_curr, y_curr
    return m.sort_values(cols, kind="mergesort").reset_index(drop=True).loc[:, list(FLIP_COLUMNS)]


def flip_summary(flips_frame: pd.DataFrame, *, weights: Mapping[str, float] | None = None) -> pd.DataFrame:
    """Per party: wins in each election, gains, losses, holds and net change.

    ``weights`` (geo code → weight, e.g. electoral votes by province) weights every geo; default 1.
    Geos with status ``new``/``dropped`` count toward the wins of their election only.
    """
    f = flips_frame
    w = (
        f["geo_code"].map(lambda g: float(weights[g]) if g in weights else np.nan).to_numpy(dtype=float)
        if weights is not None
        else np.ones(len(f))
    )
    if np.isnan(w).any():
        missing = sorted(set(f.loc[np.isnan(w), "geo_code"]))
        raise ValueError(f"flip_summary: no weight for geos {missing}")
    rows = pd.DataFrame(
        {
            "party": pd.concat(
                [f["winner_prev"], f["winner_curr"], f["gained_by"], f["lost_by"], f["winner_curr"]],
                ignore_index=True,
            ),
            "kind": np.repeat(["wins_prev", "wins_curr", "gains", "losses", "holds"], len(f)),
            "w": np.tile(w, 5),
            "_keep": np.concatenate([np.ones(4 * len(f), dtype=bool), (f["status"] == "hold").to_numpy()]),
        }
    )
    rows = rows[rows["party"].notna().to_numpy() & rows["_keep"].to_numpy()]
    cols = ["wins_prev", "wins_curr", "gains", "losses", "holds"]
    if rows.empty:
        return pd.DataFrame(columns=["party", *cols, "net"])
    out = rows.pivot_table(index="party", columns="kind", values="w", aggfunc="sum", fill_value=0.0)
    out = out.reindex(columns=cols, fill_value=0.0)
    out["net"] = out["wins_curr"] - out["wins_prev"]
    out = out.reset_index().sort_values(["wins_curr", "party"], ascending=[False, True], kind="mergesort")
    out.columns.name = None
    if weights is None:
        for c in [*cols, "net"]:
            out[c] = out[c].astype(np.int64)
    return out.reset_index(drop=True)


# =========================================================================== turnout
TURNOUT_COLUMNS: tuple[str, ...] = (
    *_META,
    "eligible",
    "ballots_cast",
    "valid_votes",
    "blank_or_invalid",
    "turnout",
    "turnout_pct",
)


def turnout(frame: pd.DataFrame, *, level: str | None = None, by: GroupBy = "race") -> pd.DataFrame:
    """Turnout per geo: ``turnout = ballots_cast / eligible`` (NaN without eligible voters),
    ``turnout_pct`` = × 100, ``blank_or_invalid = ballots_cast − valid_votes``."""
    gt = geo_totals(frame, level=level, by=by)
    gt["blank_or_invalid"] = (gt["ballots_cast"] - gt["valid_votes"]).astype(np.int64)
    gt["turnout"] = _safe_div(gt["ballots_cast"], gt["eligible"])
    gt["turnout_pct"] = 100.0 * gt["turnout"]
    return (
        gt.sort_values(_group_cols(by), kind="mergesort").reset_index(drop=True).loc[:, list(TURNOUT_COLUMNS)]
    )


def turnout_change(
    prev: pd.DataFrame, curr: pd.DataFrame, *, level: str | None = None, by: GroupBy = "race"
) -> pd.DataFrame:
    """Turnout in both elections and its change (pp) per geo; ``status`` ∈ {both, new_geo, dropped_geo}."""
    single_election(prev, "turnout_change(prev)")
    single_election(curr, "turnout_change(curr)")
    cols = _cmp_cols(by)
    tp = turnout(prev, level=level, by=by)
    tc = turnout(curr, level=level, by=by)
    keep = [*cols, "eligible", "ballots_cast", "turnout"]
    m = tc[keep].merge(tp[keep], on=cols, how="outer", suffixes=("_curr", "_prev"), indicator=True)
    m = m.merge(_meta_union(tc, tp, cols), on=cols, how="left")
    ind = m.pop("_merge").astype(str).to_numpy()
    m["status"] = np.select(
        [ind == "left_only", ind == "right_only"], ["new_geo", "dropped_geo"], default="both"
    )
    m["turnout_change_pp"] = 100.0 * (m["turnout_curr"] - m["turnout_prev"])
    m["ballots_change"] = _int_or_na(
        m["ballots_cast_curr"].astype(float) - m["ballots_cast_prev"].astype(float)
    )
    for col in ("eligible_prev", "eligible_curr", "ballots_cast_prev", "ballots_cast_curr"):
        m[col] = _int_or_na(m[col])
    order = [
        "race_code",
        "race_family",
        "level",
        "geo_code",
        "geo_name",
        "province_code",
        "eligible_prev",
        "eligible_curr",
        "ballots_cast_prev",
        "ballots_cast_curr",
        "ballots_change",
        "turnout_prev",
        "turnout_curr",
        "turnout_change_pp",
        "status",
    ]
    return m.sort_values(cols, kind="mergesort").reset_index(drop=True).loc[:, order]


# =========================================================================== lean
def partisan_lean(
    frame: pd.DataFrame,
    *,
    level: str | None = None,
    by: GroupBy = "race",
    parties: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Partisan lean per geo × party: ``lean_pp = (geo share − national share) × 100``.

    National shares are per election and race family (:func:`national_party_totals`).  Only
    parties that ran at the geo are listed (a party absent from a district has no lean there).
    """
    ps = party_shares(frame, level=level, by=by)
    nat = national_party_totals(frame)[["election_id", "race_family", "party", "share"]].rename(
        columns={"share": "national_share"}
    )
    out = ps.merge(nat, on=["election_id", "race_family", "party"], how="left")
    out["lean_pp"] = 100.0 * (out["share"] - out["national_share"])
    if parties is not None:
        out = out[out["party"].isin(list(parties)).to_numpy()]
    cols = [*_META, "party", "votes", "share", "national_share", "lean_pp"]
    return out.sort_values([*_group_cols(by), "party"], kind="mergesort").reset_index(drop=True).loc[:, cols]


def margin_lean(
    frame: pd.DataFrame,
    party_a: str | None = None,
    party_b: str | None = None,
    *,
    level: str | None = None,
    by: GroupBy = "race",
    two_party: bool = True,
) -> pd.DataFrame:
    """Margin-based lean of a party pair: ``lean_pp = geo margin(a − b) − national margin(a − b)``.

    With ``two_party`` the margins are two-party margins ``(a − b)/(a + b)``; otherwise raw share
    differences.  Positive values lean toward ``party_a``; ``leans`` names the favoured party
    (``even`` for exactly 0, null when undefined).
    """
    a, b = _pair(frame, party_a, party_b)
    tp = two_party_share(frame, a, b, level=level, by=by)
    nat = national_party_totals(frame)
    va = nat[(nat["party"] == a).to_numpy()].set_index(["election_id", "race_family"])["votes"]
    vb = nat[(nat["party"] == b).to_numpy()].set_index(["election_id", "race_family"])["votes"]
    tot = nat.drop_duplicates(["election_id", "race_family"]).set_index(["election_id", "race_family"])[
        "valid_votes"
    ]
    n = pd.DataFrame({"va": va, "vb": vb, "tot": tot}).fillna(0.0)
    if two_party:
        n["national_margin_pp"] = 100.0 * _safe_div(n["va"] - n["vb"], n["va"] + n["vb"])
    else:
        n["national_margin_pp"] = 100.0 * _safe_div(n["va"] - n["vb"], n["tot"])
    out = tp.merge(n[["national_margin_pp"]].reset_index(), on=["election_id", "race_family"], how="left")
    out["geo_margin_pp"] = out["two_party_margin_pp"] if two_party else out["margin_pp"]
    out["lean_pp"] = out["geo_margin_pp"] - out["national_margin_pp"]
    lean = out["lean_pp"].to_numpy(dtype=float)
    out["leans"] = np.where(np.isnan(lean), None, np.where(lean > 0, a, np.where(lean < 0, b, "even")))
    cols = [*_META, "party_a", "party_b", "geo_margin_pp", "national_margin_pp", "lean_pp", "leans"]
    return out.loc[:, cols]


# =========================================================================== elasticity
ELASTICITY_COLUMNS: tuple[str, ...] = (
    "race_code",
    "race_family",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "party",
    "n_obs",
    "elasticity",
    "intercept",
    "r_squared",
    "method",
    "mean_share",
    "mean_national_share",
)


def _masked_ols(X: np.ndarray, Y: np.ndarray, min_range: float) -> dict[str, np.ndarray]:
    """Row-wise OLS of Y on X with NaN masking.  X, Y: (N, T)."""
    m = ~np.isnan(X) & ~np.isnan(Y)
    n = m.sum(axis=1)
    nf = np.maximum(n, 1).astype(float)
    Xm = np.where(m, X, 0.0)
    Ym = np.where(m, Y, 0.0)
    mx = Xm.sum(axis=1) / nf
    my = Ym.sum(axis=1) / nf
    dx = np.where(m, X - mx[:, None], 0.0)
    dy = np.where(m, Y - my[:, None], 0.0)
    sxx = (dx * dx).sum(axis=1)
    sxy = (dx * dy).sum(axis=1)
    syy = (dy * dy).sum(axis=1)
    xr = np.where(m, X, -np.inf).max(axis=1) - np.where(m, X, np.inf).min(axis=1)
    ok = (n >= 2) & (xr >= min_range) & (sxx > 0)
    slope = np.divide(sxy, sxx, out=np.full(len(n), np.nan), where=ok)
    intercept = np.where(ok, my - slope * mx, np.nan)
    r2_ok = ok & (n >= 3) & (syy > 0)
    r2 = np.divide(sxy * sxy, sxx * syy, out=np.full(len(n), np.nan), where=r2_ok)
    return {
        "n": n,
        "slope": slope,
        "intercept": intercept,
        "r2": r2,
        "mx": np.where(n > 0, mx, np.nan),
        "my": np.where(n > 0, my, np.nan),
    }


def elasticity(
    frame: pd.DataFrame,
    *,
    level: str,
    by: GroupBy = "race",
    parties: Sequence[str] | None = None,
    min_national_range: float = 1e-3,
) -> pd.DataFrame:
    """Elasticity of each geo's party share with respect to the party's national share.

    ``frame`` holds ≥ 2 elections.  Per geo × party the slope of geo share on national share is
    estimated by OLS over the elections in which the party ran at the geo (``method = 'ols'``,
    ≥ 3 observations, with ``r_squared``); with exactly two observations it is the ratio of swings
    (``method = 'swing_ratio'``).  Elasticity 1 means the geo moves one-for-one with the nation.
    NaN when the national share varies by less than ``min_national_range`` (share units).
    """
    ps = party_shares(frame, level=level, by=by)
    nat = national_party_totals(frame)[["election_id", "race_family", "party", "share"]].rename(
        columns={"share": "national_share"}
    )
    ps = ps.merge(nat, on=["election_id", "race_family", "party"], how="left")
    if parties is not None:
        ps = ps[ps["party"].isin(list(parties)).to_numpy()]
    cols = _cmp_cols(by)
    idx = [*cols, "party"]
    if ps.empty:
        return pd.DataFrame(columns=list(ELASTICITY_COLUMNS))
    wide = ps.set_index([*idx, "election_id"])[["share", "national_share"]].unstack("election_id")
    Y = wide["share"]
    X = wide["national_share"].reindex(columns=Y.columns)
    fit = _masked_ols(X.to_numpy(dtype=float), Y.to_numpy(dtype=float), min_national_range)
    out = Y.index.to_frame(index=False)
    out["n_obs"] = fit["n"].astype(np.int64)
    out["elasticity"] = fit["slope"]
    out["intercept"] = fit["intercept"]
    out["r_squared"] = fit["r2"]
    out["method"] = np.where(fit["n"] == 2, "swing_ratio", np.where(fit["n"] >= 3, "ols", "insufficient"))
    out["mean_share"] = fit["my"]
    out["mean_national_share"] = fit["mx"]
    last = ps.sort_values("year", kind="mergesort").drop_duplicates(cols, keep="last")
    meta = last[["race_code", "race_family", "level", "geo_code", "geo_name", "province_code"]]
    out = out.merge(meta, on=cols, how="left")
    out = out.sort_values(idx, kind="mergesort").reset_index(drop=True)
    return out.loc[:, list(ELASTICITY_COLUMNS)]


def elasticity_from_draws(
    geo_shares: np.ndarray, national_shares: np.ndarray, *, min_national_range: float = 1e-6
) -> np.ndarray:
    """Elasticity from simulation draws: slope of each geo's share on the national share.

    ``geo_shares`` (S, G) and ``national_shares`` (S,) for one party over S draws → (G,) slopes
    (``cov(geo, nat) / var(nat)``); NaN when the national share varies by less than
    ``min_national_range`` across draws.
    """
    Y = np.asarray(geo_shares, dtype=float)
    x = np.asarray(national_shares, dtype=float)
    if Y.ndim != 2 or x.ndim != 1 or Y.shape[0] != x.shape[0]:
        raise ValueError("geo_shares must be (S, G) and national_shares (S,)")
    dx = x - x.mean()
    sxx = float(dx @ dx)
    if Y.shape[0] < 2 or sxx <= 0 or float(np.ptp(x)) < min_national_range:
        return np.full(Y.shape[1], np.nan)
    return (dx @ (Y - Y.mean(axis=0))) / sxx


# =========================================================================== uniform swing
@dataclass(frozen=True)
class UNSProjection:
    """Result of a uniform-national-swing projection.

    * ``shares`` — per contest × party: ``share_prev``, ``swing_pp`` applied, ``share_projected``;
    * ``contests`` — per contest: previous and projected winner, projected margin, ``flipped``,
      ``weight`` (seats / electoral votes the contest is worth);
    * ``seats`` — per race family × party: ``seats_prev``, ``seats_projected``, ``change`` (seats
      of different families — House districts, EV contests, governors — are never added up).
    """

    shares: pd.DataFrame
    contests: pd.DataFrame
    seats: pd.DataFrame


def _weights_for(codes: pd.Series, seat_weights: Mapping[str, float] | None) -> np.ndarray:
    if seat_weights is None:
        return np.ones(len(codes))
    w = codes.map(lambda g: float(seat_weights[g]) if g in seat_weights else np.nan).to_numpy(dtype=float)
    if np.isnan(w).any():
        missing = sorted(set(codes[np.isnan(w)]))
        raise ValueError(f"no seat weight for contests {missing}")
    return w


def _share_matrix(base: pd.DataFrame) -> tuple[pd.DataFrame, list[str], np.ndarray, pd.DataFrame]:
    """(contest index frame, party list, share matrix (N, P), party-shares long frame)."""
    ps = party_shares(base, by="race")
    cols = list(GEO_KEY)
    wide = ps.set_index([*cols, "party"])["share"].unstack("party", fill_value=0.0).fillna(0.0)
    wide = wide.reindex(columns=sorted(wide.columns))
    parties = [str(p) for p in wide.columns]
    idx = wide.index.to_frame(index=False)
    return idx, parties, wide.to_numpy(dtype=float), ps


@dataclass(frozen=True)
class _Lines:
    """Ballot lines of the contests of a share matrix (see :func:`_contest_lines`)."""

    contest: np.ndarray  # (L,) row of the share matrix
    party: np.ndarray  # (L,) column of the share matrix
    share: np.ndarray  # (L,) share of valid votes (0 without valid votes)
    flag: np.ndarray  # (L,) flagged winner (e.g. an exact tie won by lot)
    order: np.ndarray  # (L,) rank of the line key (last tie-break)
    count: np.ndarray  # (N, P) number of lines per contest × party


def _contest_lines(base: pd.DataFrame, idx: pd.DataFrame, parties: list[str]) -> _Lines:
    r = with_keys(base)
    keys = pd.MultiIndex.from_frame(idx[list(GEO_KEY)])
    contest = keys.get_indexer(pd.MultiIndex.from_frame(r[list(GEO_KEY)]))
    party = pd.Index(parties).get_indexer(r["party"])
    ok = (contest >= 0) & (party >= 0)
    share = np.nan_to_num(_safe_div(r["votes"], r["valid_votes"]))[ok]
    order = pd.factorize(r["line_key"].astype(str), sort=True)[0][ok]
    count = np.zeros((len(idx), len(parties)), dtype=np.int64)
    np.add.at(count, (contest[ok], party[ok]), 1)
    flag = as_bool(r["winner"]).to_numpy()[ok]
    return _Lines(contest[ok], party[ok], share, flag, order, count)


def _project_winners(
    lines: _Lines, S: np.ndarray, P: np.ndarray, parties: list[str]
) -> tuple[np.ndarray, np.ndarray]:
    """Projected winner (party key; None when nobody has a positive share) and top-two margin (pp)
    per contest, decided between *lines*: each line keeps its fraction of its party's share
    (pooled independents stay separate candidates); a party projected into a contest where it has
    no line counts as one line.  Exact ties go to the flagged winner, then the lowest line key, so
    a zero swing reproduces the previous winners exactly."""
    n = S.shape[0]
    names = np.full(n, None, dtype=object)
    margins = np.full(n, np.nan)
    c, j = lines.contest, lines.party
    sp, pp = S[c, j], P[c, j]
    value = lines.share * np.divide(pp, sp, out=np.zeros(len(c)), where=sp > 0)
    # a party whose lines received no votes but which gains a share splits it equally
    value = np.where((sp <= 0) & (pp > 0), pp / np.maximum(lines.count[c, j], 1), value)
    vc, vj = np.nonzero((lines.count == 0) & (P > 0))
    contest = np.concatenate([c, vc])
    party = np.concatenate([j, vj])
    value = np.concatenate([value, P[vc, vj]])
    flag = np.concatenate([lines.flag, np.zeros(len(vc), dtype=bool)])
    order = np.concatenate([lines.order, int(lines.order.max(initial=-1)) + 1 + vj])
    if not len(contest):
        return names, margins
    srt = np.lexsort((order, ~flag, -value, contest))
    cs, vs, ps = contest[srt], value[srt], party[srt]
    first = np.flatnonzero(np.r_[True, cs[1:] != cs[:-1]])
    nxt = np.minimum(first + 1, len(cs) - 1)
    second = np.where((first + 1 < len(cs)) & (cs[nxt] == cs[first]), vs[nxt], 0.0)
    top = vs[first]
    ok = top > 0
    names[cs[first][ok]] = np.asarray(parties, dtype=object)[ps[first][ok]]
    margins[cs[first][ok]] = 100.0 * (top[ok] - second[ok])
    return names, margins


def uniform_swing_projection(
    prev: pd.DataFrame,
    national_swing_pp: Mapping[str, float] | pd.Series,
    *,
    level: str | None = None,
    seat_weights: Mapping[str, float] | None = None,
    renormalize: bool = True,
    contested_only: bool = True,
) -> UNSProjection:
    """Project winners and seats by applying a uniform national swing to previous results.

    Every party's share in every contest moves by its national swing (pp; parties without a
    swing keep their share).  With ``contested_only`` (default) a party gets no swing where it did
    not run.  Shares are clipped at 0 and, with ``renormalize``, rescaled so each contest's shares
    keep their original sum.  Contests are the seat contests of ``prev`` (House districts,
    province EV contests, …) or all race-geos at ``level`` (where the ``PRES`` parent and a
    ``PRES-<PV>`` contest cover the same geo it counts once, see
    :func:`~app.analytics.results.dedupe_family_rows`).  ``seat_weights`` maps geo codes to the
    seats a contest is worth (e.g. electoral votes by province); default 1.

    The projected winner is decided between ballot *lines*: each line keeps its fraction of its
    party's projected share, so independents pooled under ``_IND`` do not win as a bloc; exact ties
    go to the line flagged ``winner`` (a tie won by lot), then the lowest line key — a zero swing
    reproduces the previous winners exactly.
    """
    base = seat_contests(prev) if level is None else dedupe_family_rows(at_level(prev, level))
    if base.empty:
        raise ResultsFrameError("uniform_swing_projection: no contest rows in the previous results")
    idx, parties, S, ps = _share_matrix(base)
    lines = _contest_lines(base, idx, parties)
    sw = pd.Series(national_swing_pp, dtype=float)
    unknown = sorted(set(sw.index.astype(str)) - set(parties))
    if unknown:
        log.debug("uniform_swing_projection: swing for parties absent from prev ignored: %s", unknown)
    swing_vec = np.array([float(sw.get(p, 0.0)) for p in parties]) / 100.0
    mask = (S > 0) if contested_only else np.ones_like(S, dtype=bool)
    P = np.clip(S + mask * swing_vec[None, :], 0.0, None)
    if renormalize:
        tot0, tot1 = S.sum(axis=1), P.sum(axis=1)
        scale = np.divide(tot0, tot1, out=np.ones_like(tot0), where=tot1 > 0)
        P = P * scale[:, None]
    proj_w, proj_m = _project_winners(lines, S, P, parties)
    won = ps[as_bool(ps["won"]).to_numpy()][[*GEO_KEY, "party"]].rename(columns={"party": "winner_prev"})
    meta = ps.drop_duplicates(list(GEO_KEY))[[*GEO_KEY, "year", "race_family", "geo_name", "province_code"]]
    contests = idx.merge(meta, on=list(GEO_KEY), how="left").merge(won, on=list(GEO_KEY), how="left")
    contests["winner_projected"] = proj_w
    contests["margin_projected_pp"] = proj_m
    wp = contests["winner_prev"].to_numpy(dtype=object)
    contests["flipped"] = ~pd.isna(wp) & ~pd.isna(proj_w) & ~_same(wp, proj_w)
    contests["weight"] = _weights_for(contests["geo_code"], seat_weights)
    contests = contests.sort_values(list(GEO_KEY), kind="mergesort").reset_index(drop=True)

    shares = pd.DataFrame(
        {
            **{c: np.repeat(idx[c].to_numpy(), len(parties)) for c in GEO_KEY},
            "party": np.tile(np.asarray(parties, dtype=object), len(idx)),
            "share_prev": S.ravel(),
            "swing_pp": np.tile(swing_vec * 100.0, len(idx)) * mask.ravel(),
            "share_projected": P.ravel(),
        }
    )
    shares = shares[(shares["share_prev"] > 0).to_numpy() | (shares["share_projected"] > 0).to_numpy()]
    shares = shares.sort_values([*GEO_KEY, "party"], kind="mergesort").reset_index(drop=True)

    seat_rows = pd.concat(
        [
            pd.DataFrame(
                {
                    "race_family": contests["race_family"].to_numpy(dtype=object),
                    "party": contests[col].to_numpy(dtype=object),
                    "kind": kind,
                    "w": contests["weight"].to_numpy(dtype=float),
                }
            )
            for col, kind in (("winner_prev", "seats_prev"), ("winner_projected", "seats_projected"))
        ],
        ignore_index=True,
    )
    seat_rows = seat_rows[seat_rows["party"].notna().to_numpy()]
    if seat_rows.empty:  # nobody received a vote anywhere
        seat_rows = pd.DataFrame(
            {"race_family": pd.Series(dtype=object), "party": pd.Series(dtype=object), "kind": "", "w": 0.0}
        )
    seats = seat_rows.pivot_table(
        index=["race_family", "party"], columns="kind", values="w", aggfunc="sum", fill_value=0.0
    )
    seats = seats.reindex(columns=["seats_prev", "seats_projected"], fill_value=0.0)
    seats.columns.name = None
    seats = seats.reset_index()
    seats["change"] = seats["seats_projected"] - seats["seats_prev"]
    seats = seats.sort_values(
        ["race_family", "seats_projected", "party"], ascending=[True, False, True], kind="mergesort"
    )
    if seat_weights is None or all(float(v).is_integer() for v in seat_weights.values()):
        for c in ("seats_prev", "seats_projected", "change"):
            seats[c] = seats[c].round().astype(np.int64)
    seats = seats.loc[:, ["race_family", "party", "seats_prev", "seats_projected", "change"]]
    return UNSProjection(shares=shares, contests=contests, seats=seats.reset_index(drop=True))


# =========================================================================== competitiveness
def rating_for_margin(margin_pp: np.ndarray | pd.Series | float) -> np.ndarray:
    """Rating (``tossup``/``lean``/``likely``/``safe``) for top-two margins in pp; null for NaN."""
    m = np.abs(np.atleast_1d(np.asarray(margin_pp, dtype=float)))
    conds = [m < bound for bound, _ in RATING_BANDS]
    out = np.select(conds, [name for _, name in RATING_BANDS], default=SAFE_RATING).astype(object)
    out[np.isnan(m)] = None
    return out


def probability_competitiveness(win_probability: np.ndarray | float) -> np.ndarray:
    """Competitiveness from a leader's win probability: ``1 − |2p − 1|`` (1 = coin flip, 0 = certain)."""
    p = np.clip(np.asarray(win_probability, dtype=float), 0.0, 1.0)
    return 1.0 - np.abs(2.0 * p - 1.0)


def competitiveness(
    frame: pd.DataFrame,
    *,
    level: str | None = None,
    threshold_pp: float = COMPETITIVENESS_THRESHOLD_PP,
) -> pd.DataFrame:
    """Competitiveness of every race (default: at its jurisdiction level) or of every geo at ``level``.

    ``competitiveness = clip(1 − margin_pp / threshold_pp, 0, 1)`` (1 = dead heat, 0 = margin ≥
    threshold), ``rating`` from :data:`RATING_BANDS`, and ``enc`` — the effective number of
    candidates ``1 / Σ sᵢ²`` (Laakso–Taagepera) which shows multi-way fragmentation that a top-two
    margin hides.
    """
    if threshold_pp <= 0:
        raise ValueError("threshold_pp must be positive")
    base = contest_rows(frame) if level is None else at_level(frame, level)
    mt = margin_table(base)
    if mt.empty:
        return mt.assign(enc=[], competitiveness=[], rating=[])
    valid = base["valid_votes"].to_numpy(dtype=float)
    s = _safe_div(base["votes"], valid)
    sq = pd.Series(np.nan_to_num(s) ** 2).groupby([base[c].to_numpy() for c in GEO_KEY]).sum()
    sq.index.names = list(GEO_KEY)
    mt = mt.merge(sq.rename("_hhi").reset_index(), on=list(GEO_KEY), how="left")
    mt["enc"] = _safe_div(1.0, mt["_hhi"])
    margin = mt["margin_pp"].to_numpy(dtype=float)
    mt["competitiveness"] = np.where(np.isnan(margin), np.nan, np.clip(1.0 - margin / threshold_pp, 0.0, 1.0))
    mt["rating"] = rating_for_margin(margin)
    return mt.drop(columns="_hhi")


def competitiveness_summary(comp: pd.DataFrame, by: str | Sequence[str] = "province_code") -> pd.DataFrame:
    """Aggregate a :func:`competitiveness` table (e.g. per province, race type or election):
    number of contests, mean index, median margin, counts per rating and mean ENC."""
    keys = [by] if isinstance(by, str) else list(by)
    g = comp.groupby(keys, sort=True, dropna=False)
    out = g.agg(
        n_contests=("margin_pp", "size"),
        mean_competitiveness=("competitiveness", "mean"),
        median_margin_pp=("margin_pp", "median"),
        mean_enc=("enc", "mean"),
    )
    counts = pd.DataFrame(
        {
            f"n_{r}": (comp["rating"] == r).groupby([comp[k] for k in keys], sort=True, dropna=False).sum()
            for r in RATINGS
        }
    )
    out = out.join(counts, how="left").fillna({f"n_{c}": 0 for c in RATINGS})
    for c in RATINGS:
        out[f"n_{c}"] = out[f"n_{c}"].astype(np.int64)
    return out.reset_index()


# =========================================================================== wasted votes & EG
WASTED_COLUMNS: tuple[str, ...] = (
    "election_id",
    "year",
    "race_code",
    "race_type",
    "race_family",
    "level",
    "geo_code",
    "province_code",
    "line_key",
    "candidate",
    "party",
    "votes",
    "won",
    "wasted_losing",
    "wasted_surplus",
    "wasted",
)


def wasted_votes(frame: pd.DataFrame) -> pd.DataFrame:
    """Wasted votes per line in every seat contest (multiparty FPTP generalisation).

    Losing lines waste all their votes; the winner wastes the votes beyond what it needed to
    beat the runner-up: ``max(0, winner − (runner-up + 1))``.  An uncontested winner needed one
    vote.  Contests without any vote have no winner and no wasted votes.
    """
    base = seat_contests(frame)
    if base.empty:
        return pd.DataFrame(columns=list(WASTED_COLUMNS))
    r = ranked_lines(with_keys(base))
    ru = r[(r["_rank"] == 1).to_numpy()][[*GEO_KEY, "votes"]].rename(columns={"votes": "_ru"})
    r = r.merge(ru, on=list(GEO_KEY), how="left")
    r["_ru"] = r["_ru"].fillna(0).astype(np.int64)
    votes = r["votes"].to_numpy(dtype=np.int64)
    won = (r["_rank"] == 0).to_numpy() & ((votes > 0) | r["_w"].to_numpy())
    r["won"] = won
    r["wasted_surplus"] = np.where(won, np.maximum(votes - (r["_ru"].to_numpy() + 1), 0), 0).astype(np.int64)
    r["wasted_losing"] = np.where(won, 0, votes).astype(np.int64)
    r["wasted"] = r["wasted_surplus"] + r["wasted_losing"]
    r = r.sort_values([*GEO_KEY, "_rank"], kind="mergesort").reset_index(drop=True)
    return r.loc[:, list(WASTED_COLUMNS)]


def party_efficiency(frame: pd.DataFrame, *, seat_weights: Mapping[str, float] | None = None) -> pd.DataFrame:
    """Per election × race family × party: votes, seats and vote efficiency.

    Columns: ``votes, vote_share, seats, seat_share, seats_total, seat_bonus_pp`` (seat share −
    vote share), ``wasted, wasted_losing, wasted_surplus, waste_rate`` (wasted / own votes),
    ``wasted_share`` (wasted / all votes), ``efficiency_gap`` — the multiparty efficiency gap
    ``(V_p · W / V − W_p) / V`` (positive: the party wastes fewer votes than its proportional
    share of all wasted votes, i.e. the system is efficient for it; sums to 0 over parties) — and
    ``votes_per_seat`` (NaN without seats).  ``seat_weights`` maps contest geo codes to seat
    values (e.g. EV per province) — default 1 per contest.
    """
    wv = wasted_votes(frame)
    cols = [
        "election_id",
        "year",
        "race_family",
        "party",
        "votes",
        "vote_share",
        "seats",
        "seat_share",
        "seats_total",
        "seat_bonus_pp",
        "wasted",
        "wasted_losing",
        "wasted_surplus",
        "waste_rate",
        "wasted_share",
        "efficiency_gap",
        "votes_per_seat",
    ]
    if wv.empty:
        return pd.DataFrame(columns=cols)
    wv["_weight"] = _weights_for(wv["geo_code"], seat_weights)
    wv["seats"] = np.where(wv["won"], wv["_weight"], 0.0)
    grp = ["election_id", "race_family"]
    per = wv.groupby([*grp, "party"], sort=True).agg(
        year=("year", "first"),
        votes=("votes", "sum"),
        seats=("seats", "sum"),
        wasted=("wasted", "sum"),
        wasted_losing=("wasted_losing", "sum"),
        wasted_surplus=("wasted_surplus", "sum"),
    )
    per = per.reset_index()
    contests = wv.drop_duplicates(list(GEO_KEY))
    tot = contests.groupby(grp, sort=False)["_weight"].sum().rename("seats_total").reset_index()
    vt = wv.groupby(grp, sort=False).agg(_V=("votes", "sum"), _W=("wasted", "sum")).reset_index()
    per = per.merge(tot, on=grp, how="left").merge(vt, on=grp, how="left")
    V = per["_V"].to_numpy(dtype=float)
    W = per["_W"].to_numpy(dtype=float)
    vp = per["votes"].to_numpy(dtype=float)
    wp = per["wasted"].to_numpy(dtype=float)
    per["vote_share"] = _safe_div(vp, V)
    per["seat_share"] = _safe_div(per["seats"], per["seats_total"])
    per["seat_bonus_pp"] = 100.0 * (per["seat_share"] - per["vote_share"])
    per["waste_rate"] = _safe_div(wp, vp)
    per["wasted_share"] = _safe_div(wp, V)
    per["efficiency_gap"] = _safe_div(vp * _safe_div(W, V) - wp, V)
    per["votes_per_seat"] = _safe_div(vp, per["seats"])
    if seat_weights is None:
        per["seats"] = per["seats"].round().astype(np.int64)
        per["seats_total"] = per["seats_total"].round().astype(np.int64)
    per = per.sort_values([*grp, "votes", "party"], ascending=[True, True, False, True], kind="mergesort")
    return per.reset_index(drop=True).loc[:, cols]


def efficiency_gap(
    frame: pd.DataFrame, party_a: str | None = None, party_b: str | None = None
) -> pd.DataFrame:
    """Pairwise efficiency gap per election × race family (seat contests only).

    ``efficiency_gap = (W_b − W_a) / V`` over *all* valid votes V in the seat contests, and
    ``efficiency_gap_pair = (W_b − W_a) / (V_a + V_b)``.  **Positive values favour** ``party_a``
    (it wastes fewer votes than ``party_b``).  Without a pair, each group's top two parties by
    votes (independents excluded) are used.
    """
    if (party_a is None) != (party_b is None):
        raise ValueError("pass both party_a and party_b, or neither")
    if party_a is not None and party_a == party_b:
        raise ValueError("party_a and party_b must differ")
    wv = wasted_votes(frame)
    cols = [
        "election_id",
        "year",
        "race_family",
        "party_a",
        "party_b",
        "votes_a",
        "votes_b",
        "seats_a",
        "seats_b",
        "wasted_a",
        "wasted_b",
        "total_votes",
        "n_contests",
        "efficiency_gap",
        "efficiency_gap_pair",
    ]
    rows = []
    for (eid, fam), g in wv.groupby(["election_id", "race_family"], sort=True):
        per = g.groupby("party").agg(votes=("votes", "sum"), wasted=("wasted", "sum"), seats=("won", "sum"))
        if party_a is None:
            ranked = per.drop(index=INDEPENDENT_KEY, errors="ignore").reset_index()
            ranked = ranked.sort_values(["votes", "party"], ascending=[False, True], kind="mergesort")
            if len(ranked) < 2:
                continue
            a, b = str(ranked["party"].iloc[0]), str(ranked["party"].iloc[1])
        else:
            a, b = party_a, str(party_b)

        def get(party: str, col: str, per: pd.DataFrame = per) -> float:
            return float(per[col].get(party, 0))

        V = float(g["votes"].sum())
        wa, wb, va, vb = get(a, "wasted"), get(b, "wasted"), get(a, "votes"), get(b, "votes")
        rows.append(
            {
                "election_id": eid,
                "year": int(g["year"].iloc[0]),
                "race_family": fam,
                "party_a": a,
                "party_b": b,
                "votes_a": int(va),
                "votes_b": int(vb),
                "seats_a": int(get(a, "seats")),
                "seats_b": int(get(b, "seats")),
                "wasted_a": int(wa),
                "wasted_b": int(wb),
                "total_votes": int(V),
                "n_contests": int(g.drop_duplicates(list(GEO_KEY)).shape[0]),
                "efficiency_gap": (wb - wa) / V if V > 0 else np.nan,
                "efficiency_gap_pair": (wb - wa) / (va + vb) if va + vb > 0 else np.nan,
            }
        )
    return pd.DataFrame(rows, columns=cols)


# =========================================================================== seat–vote
SEAT_VOTE_COLUMNS: tuple[str, ...] = (
    "election_id",
    "year",
    "race_family",
    "party",
    "votes",
    "vote_share",
    "seats",
    "seat_share",
    "seats_total",
    "seat_bonus_pp",
)


def seat_vote_table(frame: pd.DataFrame, *, seat_weights: Mapping[str, float] | None = None) -> pd.DataFrame:
    """Seat share vs vote share per election × race family × party (see :func:`party_efficiency`)."""
    return party_efficiency(frame, seat_weights=seat_weights).loc[:, list(SEAT_VOTE_COLUMNS)]


def seat_vote_from_draws(
    vote_shares: np.ndarray,
    seat_shares: np.ndarray,
    parties: Sequence[str],
    *,
    race_family: str | None = None,
) -> pd.DataFrame:
    """Long seat–vote table from simulation draws: (S, P) vote and seat shares → rows
    ``draw, race_family, party, vote_share, seat_share`` for :func:`seat_vote_fit` (``sample_col='draw'``)."""
    V = np.asarray(vote_shares, dtype=float)
    S = np.asarray(seat_shares, dtype=float)
    if V.shape != S.shape or V.ndim != 2 or V.shape[1] != len(parties):
        raise ValueError("vote_shares and seat_shares must both be (S, P) with P = len(parties)")
    n, p = V.shape
    return pd.DataFrame(
        {
            "draw": np.repeat(np.arange(n), p),
            "race_family": race_family,
            "party": np.tile(np.asarray(parties, dtype=object), n),
            "vote_share": V.ravel(),
            "seat_share": S.ravel(),
        }
    )


def _logit(x: np.ndarray) -> np.ndarray:
    return np.log(x / (1.0 - x))


def seat_vote_fit(
    table: pd.DataFrame,
    *,
    sample_col: str = "election_id",
    method: Literal["linear", "logit"] = "linear",
    reference_share: float | None = None,
) -> pd.DataFrame:
    """Estimate responsiveness and bias of the seat–vote relationship per party across
    elections or simulation draws.

    * ``linear``: ``S = α + β·V``; responsiveness = β (seat-share points per vote-share point).
    * ``logit``: ``logit S = λ + ρ·logit V`` (King–Browning; ρ ≈ 3 is the "cube law");
      responsiveness = ρ.  Seat shares of 0/1 are clipped by half a seat (``seats_total``) or 1e-3.

    ``bias_pp = (Ŝ(v*) − v*) × 100`` — seats above proportionality at the reference vote share
    ``v*`` (default: the party's mean vote share; pass 0.5 for the classic two-party bias).
    NaN with fewer than two distinct vote shares.
    """
    if method not in ("linear", "logit"):
        raise ValueError("method must be 'linear' or 'logit'")
    keys = [c for c in ("race_family", "party") if c in table.columns]
    if "party" not in keys:
        raise ValueError("table needs a 'party' column")
    rows = []
    for gkey, g in table.groupby(keys, sort=True, dropna=False):
        gkey = gkey if isinstance(gkey, tuple) else (gkey,)
        x = g["vote_share"].to_numpy(dtype=float)
        y = g["seat_share"].to_numpy(dtype=float)
        ok = ~np.isnan(x) & ~np.isnan(y)
        x, y = x[ok], y[ok]
        n = len(x)
        ref = float(reference_share) if reference_share is not None else (float(x.mean()) if n else np.nan)
        slope = intercept = r2 = bias = np.nan
        if n >= 2 and np.ptp(x) > 1e-12:
            if method == "logit":
                eps_y = (
                    (0.5 / g["seats_total"].to_numpy(dtype=float)[ok])
                    if "seats_total" in g
                    else np.full(n, 1e-3)
                )
                yy = _logit(np.clip(y, eps_y, 1 - eps_y))
                xx = _logit(np.clip(x, 1e-6, 1 - 1e-6))
            else:
                xx, yy = x, y
            dx, dy = xx - xx.mean(), yy - yy.mean()
            sxx, sxy, syy = float(dx @ dx), float(dx @ dy), float(dy @ dy)
            slope = sxy / sxx
            intercept = float(yy.mean() - slope * xx.mean())
            r2 = sxy * sxy / (sxx * syy) if syy > 0 else np.nan
            if method == "logit":
                pred = 1.0 / (1.0 + np.exp(-(intercept + slope * _logit(np.clip(ref, 1e-6, 1 - 1e-6)))))
            else:
                pred = intercept + slope * ref
            bias = 100.0 * (pred - ref)
        rows.append(
            {
                **dict(zip(keys, gkey, strict=True)),
                "n_obs": int(g[sample_col].nunique()) if sample_col in g else n,
                "method": method,
                "responsiveness": slope,
                "intercept": intercept,
                "r_squared": r2,
                "reference_share": ref,
                "bias_pp": bias,
                "mean_vote_share": float(x.mean()) if n else np.nan,
                "mean_seat_share": float(y.mean()) if n else np.nan,
            }
        )
    return pd.DataFrame(rows)


@dataclass(frozen=True)
class PartisanBias:
    """Two-party seat–vote analysis by uniform swing on one election's seat contests.

    ``bias = (S_a − S_b) / 2`` at equal national votes for ``a`` and ``b`` (positive favours
    ``a``; equals the classic ``S_a(50 %) − 50 %`` with two parties).  ``responsiveness`` is
    ``dS_a / dV_a`` (two-party vote share) at the observed result, by central difference ± ``h``.
    """

    election_id: int
    race_family: str
    party_a: str
    party_b: str
    vote_share_2p_a: float
    seat_share_a: float
    seat_share_b: float
    seat_share_a_at_parity: float
    seat_share_b_at_parity: float
    bias: float
    responsiveness: float
    h: float


def uns_seat_vote_curve(
    frame: pd.DataFrame,
    party_a: str | None = None,
    party_b: str | None = None,
    *,
    grid: Sequence[float] | np.ndarray | None = None,
    seat_weights: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    """Seat shares of ``a`` and ``b`` as a function of ``a``'s national two-party vote share,
    by uniform swing between the two (other parties unchanged) over one election's seat contests.

    For each target ``t`` in ``grid`` (default 0.30…0.70 step 0.01) the swing ``s = t·(A+B) − A``
    (national raw shares A, B) is added to ``a`` and subtracted from ``b`` in every contest they
    ran in; shares are clipped at 0 and each contest goes to the plurality *line* (pooled
    independents stay separate candidates; exact ties as in :func:`uniform_swing_projection`).
    """
    base = seat_contests(frame)
    single_election(base, "uns_seat_vote_curve")
    fams = sorted(set(with_keys(base)["race_family"]))
    if len(fams) != 1:
        raise ResultsFrameError(f"uns_seat_vote_curve: expected one race family, found {fams}")
    a, b = _pair(base, party_a, party_b)
    idx, parties, S, ps = _share_matrix(base)
    for p in (a, b):
        if p not in parties:
            raise ValueError(f"party {p!r} did not run in any seat contest")
    ia, ib = parties.index(a), parties.index(b)
    lines = _contest_lines(base, idx, parties)
    valid = idx.merge(
        ps.drop_duplicates(list(GEO_KEY))[[*GEO_KEY, "valid_votes"]], on=list(GEO_KEY), how="left"
    )["valid_votes"].to_numpy(dtype=float)
    Vt = valid.sum()
    A = float((S[:, ia] * valid).sum() / Vt) if Vt > 0 else 0.0
    B = float((S[:, ib] * valid).sum() / Vt) if Vt > 0 else 0.0
    if A + B <= 0:
        raise ResultsFrameError("uns_seat_vote_curve: parties received no votes")
    w = _weights_for(idx["geo_code"], seat_weights)
    W = w.sum()
    ts = np.round(np.arange(0.30, 0.7001, 0.01), 10) if grid is None else np.asarray(grid, dtype=float)
    ma, mb = S[:, ia] > 0, S[:, ib] > 0
    rows = []
    for t in ts:
        s = t * (A + B) - A
        M = S.copy()
        M[:, ia] = np.clip(M[:, ia] + s * ma, 0.0, None)
        M[:, ib] = np.clip(M[:, ib] - s * mb, 0.0, None)
        win, _ = _project_winners(lines, S, M, parties)
        sa = float(w[win == a].sum() / W)
        sb = float(w[win == b].sum() / W)
        rows.append(
            {
                "vote_share_2p_a": float(t),
                "swing_pp": 100.0 * s,
                "seat_share_a": sa,
                "seat_share_b": sb,
                "seat_share_other": 1.0 - sa - sb,
            }
        )
    out = pd.DataFrame(rows)
    out.attrs.update({"party_a": a, "party_b": b, "observed_vote_share_2p_a": A / (A + B)})
    return out


def partisan_bias(
    frame: pd.DataFrame,
    party_a: str | None = None,
    party_b: str | None = None,
    *,
    seat_weights: Mapping[str, float] | None = None,
    h: float = 0.025,
) -> PartisanBias:
    """Partisan bias and responsiveness of one election's seat contests by uniform swing
    (see :class:`PartisanBias` and :func:`uns_seat_vote_curve`)."""
    if not 0 < h < 0.25:
        raise ValueError("h must be in (0, 0.25)")
    base = seat_contests(frame)
    eid, _ = single_election(base, "partisan_bias")
    a, b = _pair(base, party_a, party_b)
    probe = uns_seat_vote_curve(base, a, b, grid=[0.5], seat_weights=seat_weights)
    t0 = float(probe.attrs["observed_vote_share_2p_a"])
    curve = uns_seat_vote_curve(base, a, b, grid=[0.5, t0, t0 - h, t0 + h], seat_weights=seat_weights)
    par, obs, lo, hi = (curve.iloc[i] for i in range(4))
    return PartisanBias(
        election_id=eid,
        race_family=str(with_keys(base)["race_family"].iloc[0]),
        party_a=a,
        party_b=b,
        vote_share_2p_a=t0,
        seat_share_a=float(obs["seat_share_a"]),
        seat_share_b=float(obs["seat_share_b"]),
        seat_share_a_at_parity=float(par["seat_share_a"]),
        seat_share_b_at_parity=float(par["seat_share_b"]),
        bias=float(par["seat_share_a"] - par["seat_share_b"]) / 2.0,
        responsiveness=float(hi["seat_share_a"] - lo["seat_share_a"]) / (2.0 * h),
        h=h,
    )


# =========================================================================== Electoral College
def _key_col(by: Literal["party", "line"]) -> str:
    if by == "party":
        return "party"
    if by == "line":
        return "line_key"
    raise ValueError("by must be 'party' or 'line'")


def _province_contests(frame: pd.DataFrame) -> pd.DataFrame:
    prov = frame[
        (frame["race_type"].astype(str) == RaceType.PRESIDENT_PROVINCE.value).to_numpy()
        & (frame["level"] == "province").to_numpy()
    ]
    if prov.empty:
        raise ResultsFrameError("no PRESIDENT_PROVINCE rows at level 'province' in the frame")
    return prov


def _national_pres_rows(frame: pd.DataFrame, eid: int) -> pd.DataFrame:
    f = frame[(frame["election_id"] == eid).to_numpy()]
    nat = f[
        (f["race_type"].astype(str) == RaceType.PRESIDENT.value).to_numpy()
        & (f["level"] == "national").to_numpy()
    ]
    return nat if not nat.empty else _province_contests(f)


def ec_efficiency(popular_votes: Mapping[str, float], electoral_votes: Mapping[str, float]) -> pd.DataFrame:
    """Electoral College efficiency per ticket: ``efficiency_pp = (EV share − PV share) × 100``.

    Keys are the union of both mappings (missing values count as 0).  Positive values mean the
    ticket converts its popular vote into electoral votes more efficiently than proportionally.
    """
    keys = sorted(set(popular_votes) | set(electoral_votes))
    pv = np.array([float(popular_votes.get(k, 0)) for k in keys])
    ev = np.array([float(electoral_votes.get(k, 0)) for k in keys])
    out = pd.DataFrame({"key": keys, "popular_votes": pv, "electoral_votes": ev})
    out["pv_share"] = _safe_div(pv, np.full(len(pv), pv.sum()))
    out["ev_share"] = _safe_div(ev, np.full(len(ev), ev.sum()))
    out["efficiency_pp"] = 100.0 * (out["ev_share"] - out["pv_share"])
    out = out.sort_values(
        ["electoral_votes", "popular_votes", "key"], ascending=[False, False, True], kind="mergesort"
    )
    return out.reset_index(drop=True)[
        ["key", "popular_votes", "pv_share", "electoral_votes", "ev_share", "efficiency_pp"]
    ]


def electoral_college_tally(
    frame: pd.DataFrame,
    ev_by_province: Mapping[str, int],
    *,
    by: Literal["party", "line"] = "party",
) -> pd.DataFrame:
    """Winner-take-all EV tally, national popular vote and EC efficiency per election × ticket.

    Province winners come from the ``PRESIDENT_PROVINCE`` rows at level ``province`` (the line
    flagged winner breaks exact ties); the popular vote from the ``PRESIDENT`` national rows
    (or the sum of the province contests).  ``by='party'`` keys tickets by party code (independents
    pooled), ``by='line'`` by ``line_key``.  Every province of ``ev_by_province`` must be present;
    a province without any vote awards no electoral votes.
    Columns: ``election_id, year, key, label, popular_votes, pv_share, electoral_votes, ev_share,
    provinces_won, efficiency_pp``.
    """
    kc = _key_col(by)
    prov = _province_contests(frame)
    mt = margin_table(prov)
    win_col = "winner_party" if by == "party" else "winner_line_key"
    total_ev = float(sum(ev_by_province.values()))
    rows = []
    for eid, g in mt.groupby("election_id", sort=True):
        have, want = set(g["geo_code"]), set(ev_by_province)
        if have != want:
            raise ResultsFrameError(
                f"electoral_college_tally: election {eid} provinces mismatch "
                f"(missing {sorted(want - have)}, unexpected {sorted(have - want)})"
            )
        ev = g["geo_code"].map(ev_by_province).astype(float)
        won = pd.DataFrame({"key": g[win_col].to_numpy(dtype=object), "ev": ev.to_numpy()})
        won = won[won["key"].notna().to_numpy()]
        ev_k = won.groupby("key")["ev"].agg(["sum", "size"])
        nat = with_keys(_national_pres_rows(frame, int(eid))).sort_values(["line_key"], kind="mergesort")
        pv = nat.groupby(kc, sort=True).agg(popular_votes=("votes", "sum"), label=("candidate", "first"))
        valid = float(nat.drop_duplicates(list(GEO_KEY))["valid_votes"].sum())
        t = pv.join(ev_k, how="outer")
        for k, r in t.iterrows():
            pvv = float(r["popular_votes"]) if not pd.isna(r["popular_votes"]) else 0.0
            evv = float(r["sum"]) if not pd.isna(r["sum"]) else 0.0
            rows.append(
                {
                    "election_id": int(eid),
                    "year": int(g["year"].iloc[0]),
                    "key": str(k),
                    "label": r["label"] if not pd.isna(r["label"]) else None,
                    "popular_votes": int(pvv),
                    "pv_share": pvv / valid if valid > 0 else np.nan,
                    "electoral_votes": round(evv),
                    "ev_share": evv / total_ev if total_ev > 0 else np.nan,
                    "provinces_won": int(r["size"]) if not pd.isna(r["size"]) else 0,
                }
            )
    out = pd.DataFrame(rows)
    out["efficiency_pp"] = 100.0 * (out["ev_share"] - out["pv_share"])
    out = out.sort_values(
        ["election_id", "electoral_votes", "popular_votes", "key"],
        ascending=[True, False, False, True],
        kind="mergesort",
    )
    return out.reset_index(drop=True)


@dataclass(frozen=True)
class TippingPoint:
    """The tipping-point province: provinces sorted by the candidate's margin (descending;
    equal margins keep ``ev_by_province`` order), electoral votes accumulated until the majority."""

    province: str
    margin_pp: float
    cumulative_ev: int
    majority: int
    order: tuple[str, ...]
    cumulative: tuple[int, ...]


@dataclass(frozen=True)
class ECBias:
    """Electoral College bias for one candidate: ``bias_pp = tipping-point margin − national
    margin``.  Positive: the EC is more favourable to the candidate than the popular vote."""

    key: str
    tipping_point: TippingPoint
    national_margin_pp: float
    bias_pp: float

    @property
    def tipping_province(self) -> str:
        return self.tipping_point.province

    @property
    def tipping_margin_pp(self) -> float:
        return self.tipping_point.margin_pp


def tipping_point(
    province_margins: Mapping[str, float],
    ev_by_province: Mapping[str, int],
    majority: int | None = None,
) -> TippingPoint:
    """Tipping-point province for a candidate whose margin (pp; negative where it trails) is
    given per province.  ``majority`` defaults to ``majority_of(sum EV)`` (88 of 174 canonically).
    Provinces with an undefined (NaN) margin sort last."""
    provinces = list(ev_by_province)
    missing = [p for p in provinces if p not in province_margins]
    if missing:
        raise ValueError(f"tipping_point: no margin for provinces {missing}")
    total = int(sum(int(ev_by_province[p]) for p in provinces))
    need = int(majority) if majority is not None else majority_of(total)
    if need > total:
        raise ValueError(f"tipping_point: majority {need} exceeds total electoral votes {total}")
    margins = np.array([float(province_margins[p]) for p in provinces])
    key = np.where(np.isnan(margins), np.inf, -margins)
    order = np.argsort(key, kind="stable")
    ev = np.array([int(ev_by_province[provinces[i]]) for i in order])
    cum = np.cumsum(ev)
    pos = int(np.searchsorted(cum, need))
    tp = provinces[order[pos]]
    return TippingPoint(
        province=tp,
        margin_pp=float(margins[order[pos]]),
        cumulative_ev=int(cum[pos]),
        majority=need,
        order=tuple(provinces[i] for i in order),
        cumulative=tuple(int(c) for c in cum),
    )


def ec_bias(
    province_margins: Mapping[str, float],
    ev_by_province: Mapping[str, int],
    national_margin_pp: float,
    majority: int | None = None,
    *,
    key: str = "",
) -> ECBias:
    """Electoral College bias: tipping-point margin minus national popular-vote margin (pp)."""
    tp = tipping_point(province_margins, ev_by_province, majority)
    return ECBias(
        key=key,
        tipping_point=tp,
        national_margin_pp=float(national_margin_pp),
        bias_pp=tp.margin_pp - float(national_margin_pp),
    )


def _key_margins(rows: pd.DataFrame, kc: str, key: str) -> pd.Series:
    """Margin (pp) of ``key`` over its strongest opponent per geo_code."""
    v = rows.groupby(["geo_code", kc], sort=True)["votes"].sum().unstack(kc, fill_value=0)
    valid = rows.drop_duplicates(list(GEO_KEY)).groupby("geo_code")["valid_votes"].sum().reindex(v.index)
    own = v[key] if key in v.columns else pd.Series(0, index=v.index)
    others = v.drop(columns=[key], errors="ignore")
    best = others.max(axis=1) if others.shape[1] else pd.Series(0, index=v.index)
    # same operation order as app.elections.electoral_college (100·Δ / V) so margins agree exactly
    diff = 100.0 * (own - best).to_numpy(dtype=float)
    return pd.Series(_safe_div(diff, valid), index=v.index)


def tipping_point_from_frame(
    frame: pd.DataFrame,
    ev_by_province: Mapping[str, int],
    *,
    majority: int | None = None,
    key: str | None = None,
    by: Literal["party", "line"] = "party",
) -> ECBias:
    """Tipping point and EC bias of one presidential election in a results frame.

    ``key`` (party code or line key per ``by``) defaults to the EV leader (most EV, then most
    popular votes).  Province margins are the key's share minus the strongest opponent's share
    in each ``PRESIDENT_PROVINCE`` contest; the national margin comes from the ``PRESIDENT``
    national rows (or the sum of the province contests).
    """
    eid, _ = single_election(frame, "tipping_point_from_frame")
    kc = _key_col(by)
    prov = with_keys(_province_contests(frame))
    have, want = set(prov["geo_code"]), set(ev_by_province)
    if have != want:
        raise ResultsFrameError(
            f"tipping_point_from_frame: provinces mismatch (missing {sorted(want - have)}, "
            f"unexpected {sorted(have - want)})"
        )
    k = key if key is not None else str(electoral_college_tally(frame, ev_by_province, by=by)["key"].iloc[0])
    margins = _key_margins(prov, kc, k)
    nat = with_keys(_national_pres_rows(frame, eid)).assign(geo_code="__NAT__")
    nat_margin = float(_key_margins(nat, kc, k).iloc[0])
    return ec_bias(margins.to_dict(), ev_by_province, nat_margin, majority, key=k)


# =========================================================================== race summaries
RACE_SUMMARY_COLUMNS: tuple[str, ...] = (
    "election_id",
    "year",
    "race_code",
    "race_type",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "winner_line_key",
    "winner_candidate",
    "winner_party",
    "winner_votes",
    "winner_share",
    "runner_up_candidate",
    "runner_up_party",
    "runner_up_votes",
    "margin_votes",
    "margin_pp",
    "tied",
    "contested",
    "valid_votes",
    "eligible",
    "ballots_cast",
    "turnout",
    "previous_winner_party",
    "flip_status",
    "flipped",
    "incumbent_candidate",
    "incumbent_party",
    "is_open_seat",
    "incumbent_won",
)


def race_summaries(
    frame: pd.DataFrame,
    prev: pd.DataFrame | None = None,
    incumbents: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per race (at its jurisdiction level): winner, runner-up, margin, turnout, flip and
    incumbency.

    The previous holder is the winning party of the same race code in ``prev`` (its most recent
    election holding that race *before* the row's election, by ``(year, election_id)``; ``prev``
    may therefore be the whole history, including the current election) if given, else
    ``incumbents.incumbent_party``.  ``incumbents`` (optional) has ``race_code`` and any of
    ``incumbent_candidate, incumbent_party, is_open_seat``.  ``flip_status`` ∈ {hold, flip,
    undecided, new} (``new``: no previous holder known).  ``incumbent_won`` is null when the
    incumbent candidate is unknown or the seat is open.
    """
    rows = contest_rows(frame)
    mt = margin_table(rows)
    out = mt.copy()
    out["turnout"] = _safe_div(out["ballots_cast"], out["eligible"])
    inc = pd.DataFrame(columns=["race_code", "incumbent_candidate", "incumbent_party", "is_open_seat"])
    if incumbents is not None:
        inc = incumbents.copy()
        for c in ("incumbent_candidate", "incumbent_party", "is_open_seat"):
            if c not in inc:
                inc[c] = None
        inc = inc[["race_code", "incumbent_candidate", "incumbent_party", "is_open_seat"]].drop_duplicates(
            "race_code"
        )
    out = out.merge(inc, on="race_code", how="left")
    prev_holder = out["incumbent_party"].astype(object)
    if prev is not None and not prev.empty:
        pw = margin_table(contest_rows(prev))[["election_id", "year", "race_code", "winner_party"]]
        pw = pw.rename(columns={"election_id": "_pe", "year": "_py", "winner_party": "_prev_w"})
        cur = out[["election_id", "year", "race_code"]].drop_duplicates()
        pw = cur.merge(pw, on="race_code", how="inner")
        # only elections strictly before the current one: (year, election_id) order
        before = (pw["_py"] < pw["year"]) | ((pw["_py"] == pw["year"]) & (pw["_pe"] < pw["election_id"]))
        pw = pw[before.to_numpy()].sort_values(["_py", "_pe"], kind="mergesort")
        pw = pw.drop_duplicates(["election_id", "race_code"], keep="last")
        out = out.merge(
            pw[["election_id", "race_code", "_prev_w"]], on=["election_id", "race_code"], how="left"
        )
        prev_holder = out["_prev_w"].astype(object).where(out["_prev_w"].notna(), prev_holder)
        out = out.drop(columns="_prev_w")
    out["previous_winner_party"] = prev_holder.where(prev_holder.notna(), None)
    pw_arr = out["previous_winner_party"].to_numpy(dtype=object)
    cw_arr = out["winner_party"].to_numpy(dtype=object)
    no_prev = pd.isna(pw_arr)
    undecided = pd.isna(cw_arr)
    same = _same(pw_arr, cw_arr)
    out["flip_status"] = np.select([no_prev, undecided, same], ["new", "undecided", "hold"], default="flip")
    out["flipped"] = out["flip_status"] == "flip"
    open_seat = as_bool(out["is_open_seat"])
    out["is_open_seat"] = open_seat
    known = out["incumbent_candidate"].notna().to_numpy() & ~open_seat.to_numpy()
    won = pd.array(_same(out["incumbent_candidate"], out["winner_candidate"]), dtype="boolean")
    won[~known] = pd.NA
    out["incumbent_won"] = won
    out = out.sort_values(["election_id", "race_code"], kind="mergesort").reset_index(drop=True)
    return out.loc[:, list(RACE_SUMMARY_COLUMNS)]
