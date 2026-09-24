"""Historical comparison functions over standard results frames (SIMULATED data).

* records: :func:`closest_races`, :func:`largest_landslides`;
* Electoral College vs popular vote: :func:`ec_pv_divergence` (descriptive only);
* series across elections: :func:`province_trend`, :func:`party_support_series`,
  :func:`district_history`, :func:`seat_totals`;
* comparisons between two elections: :func:`compare_elections` (tidy swing table),
  :func:`municipality_flips`;
* lineage: :func:`remap_lineage` maps an older election's municipality results onto a newer
  municipality set (mergers / splits between cycles) so swings are computed like-for-like.

``frames`` arguments accept a single results frame holding several elections or a sequence of
per-election frames.  Race-type arguments of the series/comparison functions are *families*:
``PRESIDENT`` includes the ``PRESIDENT_PROVINCE`` contests.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from app.analytics.metrics import (
    FLIP_COLUMNS,
    SWING_COLUMNS,
    flips,
    margin_table,
    swing,
    turnout_change,
)
from app.analytics.results import (
    GEO_KEY,
    LEVELS,
    PROPORTIONAL_RACE_TYPES,
    RESULTS_COLUMNS,
    GroupBy,
    ResultsFrameError,
    as_bool,
    as_frame,
    at_level,
    contest_rows,
    filter_family,
    national_party_totals,
    normalize_dtypes,
    normalize_values,
    party_shares,
    ranked_lines,
    require_columns,
    seat_contests,
)
from app.core.constitution import RaceType, majority_of
from app.core.logging import get_logger

log = get_logger(__name__)

RECORD_COLUMNS: tuple[str, ...] = (
    "rank",
    "election_id",
    "year",
    "race_code",
    "race_type",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "winner_candidate",
    "winner_party",
    "winner_share",
    "runner_up_candidate",
    "runner_up_party",
    "runner_up_share",
    "margin_votes",
    "margin_pp",
    "valid_votes",
    "tied",
    "contested",
)


# =========================================================================== records
def _race_records(
    frames: pd.DataFrame | Iterable[pd.DataFrame],
    race_types: str | RaceType | Iterable[str] | None,
    include_uncontested: bool,
) -> pd.DataFrame:
    df = as_frame(frames)
    rt = df["race_type"].astype(str)
    if race_types is not None:
        df = df[rt.isin(normalize_values(race_types)).to_numpy()]
    else:  # a list's top-two margin decides no seat under D'Hondt
        df = df[~rt.isin(PROPORTIONAL_RACE_TYPES).to_numpy()]
    mt = margin_table(contest_rows(df))
    mt = mt[mt["margin_pp"].notna().to_numpy()]
    if not include_uncontested:
        mt = mt[mt["contested"].to_numpy()]
    return mt


def closest_races(
    frame: pd.DataFrame | Iterable[pd.DataFrame],
    n: int = 10,
    race_types: str | RaceType | Iterable[str] | None = None,
    *,
    include_uncontested: bool = False,
) -> pd.DataFrame:
    """The ``n`` closest races (smallest top-two margin in pp at the race's jurisdiction level).

    Ordered by ``margin_pp``, then ``margin_votes``, ``year`` and ``race_code`` (deterministic).
    Races without valid votes are excluded; uncontested races only with ``include_uncontested``.
    Proportional party-list races (councils, provincial legislatures) are only included when
    ``race_types`` names them.
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    mt = _race_records(frame, race_types, include_uncontested)
    mt = mt.sort_values(["margin_pp", "margin_votes", "year", "race_code"], kind="mergesort").head(n)
    mt = mt.reset_index(drop=True)
    mt["rank"] = np.arange(1, len(mt) + 1)
    return mt.loc[:, list(RECORD_COLUMNS)]


def largest_landslides(
    frame: pd.DataFrame | Iterable[pd.DataFrame],
    n: int = 10,
    race_types: str | RaceType | Iterable[str] | None = None,
    *,
    include_uncontested: bool = False,
) -> pd.DataFrame:
    """The ``n`` races with the largest top-two margin (pp), ordered by ``margin_pp`` desc, then
    ``margin_votes`` desc, ``year`` and ``race_code``.  Uncontested races (100 pp by definition)
    are excluded unless ``include_uncontested``; proportional party-list races unless
    ``race_types`` names them."""
    if n < 0:
        raise ValueError("n must be non-negative")
    mt = _race_records(frame, race_types, include_uncontested)
    mt = mt.sort_values(
        ["margin_pp", "margin_votes", "year", "race_code"],
        ascending=[False, False, True, True],
        kind="mergesort",
    ).head(n)
    mt = mt.reset_index(drop=True)
    mt["rank"] = np.arange(1, len(mt) + 1)
    return mt.loc[:, list(RECORD_COLUMNS)]


# =========================================================================== EC vs PV
DIVERGENCE_COLUMNS: tuple[str, ...] = (
    "election_id",
    "year",
    "pv_leader",
    "pv_leader_share",
    "pv_margin_pp",
    "ev_leader",
    "ev_leader_votes",
    "ev_leader_pv_share",
    "total_ev",
    "majority",
    "ev_majority",
    "diverged",
    "description",
)


def ec_pv_divergence(elections: pd.DataFrame, *, majority: int | None = None) -> pd.DataFrame:
    """Per election: popular-vote plurality leader vs Electoral College leader (descriptive).

    ``elections`` is long-form, one row per election × ticket with ``year, key, popular_votes,
    electoral_votes`` (and optionally ``election_id``) — e.g. the output of
    :func:`app.analytics.metrics.electoral_college_tally`.  ``majority`` defaults to
    ``majority_of(total EV)`` per election.  A leader is null on an exact tie.  ``diverged`` is
    true when both leaders exist and differ.  ``description`` states the facts without judgement.
    """
    require_columns(elections, ("year", "key", "popular_votes", "electoral_votes"), "ec_pv_divergence")
    df = elections.copy()
    group = "election_id" if "election_id" in df.columns else "year"
    rows = []
    for gid, g in df.groupby(group, sort=True):
        pv = (
            g.groupby("key")["popular_votes"]
            .sum()
            .astype(float)
            .sort_values(ascending=False, kind="mergesort")
        )
        ev = (
            g.groupby("key")["electoral_votes"]
            .sum()
            .astype(float)
            .sort_values(ascending=False, kind="mergesort")
        )
        pv_tot = float(pv.sum())
        total_ev = round(ev.sum())
        need = int(majority) if majority is not None else (majority_of(total_ev) if total_ev > 0 else None)
        pv_leader = (
            str(pv.index[0])
            if len(pv) and pv.iloc[0] > 0 and (len(pv) == 1 or pv.iloc[0] > pv.iloc[1])
            else None
        )
        ev_leader = (
            str(ev.index[0])
            if len(ev) and ev.iloc[0] > 0 and (len(ev) == 1 or ev.iloc[0] > ev.iloc[1])
            else None
        )
        second_pv = float(pv.iloc[1]) if len(pv) > 1 else 0.0
        pv_margin = 100.0 * (float(pv.iloc[0]) - second_pv) / pv_tot if pv_tot > 0 and len(pv) else np.nan
        ev_votes = int(ev.iloc[0]) if len(ev) else 0
        ev_majority = bool(ev_votes >= need) if need is not None and ev_leader is not None else False
        diverged = pv_leader is not None and ev_leader is not None and pv_leader != ev_leader
        ev_leader_pv = (
            float(pv.get(ev_leader, 0.0)) / pv_tot if ev_leader is not None and pv_tot > 0 else np.nan
        )
        if pv_leader is None and ev_leader is None:
            text = "Exact ties at the top of both the popular vote and the electoral vote."
        elif pv_leader is None:
            text = f"The popular vote is tied at the top; {ev_leader} leads the electoral vote with {ev_votes} EV."
        elif ev_leader is None:
            text = f"{pv_leader} leads the popular vote by {pv_margin:.2f} pp; the electoral vote is tied at the top."
        elif diverged:
            text = (
                f"{ev_leader} leads the electoral vote with {ev_votes} EV while {pv_leader} leads the popular "
                f"vote by {pv_margin:.2f} pp."
            )
        else:
            text = f"{ev_leader} leads both the electoral vote ({ev_votes} EV) and the popular vote (by {pv_margin:.2f} pp)."
        if need is not None and not ev_majority:
            text += f" No ticket reached the {need}-EV majority."
        rows.append(
            {
                "election_id": int(g["election_id"].iloc[0]) if "election_id" in g else None,
                "year": int(g["year"].iloc[0]),
                "pv_leader": pv_leader,
                "pv_leader_share": float(pv.iloc[0]) / pv_tot if pv_tot > 0 and len(pv) else np.nan,
                "pv_margin_pp": pv_margin,
                "ev_leader": ev_leader,
                "ev_leader_votes": ev_votes,
                "ev_leader_pv_share": ev_leader_pv,
                "total_ev": total_ev,
                "majority": need,
                "ev_majority": ev_majority,
                "diverged": diverged,
                "description": text,
            }
        )
        log.debug("ec_pv_divergence %s=%s diverged=%s", group, gid, diverged)
    return pd.DataFrame(rows, columns=list(DIVERGENCE_COLUMNS))


# =========================================================================== lineage
#: Weights of one old unit summing to within this tolerance of 1 are rounding noise (e.g. the
#: 6-decimal ``population_weight`` of :func:`app.geography.lineage.compute_lineage`) and are
#: rescaled to sum to exactly 1, so splits preserve totals.
LINEAGE_WEIGHT_TOLERANCE: float = 1e-4


def _lineage_table(lineage: pd.DataFrame) -> pd.DataFrame:
    require_columns(lineage, ("from_code", "to_code"), "lineage")
    lin = pd.DataFrame(
        {
            "from_code": lineage["from_code"].astype(str).to_numpy(),
            "to_code": lineage["to_code"].astype(str).to_numpy(),
            "w": lineage["population_weight"].astype(float).to_numpy()
            if "population_weight" in lineage
            else 1.0,
        }
    )
    if (lin["w"] < 0).any() or lin["w"].isna().any() or np.isinf(lin["w"]).any():
        raise ResultsFrameError("lineage: population_weight must be finite and non-negative")
    if lin.duplicated(["from_code", "to_code"]).any():
        raise ResultsFrameError("lineage: duplicate (from_code, to_code) pairs")
    total = lin.groupby("from_code", sort=False)["w"].transform("sum").to_numpy(dtype=float)
    over = total > 1.0 + LINEAGE_WEIGHT_TOLERANCE
    if over.any():
        raise ResultsFrameError(
            f"lineage: weights of {sorted(set(lin.loc[over, 'from_code']))} sum to more than 1"
        )
    rounding = (np.abs(total - 1.0) <= LINEAGE_WEIGHT_TOLERANCE) & (total != 1.0)
    if rounding.any():
        lin.loc[rounding, "w"] = lin.loc[rounding, "w"].to_numpy() / total[rounding]
    return lin


def remap_lineage(
    frame: pd.DataFrame,
    lineage: pd.DataFrame,
    *,
    level: str = "municipality",
    names: Mapping[str, str] | None = None,
    province_codes: Mapping[str, str] | None = None,
    round_votes: bool = True,
) -> pd.DataFrame:
    """Re-express an older election's results at ``level`` on a newer code set.

    ``lineage`` has ``from_code, to_code`` and ``population_weight`` (share of the old unit's
    population that moved to the new unit; 1.0 for a clean merger, default 1.0) — one transition
    from the old code set to the new one, as :func:`app.geography.lineage.compute_lineage`
    produces (compose several transitions before calling).  Weights of an old unit that sum to 1
    up to :data:`LINEAGE_WEIGHT_TOLERANCE` are rescaled to exactly 1.  Votes, valid votes,
    eligible voters and ballots of every old unit are multiplied by the weight and summed per new
    unit; codes absent from the lineage map to themselves.  With ``round_votes`` weighted counts
    are rounded to integers (``valid_votes`` = sum of rounded line votes), so a clean merger is
    exact.  Shares and winners of affected units are recomputed (plurality); unaffected rows are
    returned unchanged.

    Race codes that embed a remapped code (e.g. ``MAYOR-GM0001``) are rewritten to the dominant
    successor.  When that merges several old races into one (``MAYOR-GM0001`` and
    ``MAYOR-GM0002`` → ``MAYOR-GM0100``), their rows at the *other* levels (e.g. the ``province``
    rows services store for every race) are pooled the same way, so the result stays a valid
    results frame; other rows at other levels pass through.

    ``names`` / ``province_codes`` supply the new units' names and provinces (default: the name
    of an identically-coded old unit, else the new code; the province of the largest source).
    """
    if level not in LEVELS:
        raise ResultsFrameError(f"unknown level {level!r}; expected one of {LEVELS}")
    lin = _lineage_table(lineage)
    names = dict(names or {})
    province_codes = dict(province_codes or {})
    df = frame.loc[:, list(RESULTS_COLUMNS)].copy().reset_index(drop=True)
    df["_src_race"] = df["race_code"].astype(object)

    dom = lin.sort_values(["from_code", "w", "to_code"], ascending=[True, False, True], kind="mergesort")
    dom = dom.drop_duplicates("from_code")
    dom = dom[(dom["from_code"] != dom["to_code"]).to_numpy()]
    merged_races: set[str] = set()
    if len(dom):
        succ = dict(zip(dom["from_code"], dom["to_code"], strict=True))
        parts = df["race_code"].astype(str).str.rsplit("-", n=1)
        mapped = parts.str[-1].map(succ)
        has = mapped.notna() & (parts.str.len() == 2)
        if has.any():
            df.loc[has, "race_code"] = parts[has].str[0] + "-" + mapped[has]
            merged_races = set(df.loc[has, "race_code"])

    is_level = (df["level"] == level).to_numpy()
    cand = is_level | df["race_code"].isin(merged_races).to_numpy()
    cdf = df[cand]
    # source race-geos → target geos (lineage at `level`, identity elsewhere)
    src_key = ["election_id", "_src_race", "level", "geo_code"]
    src = cdf.drop_duplicates(src_key)[[*src_key, "race_code", "valid_votes", "eligible", "ballots_cast"]]
    at_codes = pd.Index(src.loc[(src["level"] == level).to_numpy(), "geo_code"].astype(str).unique())
    ident = at_codes.difference(pd.Index(lin["from_code"]))
    full = pd.concat(
        [
            lin[lin["from_code"].isin(at_codes).to_numpy()],
            pd.DataFrame({"from_code": ident, "to_code": ident, "w": 1.0}),
        ],
        ignore_index=True,
    )
    src_at = src[(src["level"] == level).to_numpy()].merge(
        full.rename(columns={"from_code": "geo_code"}), on="geo_code", how="inner"
    )
    src_other = src[(src["level"] != level).to_numpy()].assign(to_code=lambda s: s["geo_code"], w=1.0)
    src = pd.concat([src_at, src_other], ignore_index=True)
    tk = ["election_id", "race_code", "level", "to_code"]
    src["_one"] = src["w"] == 1.0
    src["_same"] = (src["geo_code"].astype(str) == src["to_code"].astype(str)).to_numpy()
    tgt = src.groupby(tk, sort=False).agg(n=("geo_code", "size"), one=("_one", "all"), same=("_same", "all"))
    changed = tgt[((tgt["n"] > 1) | ~tgt["one"] | ~tgt["same"]).to_numpy()]
    single = tgt[((tgt["n"] == 1) & tgt["one"]).to_numpy()].index
    src = src.merge(changed[[]].reset_index(), on=tk, how="inner")
    if src.empty:
        return (
            df.drop(columns="_src_race")
            .sort_values([*GEO_KEY, "line_key"], kind="mergesort")
            .reset_index(drop=True)
        )
    aff_keys = pd.MultiIndex.from_frame(src[src_key].drop_duplicates())
    aff_mask = np.zeros(len(df), dtype=bool)
    aff_mask[np.flatnonzero(cand)] = pd.MultiIndex.from_frame(cdf[src_key]).isin(aff_keys)
    passthrough = df[~aff_mask].drop(columns="_src_race")
    aff = df[aff_mask]

    m = aff.merge(src[[*src_key, "to_code", "w"]], on=src_key, how="inner")
    # labels of pooled lines come from the first source in a fixed order (input-order independent)
    m = m.sort_values(["_src_race", "geo_code", "line_key"], kind="mergesort")
    m["_votes"] = m["votes"].to_numpy(dtype=float) * m["w"].to_numpy()
    in_single = pd.MultiIndex.from_frame(m[tk]).isin(single)
    m["_flag"] = as_bool(m["winner"]).to_numpy() & in_single
    lk = [*tk, "line_key"]
    lines = m.groupby(lk, sort=False).agg(
        year=("year", "first"),
        race_type=("race_type", "first"),
        candidate=("candidate", "first"),
        party_code=("party_code", "first"),
        votes=("_votes", "sum"),
        winner=("_flag", "any"),
    )
    lines = lines.reset_index()
    g = src.copy()
    for c in ("valid_votes", "eligible", "ballots_cast"):
        g[c] = g[c].to_numpy(dtype=float) * g["w"].to_numpy()
    g["_size"] = g["eligible"]
    g = g.merge(aff.drop_duplicates(src_key)[[*src_key, "geo_name", "province_code"]], on=src_key, how="left")
    g = g.sort_values(["_size", "geo_code"], ascending=[False, True], kind="mergesort")
    geo = g.groupby(tk, sort=False).agg(
        valid_votes=("valid_votes", "sum"),
        eligible=("eligible", "sum"),
        ballots_cast=("ballots_cast", "sum"),
        province_code=("province_code", "first"),
        geo_name=("geo_name", "first"),
    )
    geo = geo.reset_index()
    old_names = dict(zip(df.loc[is_level, "geo_code"], df.loc[is_level, "geo_name"], strict=False))
    out = lines.merge(geo, on=tk, how="left").rename(columns={"to_code": "geo_code"})
    if round_votes:
        out["votes"] = np.rint(out["votes"].to_numpy()).astype(np.int64)
        out["valid_votes"] = out.groupby(list(GEO_KEY), sort=False)["votes"].transform("sum").astype(np.int64)
        out["ballots_cast"] = np.maximum(np.rint(out["ballots_cast"].to_numpy()), out["valid_votes"]).astype(
            np.int64
        )
        out["eligible"] = np.maximum(np.rint(out["eligible"].to_numpy()), out["ballots_cast"]).astype(
            np.int64
        )
    valid = out["valid_votes"].to_numpy(dtype=float)
    out["share"] = np.divide(
        out["votes"].to_numpy(dtype=float), valid, out=np.zeros(len(out)), where=valid > 0
    )
    ranked = ranked_lines(out)
    top = ranked.index[(ranked["_rank"] == 0).to_numpy() & ((ranked["votes"] > 0) | ranked["_w"]).to_numpy()]
    out["winner"] = False
    out.loc[top, "winner"] = True
    at_out = (out["level"] == level).to_numpy()
    codes = out["geo_code"].to_numpy(dtype=object)
    out["geo_name"] = np.where(
        at_out,
        np.array([names.get(c, old_names.get(c, c)) for c in codes], dtype=object),
        out["geo_name"].to_numpy(dtype=object),
    )
    out["province_code"] = np.where(
        at_out,
        np.array(
            [province_codes.get(c, p) for c, p in zip(codes, out["province_code"], strict=True)], dtype=object
        ),
        out["province_code"].to_numpy(dtype=object),
    )
    log.debug("remap_lineage: %d source race-geos → %d target race-geos", len(aff_keys), len(changed))
    result = pd.concat([passthrough, normalize_dtypes(out, integer_counts=round_votes)], ignore_index=True)
    return result.sort_values([*GEO_KEY, "line_key"], kind="mergesort").reset_index(drop=True)


# =========================================================================== comparisons
COMPARE_COLUMNS: tuple[str, ...] = (
    *SWING_COLUMNS,
    "winner_prev",
    "winner_curr",
    "flip_status",
    "turnout_prev",
    "turnout_curr",
    "turnout_change_pp",
)


def _prepare_pair(
    prev: pd.DataFrame,
    curr: pd.DataFrame,
    level: str,
    race_type: str | RaceType | None,
    lineage: pd.DataFrame | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    p = at_level(filter_family(prev, race_type), level)
    c = at_level(filter_family(curr, race_type), level)
    if p.empty or c.empty:
        raise ResultsFrameError(f"no rows at level {level!r} in one of the elections")
    if lineage is not None:
        meta = c.drop_duplicates("geo_code")
        p = remap_lineage(
            p,
            lineage,
            level=level,
            names=dict(zip(meta["geo_code"], meta["geo_name"], strict=True)),
            province_codes=dict(zip(meta["geo_code"], meta["province_code"], strict=True)),
        )
    return p, c


def compare_elections(
    prev: pd.DataFrame,
    curr: pd.DataFrame,
    level: str,
    *,
    race_type: str | RaceType | None = None,
    lineage: pd.DataFrame | None = None,
    parties: Sequence[str] | None = None,
    by: GroupBy = "family",
) -> pd.DataFrame:
    """Tidy swing table between two elections at ``level`` (one row per geo × party).

    Columns: those of :func:`app.analytics.metrics.swing` plus the geo's winner in both
    elections, its ``flip_status`` (hold/flip/new/dropped/undecided) and turnout change.  With
    ``lineage`` the previous election's rows are first remapped onto the current code set
    (:func:`remap_lineage`) so mergers between cycles do not produce spurious new/dropped geos.
    ``by='family'`` (default) pools a family's races per geo; ``race_type`` restricts the family.
    """
    p, c = _prepare_pair(prev, curr, level, race_type, lineage)
    cols = ["race_code" if by == "race" else "race_family", "level", "geo_code"]
    sw = swing(p, c, level=level, by=by, parties=parties)
    fl = flips(p, c, level=level, by=by)[[*cols, "winner_prev", "winner_curr", "status"]].rename(
        columns={"status": "flip_status"}
    )
    tc = turnout_change(p, c, level=level, by=by)[
        [*cols, "turnout_prev", "turnout_curr", "turnout_change_pp"]
    ]
    out = sw.merge(fl, on=cols, how="left").merge(tc, on=cols, how="left")
    return out.loc[:, list(COMPARE_COLUMNS)]


def municipality_flips(
    prev: pd.DataFrame,
    curr: pd.DataFrame,
    *,
    race_type: str | RaceType | None = RaceType.PRESIDENT,
    lineage: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Municipalities whose winning party changed (lineage-aware; family-pooled per municipality).

    Returns the full :func:`app.analytics.metrics.flips` table at level ``municipality``; filter
    ``status == 'flip'`` for the flips only.
    """
    p, c = _prepare_pair(prev, curr, "municipality", race_type, lineage)
    return flips(p, c, level="municipality", by="family").loc[:, list(FLIP_COLUMNS)]


# =========================================================================== series
SERIES_COLUMNS: tuple[str, ...] = (
    "election_id",
    "year",
    "race_family",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "party",
    "votes",
    "valid_votes",
    "share",
    "share_pp",
    "national_share",
    "lean_pp",
    "won",
    "change_pp",
)


def _geo_party_series(df: pd.DataFrame, level: str, geo_code: str, party: str) -> pd.DataFrame:
    rows = at_level(df, level)
    rows = rows[(rows["geo_code"] == geo_code).to_numpy()]
    if rows.empty:
        return pd.DataFrame(columns=list(SERIES_COLUMNS))
    ps = party_shares(rows, by="family")
    base = ps.drop_duplicates(["election_id", "race_family"])[
        [
            "election_id",
            "year",
            "race_family",
            "level",
            "geo_code",
            "geo_name",
            "province_code",
            "valid_votes",
        ]
    ]
    pr = ps[(ps["party"] == party).to_numpy()][["election_id", "race_family", "votes", "share", "won"]]
    out = base.merge(pr, on=["election_id", "race_family"], how="left")
    valid = out["valid_votes"].to_numpy(dtype=float)
    out["votes"] = out["votes"].fillna(0).astype(np.int64)
    out["share"] = np.where(valid > 0, out["share"].fillna(0.0).to_numpy(dtype=float), np.nan)
    out["won"] = as_bool(out["won"])
    nat = national_party_totals(filter_family(df, list(set(out["race_family"]))))
    nat = nat[(nat["party"] == party).to_numpy()][["election_id", "race_family", "share"]].rename(
        columns={"share": "national_share"}
    )
    out = out.merge(nat, on=["election_id", "race_family"], how="left")
    out["national_share"] = out["national_share"].fillna(0.0)
    out["party"] = party
    out["share_pp"] = 100.0 * out["share"]
    out["lean_pp"] = 100.0 * (out["share"] - out["national_share"])
    out = out.sort_values(["race_family", "year", "election_id"], kind="mergesort")
    out["change_pp"] = out.groupby("race_family", sort=False)["share_pp"].diff()
    out = out.sort_values(["year", "election_id", "race_family"], kind="mergesort").reset_index(drop=True)
    return out.loc[:, list(SERIES_COLUMNS)]


def province_trend(
    frames: pd.DataFrame | Iterable[pd.DataFrame],
    province: str,
    party: str,
    *,
    race_type: str | RaceType | None = RaceType.PRESIDENT,
) -> pd.DataFrame:
    """A party's share in a province across elections, with its national share, the province's
    lean (pp) and the change since the previous election of the same family."""
    df = filter_family(as_frame(frames), race_type)
    return _geo_party_series(df, "province", province, party)


def party_support_series(
    frames: pd.DataFrame | Iterable[pd.DataFrame],
    geo_code: str,
    party: str,
    *,
    since_year: int | None = None,
    race_type: str | RaceType | None = None,
    level: str | None = None,
) -> pd.DataFrame:
    """A party's support at any geo across elections, e.g. Tilburg (``GM0855``) since 2028.

    ``level`` is inferred from where ``geo_code`` occurs when omitted.  One row per election ×
    race family (a municipality split across House districts is pooled); elections where the
    party did not run at the geo show 0 votes.
    """
    df = filter_family(as_frame(frames), race_type)
    if since_year is not None:
        df = df[(df["year"] >= since_year).to_numpy()]
    if level is None:
        levels = sorted(set(df.loc[(df["geo_code"] == geo_code).to_numpy(), "level"]))
        if not levels:
            return pd.DataFrame(columns=list(SERIES_COLUMNS))
        if len(levels) > 1:
            raise ResultsFrameError(f"geo {geo_code!r} occurs at levels {levels}; pass level=")
        level = levels[0]
    return _geo_party_series(df, level, geo_code, party)


DISTRICT_HISTORY_COLUMNS: tuple[str, ...] = (
    "election_id",
    "year",
    "race_code",
    "geo_code",
    "geo_name",
    "province_code",
    "winner_candidate",
    "winner_party",
    "winner_share",
    "runner_up_candidate",
    "runner_up_party",
    "margin_votes",
    "margin_pp",
    "valid_votes",
    "turnout",
    "previous_winner_party",
    "status",
)


def district_history(frames: pd.DataFrame | Iterable[pd.DataFrame], district_code: str) -> pd.DataFrame:
    """House results of one district code across elections: winner, margin, turnout and
    ``status`` (``first`` / ``hold`` / ``flip`` / ``undecided``) relative to the previous election.

    District codes are matched literally; after a redistricting the same code may cover a
    different territory (compare the district plans before reading a flip as a swing).
    """
    df = as_frame(frames)
    rows = df[
        (df["race_type"].astype(str) == RaceType.HOUSE.value).to_numpy()
        & (df["level"] == "district").to_numpy()
        & (df["geo_code"] == district_code).to_numpy()
    ]
    mt = margin_table(rows)
    if mt.empty:
        return pd.DataFrame(columns=list(DISTRICT_HISTORY_COLUMNS))
    mt = mt.sort_values(["year", "election_id"], kind="mergesort").reset_index(drop=True)
    mt["turnout"] = np.divide(
        mt["ballots_cast"].to_numpy(dtype=float),
        mt["eligible"].to_numpy(dtype=float),
        out=np.full(len(mt), np.nan),
        where=mt["eligible"].to_numpy(dtype=float) > 0,
    )
    prev_w = mt["winner_party"].shift(1)
    mt["previous_winner_party"] = prev_w.where(prev_w.notna(), None)
    first = np.arange(len(mt)) == 0
    undecided = mt["winner_party"].isna().to_numpy() | (
        ~first & mt["previous_winner_party"].isna().to_numpy()
    )
    same = (mt["winner_party"] == mt["previous_winner_party"]).to_numpy()
    mt["status"] = np.select([first, undecided, same], ["first", "undecided", "hold"], default="flip")
    return mt.loc[:, list(DISTRICT_HISTORY_COLUMNS)]


def seat_totals(
    frames: pd.DataFrame | Iterable[pd.DataFrame],
    race_type: str | RaceType = RaceType.HOUSE,
    *,
    majority: int | None = None,
) -> pd.DataFrame:
    """Seats won per election × party in the seat contests of ``race_type`` (strict race type),
    with ``seats_total`` (contests held), ``majority`` (default: majority of the contests held)
    and ``has_majority``.  Senate control additionally depends on holdover seats not in the frame.

    Proportional party-list races (:data:`~app.analytics.results.PROPORTIONAL_RACE_TYPES`) are
    rejected with :class:`ValueError`: their D'Hondt seat counts cannot be derived from the frame.
    """
    df = as_frame(frames)
    rt = normalize_values(race_type)
    proportional = sorted(set(rt) & PROPORTIONAL_RACE_TYPES)
    if proportional:
        raise ValueError(
            f"seat_totals: {proportional} are proportional (D'Hondt) multi-seat races; their seats are "
            "not derivable from a results frame (see app.elections.seats.dhondt)"
        )
    rows = seat_contests(df[df["race_type"].astype(str).isin(rt).to_numpy()])
    cols = ["election_id", "year", "party", "seats", "seats_total", "majority", "has_majority"]
    if rows.empty:
        return pd.DataFrame(columns=cols)
    mt = margin_table(rows)
    tot = mt.groupby("election_id").size().rename("seats_total")
    won = mt[mt["winner_party"].notna().to_numpy()]
    seats = won.groupby(["election_id", "winner_party"]).agg(
        year=("year", "first"), seats=("race_code", "size")
    )
    seats = (
        seats.reset_index()
        .rename(columns={"winner_party": "party"})
        .merge(tot.reset_index(), on="election_id")
    )
    seats["majority"] = [
        majority if majority is not None else majority_of(int(t)) for t in seats["seats_total"]
    ]
    seats["has_majority"] = seats["seats"] >= seats["majority"]
    seats = seats.sort_values(
        ["election_id", "seats", "party"], ascending=[True, False, True], kind="mergesort"
    )
    for c in ("seats", "seats_total", "majority"):
        seats[c] = seats[c].astype(np.int64)
    return seats.reset_index(drop=True).loc[:, cols]


__all__ = [
    "COMPARE_COLUMNS",
    "DISTRICT_HISTORY_COLUMNS",
    "DIVERGENCE_COLUMNS",
    "RECORD_COLUMNS",
    "SERIES_COLUMNS",
    "closest_races",
    "compare_elections",
    "district_history",
    "ec_pv_divergence",
    "largest_landslides",
    "municipality_flips",
    "party_support_series",
    "province_trend",
    "remap_lineage",
    "seat_totals",
]
