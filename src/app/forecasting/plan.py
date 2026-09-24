"""Forecast plan: everything about a Monte Carlo forecast that does not depend on the draw.

:func:`prepare_forecast` turns a structural model, the races and the election context into a
compact, picklable :class:`ForecastPlan` (a few MB even for the real country), which the Monte
Carlo loop (:mod:`app.forecasting.engine`) evaluates for blocks of draws in worker processes.

**Cells.**  Simulating 14.7k neighbourhoods × 100k draws is unnecessary: every random component
of the simulation model below the municipality is independent noise that averages out.  Units are
therefore grouped into *fragments* — the finest partition of the country such that every fragment
lies in one municipality and, for every race whose jurisdiction does not follow municipal borders
(House districts), in one race.  Races whose jurisdictions are unions of whole municipalities
(provinces, municipalities) are evaluated on municipality cells; the others on fragments.

**Party state per fragment.**  With eligible voters ``E_u``, preference shares ``s_up`` and
supporter turnout ``q_up`` of the model expectation (``app.simulation.voting.expected_party_state``):

    s̄_fp = Σ E_u s_up / Σ E_u          q̄_fp = Σ E_u s_up q_up / Σ E_u s_up
    V_fp = log s̄_fp                    Tq_fp = logit q̄_fp

so that at zero shocks the fragment's turnout and vote shares are *exactly* the eligible-weighted
aggregates of the unit expectation.  Shocks are added to ``V`` (party utility) and ``Tq`` (turnout);
utility shocks pass through per-fragment maps ``A`` that equate the fragment's share Jacobian with
the exact unit-aggregated one (national shocks including the units' elasticity), and turnout shocks
are scaled per party, so a fragment responds to shocks like the sum of its neighbourhoods
(:func:`_fragment_model`).

**Races per cell.**  Each race's :class:`~app.simulation.voting.RacePlan` (routing ``R``, undervote
``a``, independent mass, candidate effects ``Δ``, strategic-voting matrix ``G``, blank / invalid
rates) is aggregated to its cells (``Δ`` and rates weighted by expected ballots).  A multiplicative
*anchor* per cell and line makes the zero-shock line shares equal the unit-level model expectation
aggregated with expected valid votes, so the forecast is centred exactly on the model; the shocked
draws then move around it through the same logit pipeline as ``simulate_election``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import numpy as np

from app.core.config import get_constitution
from app.core.constitution import ConstitutionConfig, EVAllocationMethod, RaceType, majority_of
from app.core.errors import ElectionError
from app.core.logging import Timer, get_logger
from app.core.rng import config_hash
from app.elections.seats import INDEPENDENT
from app.elections.types import RaceSpec
from app.forecasting import kernel
from app.forecasting.config import ForecastConfig
from app.simulation.spatial import gp_cholesky
from app.simulation.structural import StructuralModel
from app.simulation.voting import (
    PRESIDENTIAL_LINE_SCOPE,
    ElectionContext,
    RacePlan,
    expected_party_state,
    prepare_race,
    race_units,
)

log = get_logger(__name__)

SOURCE_FRAGMENT = "fragment"
SOURCE_MUNICIPALITY = "municipality"
_F32 = np.float32


# --------------------------------------------------------------------------- plan structures
@dataclass(frozen=True)
class RaceInfo:
    """Static description of one forecast race (``derived`` = assembled from other races)."""

    key: str
    race_type: str
    line_keys: tuple[str, ...]
    line_parties: tuple[str | None, ...]
    line_labels: tuple[str, ...]
    layer: int  # −1 for derived races
    local: int  # index within the layer (−1 for derived races)
    province_code: str | None = None
    district_code: str | None = None
    municipality_code: str | None = None
    electoral_votes: int | None = None
    derived: bool = False
    #: Deterministic (zero-shock) model expectation of the race's line shares.
    expected_shares: tuple[float, ...] = ()
    #: Expected valid votes of the race at the model expectation.
    expected_valid: float = 0.0


@dataclass(frozen=True)
class ShockParams:
    """Resolved standard deviations of the forecast's random components (logit points)."""

    national_sd: float
    tail_df: float
    ideological_swing_share: float
    province_sd: float
    municipality_sd: float
    spatial_sd: float
    spatial_length_km: float
    kernel: str
    unit_sd: float
    turnout_national_sd: float
    turnout_local_sd: float
    turnout_unit_share: float
    party_turnout_sd: float
    race_line_sd: float
    events: bool

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


@dataclass
class LayerPlan:
    """A set of races with disjoint jurisdictions, evaluated together on cells.

    Cells are sorted by race (``race_starts`` are the offsets of each race's first cell).
    Per-cell arrays are float32; padding lines (beyond a race's own lines) have ``delta = −inf``.
    """

    index: int
    race_type: str
    source: str  # SOURCE_FRAGMENT | SOURCE_MUNICIPALITY
    races: np.ndarray  # (R_l,) indices into ForecastPlan.races
    cell_src: np.ndarray  # (C,) fragment index or municipality position
    cell_race: np.ndarray  # (C,) local race index
    cell_muni: np.ndarray  # (C,) frame municipality index of each cell
    race_starts: np.ndarray  # (R_l,)
    n_lines: np.ndarray  # (R_l,)
    R: np.ndarray  # (C, P, L)
    a: np.ndarray  # (C, P)
    indep: np.ndarray  # (C, L)
    delta: np.ndarray  # (C, L)
    G: np.ndarray | None  # (C, L, L) or None when no race of the layer has strategic voting
    ratio: np.ndarray  # (C, L)
    blank_base: np.ndarray  # (C,)
    invalid: np.ndarray  # (C,)
    #: Line-performance shock of every cell line: index into the layer's ``n_shocks`` shocks
    #: (``n_shocks`` = padding, always 0).  Presidential tickets share one shock across provinces.
    cell_shock: np.ndarray  # (C, L)
    n_shocks: int

    @property
    def n_races(self) -> int:
        return len(self.races)

    @property
    def n_cells(self) -> int:
        return len(self.cell_src)

    @property
    def L(self) -> int:
        return int(self.R.shape[2])


@dataclass
class PresidentialPlan:
    """Electoral College structure: province contests, tickets, EV, allocation method."""

    layer: int
    races: np.ndarray  # (Pv,) indices into ForecastPlan.races, in EV-map order
    local: np.ndarray  # (Pv,) local race index within the layer
    province_codes: list[str]
    ev: np.ndarray  # (Pv,) int64
    tickets: list[str]
    ticket_parties: list[str | None]
    ticket_labels: list[str]
    line_ticket: np.ndarray  # (Pv, L_layer) ticket index, −1 = padding / not a ticket
    method: str
    majority: int
    total_ev: int
    statewide_ev: int
    parent: int | None  # index of the derived national race in ForecastPlan.races
    muni_perm: np.ndarray | None  # cell permutation making municipalities contiguous (None: identity)
    muni_starts: np.ndarray  # offsets of each municipality group in the (permuted) cells
    muni_index: np.ndarray  # (M_p,) frame municipality index of each group
    district_perm: np.ndarray | None = None  # DISTRICT method: cell permutation by district
    district_starts: np.ndarray | None = None
    district_province: np.ndarray | None = None  # (D,) index into province_codes
    district_codes: list[str] = field(default_factory=list)
    #: Deterministic model expectation of the national popular-vote shares per ticket.
    expected_pv: np.ndarray = field(default_factory=lambda: np.zeros(0))


@dataclass
class ChamberPlan:
    """Seat bookkeeping of a chamber (House / Senate) or of the governorships."""

    name: str
    races: np.ndarray  # indices into ForecastPlan.races
    keys: list[str]  # party codes (+ INDEPENDENT)
    holdover: np.ndarray  # (K,) int64 seats not up for election
    majority: int | None
    #: (layer index, local race ids (R_s,), key index per race line (R_s, L_layer), −1 = padding)
    parts: list[tuple[int, np.ndarray, np.ndarray]]

    @property
    def seats_up(self) -> int:
        return len(self.races)

    @property
    def seats_total(self) -> int:
        return int(self.seats_up + self.holdover.sum())


@dataclass
class ForecastPlan:
    """Draw-independent inputs of the Monte Carlo loop (picklable; no model reference)."""

    party_codes: list[str]
    province_codes: list[str]
    muni_codes: list[str]
    races: list[RaceInfo]
    layers: list[LayerPlan]
    # fragments (sorted by municipality)
    frag_muni: np.ndarray  # (F,) frame municipality index
    frag_prov: np.ndarray  # (F,) province index
    frag_eligible: np.ndarray  # (F,) float32
    V: np.ndarray  # (F, P) float32 log preference shares
    Tq: np.ndarray  # (F, P) float32 supporter turnout logits
    A_nat_T: np.ndarray  # (F, P, P) float32: national shock → fragment utility (transposed)
    A_loc_T: np.ndarray  # (F, P, P) float32: local shock → fragment utility (transposed)
    turnout_gain: np.ndarray  # (F, P) float32: turnout-shock sensitivity per party supporters
    cell_sd: np.ndarray  # (F,) float32: 1 / sqrt(effective neighbourhoods)
    muni_present: np.ndarray  # (M_p,) frame municipality indices with fragments (fragment order)
    muni_starts: np.ndarray  # (M_p,) first fragment of each municipality
    muni_pos: np.ndarray  # (M,) position of each frame municipality in muni_present (−1 = none)
    needs_munis: bool
    total_eligible: float
    ideology_unit: np.ndarray  # (P, 3) ideology / RMS norm (common ideological swing)
    event_prob: np.ndarray  # (E,)
    event_national: np.ndarray  # (E, P)
    event_province: np.ndarray  # (E, Pv, P)
    event_turnout: np.ndarray  # (E,)
    event_names: list[str]
    #: (M, M) float32 Cholesky factor of the municipal error covariance
    #: ``municipality_sd² I + spatial_sd² K`` (None without a spatial component)
    muni_chol: np.ndarray | None
    shocks: ShockParams
    q_min: float
    q_max: float
    president: PresidentialPlan | None
    house: ChamberPlan | None
    senate: ChamberPlan | None
    governors: ChamberPlan | None
    constitution: dict[str, int]
    block_size: int
    share_bins: int
    municipality_distributions: bool
    metadata: dict[str, Any]

    @property
    def n_parties(self) -> int:
        return len(self.party_codes)

    @property
    def n_fragments(self) -> int:
        return len(self.frag_muni)


# --------------------------------------------------------------------------- helpers
def _cell_mean(inv: np.ndarray, n_cells: int, x: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted mean of ``x`` (n,) or (n, K) per cell (unweighted where a cell's weights sum to 0)."""
    den = np.bincount(inv, weights=w, minlength=n_cells)
    cnt = np.bincount(inv, minlength=n_cells).astype(float)
    use_w = den > 0
    ww = np.where(use_w[inv], w, 1.0)
    den = np.where(use_w, den, np.maximum(cnt, 1.0))
    if x.ndim == 1:
        return np.bincount(inv, weights=ww * x, minlength=n_cells) / den
    out = np.empty((n_cells, x.shape[1]))
    for k in range(x.shape[1]):
        out[:, k] = np.bincount(inv, weights=ww * x[:, k], minlength=n_cells) / den
    return out


def _starts(groups: np.ndarray) -> np.ndarray:
    """Offsets where a sorted group-label array changes (first entry 0)."""
    if len(groups) == 0:
        return np.zeros(0, dtype=np.int64)
    return np.flatnonzero(np.r_[True, groups[1:] != groups[:-1]]).astype(np.int64)


def _resolve_shocks(model: StructuralModel, cfg: ForecastConfig, with_polls: bool) -> ShockParams:
    sc = model.scenario.environment.shocks
    mc = model.config
    e = cfg.errors
    s = float(e.scale)

    def pick(v: float | None, default: float) -> float:
        return float(default if v is None else v)

    national = pick(e.national_sd, sc.national_sd) * s
    if with_polls:
        national *= float(cfg.polls.national_sd_scale)
    return ShockParams(
        national_sd=national,
        tail_df=pick(e.tail_df, sc.tail_df),
        ideological_swing_share=pick(e.ideological_swing_share, mc.shocks.ideological_swing_share),
        province_sd=pick(e.province_sd, sc.province_sd) * s,
        municipality_sd=pick(e.municipality_sd, sc.municipality_sd) * s,
        spatial_sd=pick(e.spatial_sd, sc.spatial_sd) * s,
        spatial_length_km=pick(e.spatial_length_km, sc.spatial_length_km),
        kernel=str(mc.lean.kernel),
        unit_sd=pick(e.unit_sd, sc.unit_sd) * s,
        turnout_national_sd=pick(e.turnout_national_sd, sc.turnout_national_sd),
        turnout_local_sd=pick(e.turnout_local_sd, sc.turnout_local_sd),
        turnout_unit_share=pick(e.turnout_unit_share, mc.turnout.unit_shock_share),
        party_turnout_sd=pick(e.party_turnout_sd, mc.turnout.party_turnout_shock_sd),
        race_line_sd=pick(e.race_line_sd, mc.candidates.race_line_sd) * s,
        events=bool(e.events),
    )


def _race_signature(race: RaceSpec, units: np.ndarray) -> dict[str, Any]:
    return {
        "key": race.key,
        "type": str(race.race_type),
        "system": str(race.electoral_system),
        "seats": race.seats,
        "ev": race.electoral_votes,
        "incumbent_party": race.incumbent_party,
        "units": config_hash(np.ascontiguousarray(units, dtype=np.int64).tobytes()),
        "lines": [
            [
                ln.key,
                ln.party_code,
                ln.candidate_key,
                ln.running_mate_key,
                round(float(ln.quality), 9),
                ln.incumbent,
                ln.home_province,
                ln.home_municipality,
                ln.running_mate_home_province,
                ln.withdrawn,
            ]
            for ln in race.lines
        ],
    }


def _context_signature(ctx: ElectionContext) -> dict[str, Any]:
    def keyed(m: Mapping[Any, Any]) -> dict[str, Any]:
        return {(":".join(k) if isinstance(k, tuple) else str(k)): v for k, v in m.items()}

    return {
        "year": ctx.year,
        "election_type": ctx.election_type,
        "president_party": ctx.president_party,
        "incumbents": {k: [v.party, v.candidate_key, v.running] for k, v in ctx.incumbents.items()},
        "candidates": sorted(ctx.candidates),
        "campaign_effects": keyed({k: dict(v) for k, v in ctx.campaign_effects.items()}),
        "turnout_effects": keyed(ctx.turnout_effects),
        "party_turnout_effects": keyed({k: dict(v) for k, v in ctx.party_turnout_effects.items()}),
        "national_shifts": dict(ctx.national_shifts),
        "home_province_bonus": ctx.home_province_bonus,
        "vp_home_province_bonus": ctx.vp_home_province_bonus,
        "include_environment": ctx.include_environment,
    }


def _merge_poll_shift(
    model: StructuralModel, ctx: ElectionContext, poll_shift: Mapping[str, float] | None, weight: float
) -> tuple[ElectionContext, dict[str, float]]:
    """Context whose ``national_shifts`` include ``weight × poll_shift`` (party logit shifts)."""
    if not poll_shift:
        return ctx, {}
    applied: dict[str, float] = {}
    for code, v in poll_shift.items():
        if code not in model.party_index:
            raise ElectionError(f"poll shift for unknown party {code!r} (expected party codes)")
        x = float(v)
        if not np.isfinite(x):
            raise ElectionError(f"poll shift for {code} is not finite")
        applied[str(code)] = weight * x
    merged = dict(ctx.national_shifts)
    for code, v in applied.items():
        merged[code] = float(merged.get(code, 0.0)) + v
    return replace(ctx, national_shifts=merged), applied


def _layer_groups(
    races: Sequence[RaceSpec], units: Sequence[np.ndarray], n_units: int
) -> list[tuple[str, list[int]]]:
    """Pack races into layers of one race type with pairwise-disjoint jurisdictions (greedy)."""
    layers: list[tuple[str, list[int], np.ndarray]] = []
    for i, race in enumerate(races):
        rt = str(RaceType(race.race_type))
        u = units[i]
        for lrt, members, occ in layers:
            if lrt == rt and not occ[u].any():
                members.append(i)
                occ[u] = True
                break
        else:
            occ = np.zeros(n_units, dtype=bool)
            occ[u] = True
            layers.append((rt, [i], occ))
    return [(rt, members) for rt, members, _ in layers]


def _muni_aligned(unit_lists: Sequence[np.ndarray], unit_muni: np.ndarray, muni_count: np.ndarray) -> bool:
    """True when every race covers whole municipalities."""
    for u in unit_lists:
        c = np.bincount(unit_muni[u], minlength=len(muni_count))
        touched = c > 0
        if not np.array_equal(c[touched], muni_count[touched]):
            return False
    return True


def _pad(x: np.ndarray, L: int, fill: float) -> np.ndarray:
    out = np.full((*x.shape[:-1], L), fill, dtype=float)
    out[..., : x.shape[-1]] = x
    return out


# --------------------------------------------------------------------------- fragment model
@dataclass
class FragmentModel:
    """Party state of the fragments and their first-order shock sensitivities (float64)."""

    V: np.ndarray  # (F, P) log preference shares
    Tq: np.ndarray  # (F, P) supporter turnout logits
    eligible: np.ndarray  # (F,)
    neff: np.ndarray  # (F,) effective number of neighbourhoods
    A_nat: np.ndarray  # (F, P, P)
    A_loc: np.ndarray  # (F, P, P)
    turnout_gain: np.ndarray  # (F, P)


def share_jacobian(
    group: np.ndarray,
    n_groups: int,
    weight: np.ndarray,
    shift: np.ndarray,
    s: np.ndarray,
    q: np.ndarray,
    T: np.ndarray,
) -> np.ndarray:
    """Jacobian (G, P, P) of each group's vote shares with respect to a party-utility shock.

    Members ``u`` (eligible ``weight``) with preference ``s_u``, supporter turnout ``q_u`` and
    turnout ``T_u`` receive the shock ``shift_u · δ``; the group's vote share is
    ``π̄_p = Σ E s_p q_p / Σ E T``, so ``∂π̄_p/∂δ_k = (Σ E h q_p s_p (1[p=k] − s_k) −
    π̄_p Σ E h s_k (q_k − T)) / Σ E T`` with ``h = shift``.
    """
    P = s.shape[1]
    wh = weight * shift
    qs = q * s
    B = np.bincount(group, weights=weight * T, minlength=n_groups)
    votes = np.column_stack(
        [np.bincount(group, weights=weight * qs[:, p], minlength=n_groups) for p in range(P)]
    )
    pi_bar = votes / np.maximum(B, 1e-300)[:, None]
    J = np.zeros((n_groups, P, P))
    for p in range(P):
        J[:, p, p] = np.bincount(group, weights=wh * qs[:, p], minlength=n_groups)
        for k in range(P):
            J[:, p, k] -= np.bincount(group, weights=wh * qs[:, p] * s[:, k], minlength=n_groups)
    J2 = np.column_stack(
        [np.bincount(group, weights=wh * s[:, k] * (q[:, k] - T), minlength=n_groups) for k in range(P)]
    )
    J -= pi_bar[:, :, None] * J2[:, None, :]
    return J / np.maximum(B, 1e-300)[:, None, None]


def _match_jacobian(J_cell: np.ndarray, J_target: np.ndarray) -> np.ndarray:
    """``A`` (G, P, P) with ``J_cell A ≈ J_target`` (ridge towards the identity for directions the
    cell Jacobian does not identify, e.g. a common shift of every party or negligible parties)."""
    P = J_cell.shape[1]
    JtJ = np.einsum("gkp,gkq->gpq", J_cell, J_cell)
    lam = 1e-6 * np.maximum(np.trace(JtJ, axis1=1, axis2=2) / P, 1e-30)
    eye = np.eye(P)[None, :, :]
    lhs = JtJ + lam[:, None, None] * eye
    rhs = np.einsum("gkp,gkq->gpq", J_cell, J_target) + lam[:, None, None] * eye
    return np.linalg.solve(lhs, rhs)


def _fragment_model(
    model: StructuralModel,
    expected: Any,
    frag_of_unit: np.ndarray,
    F: int,
    q_min: float,
    q_max: float,
) -> FragmentModel:
    """Aggregate the unit expectation to fragments and match the first-order shock responses.

    A fragment's single multinomial logit reacts more strongly to a shock than the sum of its
    heterogeneous neighbourhoods (Jensen).  Utility shocks are therefore mapped through ``A`` so
    that the fragment's share Jacobian equals the exact unit-aggregated Jacobian (national shocks
    include each unit's elasticity), and turnout shocks are scaled per party by the ratio of the
    vote-count derivatives ``Σ E s q (1 − q)`` (zero where the turnout probability is clipped).
    """
    P = model.n_parties
    E = model.eligible.astype(float)
    s = expected.preference
    T = expected.turnout
    q = expected.vote_share * T[:, None] / np.maximum(s, 1e-300)
    Ef = np.bincount(frag_of_unit, weights=E, minlength=F)
    w = np.where(Ef[frag_of_unit] > 0, E, 1.0)
    Sfp = np.column_stack([np.bincount(frag_of_unit, weights=w * s[:, p], minlength=F) for p in range(P)])
    Qfp = np.column_stack(
        [np.bincount(frag_of_unit, weights=w * s[:, p] * q[:, p], minlength=F) for p in range(P)]
    )
    s_bar = Sfp / np.maximum(Sfp.sum(axis=1, keepdims=True), 1e-300)
    q_bar = np.clip(Qfp / np.maximum(Sfp, 1e-300), 1e-6, 1.0 - 1e-6)
    wf = np.bincount(frag_of_unit, weights=w, minlength=F)
    T_bar = (s_bar * q_bar).sum(axis=1)
    # --- utility shocks: match the unit-aggregated Jacobians ---------------------------------
    cells = np.arange(F)
    J_cell = share_jacobian(cells, F, wf, np.ones(F), s_bar, q_bar, T_bar)
    J_loc = share_jacobian(frag_of_unit, F, w, np.ones(len(w)), s, q, T)
    J_nat = share_jacobian(frag_of_unit, F, w, model.elasticity, s, q, T)
    # --- turnout shocks: per-party vote-count derivative ratio -----------------------------------
    free = (q > q_min + 1e-9) & (q < q_max - 1e-9)
    dq = np.where(free, q * (1.0 - q), 0.0)
    unit_d = np.column_stack(
        [np.bincount(frag_of_unit, weights=w * s[:, p] * dq[:, p], minlength=F) for p in range(P)]
    )
    cell_free = (q_bar > q_min + 1e-9) & (q_bar < q_max - 1e-9)
    cell_d = wf[:, None] * s_bar * np.where(cell_free, q_bar * (1.0 - q_bar), 0.0)
    gain = np.where(cell_d > 1e-12 * np.maximum(wf, 1.0)[:, None], unit_d / np.maximum(cell_d, 1e-300), 1.0)
    w2 = np.bincount(frag_of_unit, weights=w * w, minlength=F)
    return FragmentModel(
        V=np.log(np.maximum(s_bar, kernel.TINY)),
        Tq=np.log(q_bar / (1.0 - q_bar)),
        eligible=Ef,
        neff=np.maximum(wf**2 / np.maximum(w2, 1e-300), 1.0),
        A_nat=_match_jacobian(J_cell, J_nat),
        A_loc=_match_jacobian(J_cell, J_loc),
        turnout_gain=np.clip(gain, 0.0, 10.0),
    )


# --------------------------------------------------------------------------- preparation
def prepare_forecast(
    model: StructuralModel,
    races: Sequence[RaceSpec],
    context: ElectionContext | None = None,
    *,
    config: ForecastConfig,
    ev_by_province: Mapping[str, int] | None = None,
    constitution: ConstitutionConfig | None = None,
    holdover_senate: Mapping[str, int] | None = None,
    poll_shift: Mapping[str, float] | None = None,
    ev_method: EVAllocationMethod | str | None = None,
) -> ForecastPlan:
    """Precompute the draw-independent :class:`ForecastPlan` (see the module docstring).

    ``poll_shift`` (party → logit shift, e.g. from ``app.polling.blend.shift_from_poll_average``)
    is added, times ``config.polls.weight``, to the context's national shifts (scaled by local
    elasticity, exactly like ``ElectionContext.national_shifts`` in ``simulate_election``).
    ``ev_method`` defaults to the scenario's ``electoral_college.allocation``.
    """
    const = constitution or get_constitution()
    ctx0 = context if context is not None else ElectionContext.from_scenario(model.scenario)
    ctx, applied_polls = _merge_poll_shift(model, ctx0, poll_shift, float(config.polls.weight))
    f = model.frame
    if not races:
        raise ElectionError("a forecast needs at least one race")
    keys = [r.key for r in races]
    if len(set(keys)) != len(keys):
        raise ElectionError("duplicate race keys")
    method = EVAllocationMethod(
        ev_method if ev_method is not None else model.scenario.electoral_college.allocation
    )

    with Timer(log, f"forecast plan ({len(races)} races)"):
        # --- races: the national PRES race is derived from its province contests -------------
        children = [r for r in races if RaceType(r.race_type) == RaceType.PRESIDENT_PROVINCE]
        parents = [r for r in races if RaceType(r.race_type) == RaceType.PRESIDENT]
        if len(parents) > 1:
            raise ElectionError("more than one national presidential race")
        derived_parent = parents[0] if parents and children else None
        sim_races = [r for r in races if r is not derived_parent]
        sim_units = [race_units(model, r) for r in sim_races]
        for r, u in zip(sim_races, sim_units, strict=True):
            if u.size == 0:
                raise ElectionError(f"race {r.key} has no units")
        expected = expected_party_state(model, ctx)
        plans = [prepare_race(model, r, ctx, expected) for r in sim_races]

        # --- layers, fragments and the fragment party model --------------------------------------
        groups = _layer_groups(sim_races, sim_units, f.n_units)
        muni_count = np.bincount(f.unit_muni, minlength=f.n_munis)
        sources = []
        for rt, members in groups:
            aligned = _muni_aligned([sim_units[i] for i in members], f.unit_muni, muni_count)
            if rt == RaceType.PRESIDENT_PROVINCE.value and method is EVAllocationMethod.DISTRICT:
                aligned = False  # district-level presidential votes need fragments
            sources.append(SOURCE_MUNICIPALITY if aligned else SOURCE_FRAGMENT)
        frag_of_unit = _fragments(model, sim_races, sim_units, groups, sources, method)
        F = int(frag_of_unit.max()) + 1
        frag_muni = np.zeros(F, dtype=np.int64)
        frag_muni[frag_of_unit] = f.unit_muni
        tc = model.config.turnout
        fm = _fragment_model(model, expected, frag_of_unit, F, tc.min_probability, tc.max_probability)
        geo = _CellGeometry.build(
            fm, frag_of_unit, frag_muni, f.n_munis, tc.min_probability, tc.max_probability
        )

        # --- layers of anchored race pipelines -----------------------------------------------
        layers: list[LayerPlan] = []
        infos: list[RaceInfo | None] = [None] * len(sim_races)
        for li, ((rt, members), src) in enumerate(zip(groups, sources, strict=True)):
            layer, exp_shares, exp_valid = _build_layer(
                li, rt, src, members, sim_races, plans, model, expected, frag_of_unit, geo
            )
            layers.append(layer)
            for j, i in enumerate(members):
                infos[i] = _race_info(sim_races[i], li, j, exp_shares[j], exp_valid[j])
        race_infos: list[RaceInfo] = [i for i in infos if i is not None]

        # --- presidency and chambers ---------------------------------------------------------
        president = None
        if children:
            president = _presidential_plan(
                sim_races,
                race_infos,
                layers,
                derived_parent,
                ev_by_province,
                const,
                method,
                frag_of_unit,
                sim_units,
            )
            president.expected_pv, national_valid = _national_expectation(president, race_infos)
            if derived_parent is not None:
                race_infos.append(_derived_parent_info(derived_parent, president, national_valid))
                president.parent = len(race_infos) - 1
        house = _chamber_plan(
            "house", RaceType.HOUSE, sim_races, race_infos, layers, {}, const.house_majority
        )
        senate = _chamber_plan(
            "senate",
            RaceType.SENATE,
            sim_races,
            race_infos,
            layers,
            holdover_senate or {},
            const.senate_majority,
        )
        if senate is not None and senate.seats_total > const.senate_seats:
            raise ElectionError(
                f"Senate: {senate.seats_up} seats up + {int(senate.holdover.sum())} holdovers exceed "
                f"the {const.senate_seats} seats of the chamber"
            )
        governors = _chamber_plan("governors", RaceType.GOVERNOR, sim_races, race_infos, layers, {}, None)

        # --- error structure -------------------------------------------------------------------
        shocks = _resolve_shocks(model, config, with_polls=bool(applied_polls))
        events = _event_arrays(model, shocks.events)
        norm = float(np.sqrt((model.ideology**2).sum(axis=1).mean())) or 1.0
        metadata = _metadata(
            model,
            config,
            const,
            ctx,
            sim_races,
            sim_units,
            derived_parent,
            applied_polls,
            president,
            method,
            holdover_senate,
            shocks,
            events.names,
            layers,
            F,
        )
    plan = ForecastPlan(
        party_codes=list(model.party_codes),
        province_codes=list(f.province_codes),
        muni_codes=list(f.muni_codes),
        races=race_infos,
        layers=layers,
        frag_muni=frag_muni,
        frag_prov=f.muni_province[frag_muni].astype(np.int64),
        frag_eligible=fm.eligible.astype(_F32),
        V=fm.V.astype(_F32),
        Tq=fm.Tq.astype(_F32),
        A_nat_T=np.ascontiguousarray(fm.A_nat.transpose(0, 2, 1), dtype=_F32),
        A_loc_T=np.ascontiguousarray(fm.A_loc.transpose(0, 2, 1), dtype=_F32),
        turnout_gain=fm.turnout_gain.astype(_F32),
        cell_sd=(1.0 / np.sqrt(fm.neff)).astype(_F32),
        muni_present=geo.muni_present,
        muni_starts=geo.muni_starts,
        muni_pos=geo.muni_pos,
        needs_munis=any(lp.source == SOURCE_MUNICIPALITY for lp in layers),
        total_eligible=float(fm.eligible.sum()),
        ideology_unit=model.ideology / norm,
        event_prob=events.prob,
        event_national=events.national,
        event_province=events.province,
        event_turnout=events.turnout,
        event_names=events.names,
        muni_chol=_municipal_cholesky(f.muni_xy, shocks),
        shocks=shocks,
        q_min=float(tc.min_probability),
        q_max=float(tc.max_probability),
        president=president,
        house=house,
        senate=senate,
        governors=governors,
        constitution={
            "electoral_votes": const.electoral_votes,
            "presidential_majority": const.presidential_majority,
            "house_seats": const.house_seats,
            "house_majority": const.house_majority,
            "senate_seats": const.senate_seats,
            "senate_majority": const.senate_majority,
            "senators_per_province": const.senators_per_province,
        },
        block_size=int(config.block_size),
        share_bins=int(config.outputs.share_bins),
        municipality_distributions=bool(config.outputs.municipality_distributions),
        metadata=metadata,
    )
    log.info(
        "forecast plan: %d races in %d layers, %d fragments from %d units",
        len(race_infos),
        len(layers),
        F,
        f.n_units,
    )
    return plan


def _fragments(
    model: StructuralModel,
    sim_races: Sequence[RaceSpec],
    sim_units: Sequence[np.ndarray],
    groups: Sequence[tuple[str, list[int]]],
    sources: Sequence[str],
    method: EVAllocationMethod,
) -> np.ndarray:
    """Fragment of every unit: units grouped by municipality and by their race in every layer that
    does not follow municipal borders (and by House district for DISTRICT EV allocation).
    Fragments are numbered in municipality order."""
    f = model.frame
    U = f.n_units
    cols = [f.unit_muni.astype(np.int64)]
    for (_, members), src in zip(groups, sources, strict=True):
        if src == SOURCE_FRAGMENT:
            lab = np.full(U, -1, dtype=np.int64)
            for j, i in enumerate(members):
                lab[sim_units[i]] = j
            cols.append(lab)
    if method is EVAllocationMethod.DISTRICT:
        lab = np.full(U, -1, dtype=np.int64)
        for j, (r, u) in enumerate(zip(sim_races, sim_units, strict=True)):
            if RaceType(r.race_type) == RaceType.HOUSE:
                lab[u] = j
        cols.append(lab)
    _, frag_of_unit = np.unique(np.column_stack(cols), axis=0, return_inverse=True)
    return np.asarray(frag_of_unit).reshape(-1).astype(np.int64)


@dataclass
class _CellGeometry:
    """Fragment → municipality structure and the zero-shock state of fragments and municipalities."""

    muni_present: np.ndarray  # (M_p,) frame municipality indices, fragment order
    muni_starts: np.ndarray  # (M_p,) first fragment of each municipality
    muni_pos: np.ndarray  # (M,) position of a frame municipality in muni_present (−1 = none)
    frag_muni: np.ndarray  # (F,)
    pi_frag: np.ndarray  # (F, 1, P) zero-shock vote shares
    pi_muni: np.ndarray  # (M_p, 1, P)

    @classmethod
    def build(
        cls,
        fm: FragmentModel,
        frag_of_unit: np.ndarray,
        frag_muni: np.ndarray,
        n_munis: int,
        q_min: float,
        q_max: float,
    ) -> _CellGeometry:
        pi0, T0 = kernel.fragment_state(fm.V, fm.Tq, None, None, None, q_min, q_max)
        muni_present = np.unique(frag_muni).astype(np.int64)
        muni_starts = _starts(frag_muni)
        muni_pos = np.full(n_munis, -1, dtype=np.int64)
        muni_pos[muni_present] = np.arange(len(muni_present))
        pim0, _ = kernel.municipality_state(pi0, (fm.eligible * T0[:, 0])[:, None], muni_starts)
        return cls(muni_present, muni_starts, muni_pos, frag_muni, pi0, pim0)


def _build_layer(
    index: int,
    race_type: str,
    source: str,
    members: Sequence[int],
    sim_races: Sequence[RaceSpec],
    plans: Sequence[RacePlan],
    model: StructuralModel,
    expected: Any,
    frag_of_unit: np.ndarray,
    geo: _CellGeometry,
) -> tuple[LayerPlan, list[np.ndarray], list[float]]:
    """Aggregate the race plans of one layer to its cells and anchor them on the model expectation.

    Returns the layer and, per member race, the expected line shares and valid votes.
    """
    f = model.frame
    P = model.n_parties
    E = model.eligible.astype(float)
    T = expected.turnout
    L = max(len(sim_races[i].lines) for i in members)
    parts: dict[str, list[np.ndarray]] = {
        k: [] for k in ("src", "race", "R", "a", "ind", "delta", "G", "bb", "inv", "tgt")
    }
    strategic = False
    exp_shares: list[np.ndarray] = []
    exp_valid: list[float] = []
    for j, i in enumerate(members):
        rp = plans[i]
        u = rp.units
        cell_of_unit = frag_of_unit[u] if source == SOURCE_FRAGMENT else geo.muni_pos[f.unit_muni[u]]
        cells, inv = np.unique(cell_of_unit, return_inverse=True)
        inv = np.asarray(inv).reshape(-1)
        C, Lr = len(cells), len(rp.race.lines)
        ballots = E[u] * T[u]
        valid = ballots * (1.0 - rp.blank_probability(expected.vote_share[u]) - rp.invalid_p)
        tgt = _cell_mean(inv, C, rp.expected_shares, valid)
        cell_valid = np.bincount(inv, weights=valid, minlength=C)
        tot = (tgt * cell_valid[:, None]).sum(axis=0)
        exp_shares.append(tot / tot.sum() if tot.sum() > 0 else rp.jurisdiction_shares)
        exp_valid.append(float(cell_valid.sum()))
        strategic = strategic or not np.allclose(rp.G, np.eye(Lr))
        G = np.zeros((L, L))
        G[:Lr, :Lr] = rp.G
        parts["src"].append(cells)
        parts["race"].append(np.full(C, j, dtype=np.int64))
        parts["R"].append(np.broadcast_to(_pad(rp.R, L, 0.0)[None], (C, P, L)))
        parts["a"].append(np.broadcast_to(rp.a[None], (C, P)))
        parts["ind"].append(np.broadcast_to(_pad(rp.indep_mass, L, 0.0)[None], (C, L)))
        parts["delta"].append(_pad(_cell_mean(inv, C, rp.delta, ballots), L, -np.inf))
        parts["G"].append(np.broadcast_to(G[None], (C, L, L)))
        parts["bb"].append(_cell_mean(inv, C, rp.blank_base, ballots))
        parts["inv"].append(_cell_mean(inv, C, rp.invalid_p, ballots))
        parts["tgt"].append(_pad(tgt, L, 0.0))
    cat = {k: np.concatenate(v) for k, v in parts.items()}
    G_all = cat["G"] if strategic else None
    cell_src, cell_race = cat["src"].astype(np.int64), cat["race"]
    pi0 = (geo.pi_frag if source == SOURCE_FRAGMENT else geo.pi_muni)[cell_src]  # (C, 1, P)
    p0, _ = kernel.line_shares(
        pi0, cat["R"], cat["a"], cat["ind"], cat["delta"], G_all, None, cat["bb"], cat["inv"], None
    )
    ratio = np.where(p0[:, 0] > 0, cat["tgt"] / np.maximum(p0[:, 0], 1e-300), 0.0)
    cell_muni = geo.frag_muni[cell_src] if source == SOURCE_FRAGMENT else geo.muni_present[cell_src]
    shock_index, n_shocks = _line_shock_index([sim_races[i] for i in members], L)
    layer = LayerPlan(
        index=index,
        race_type=race_type,
        source=source,
        races=np.asarray(members, dtype=np.int64),
        cell_src=cell_src,
        cell_race=cell_race,
        cell_muni=cell_muni.astype(np.int64),
        race_starts=_starts(cell_race),
        n_lines=np.array([len(sim_races[i].lines) for i in members], dtype=np.int64),
        R=np.ascontiguousarray(cat["R"], dtype=_F32),
        a=np.ascontiguousarray(cat["a"], dtype=_F32),
        indep=np.ascontiguousarray(cat["ind"], dtype=_F32),
        delta=np.ascontiguousarray(cat["delta"], dtype=_F32),
        G=None if G_all is None else np.ascontiguousarray(G_all, dtype=_F32),
        ratio=np.ascontiguousarray(ratio, dtype=_F32),
        blank_base=cat["bb"].astype(_F32),
        invalid=cat["inv"].astype(_F32),
        cell_shock=shock_index[cell_race],
        n_shocks=n_shocks,
    )
    return layer, exp_shares, exp_valid


@dataclass
class _Events:
    prob: np.ndarray  # (E,)
    national: np.ndarray  # (E, P)
    province: np.ndarray  # (E, Pv, P)
    turnout: np.ndarray  # (E,)
    names: list[str]


def _event_arrays(model: StructuralModel, enabled: bool) -> _Events:
    """Probabilistic scenario events (0 < probability < 1) whose occurrence is drawn per draw."""
    f = model.frame
    P = model.n_parties
    events = [ev for ev in model.events if 0.0 < ev.probability < 1.0] if enabled else []
    nat = np.zeros((len(events), P))
    prov = np.zeros((len(events), f.n_provinces, P))
    for k, ev in enumerate(events):
        for c, v in ev.national.items():
            nat[k, model.party_index[c]] += v
        for pc, shifts in ev.provinces.items():
            for c, v in shifts.items():
                prov[k, f.province_index(pc), model.party_index[c]] += v
    return _Events(
        prob=np.array([ev.probability for ev in events], dtype=float),
        national=nat,
        province=prov,
        turnout=np.array([ev.turnout for ev in events], dtype=float),
        names=[ev.name for ev in events],
    )


def _metadata(
    model: StructuralModel,
    config: ForecastConfig,
    const: ConstitutionConfig,
    ctx: ElectionContext,
    sim_races: Sequence[RaceSpec],
    sim_units: Sequence[np.ndarray],
    derived_parent: RaceSpec | None,
    applied_polls: Mapping[str, float],
    president: PresidentialPlan | None,
    method: EVAllocationMethod,
    holdover: Mapping[str, int] | None,
    shocks: ShockParams,
    event_names: Sequence[str],
    layers: Sequence[LayerPlan],
    n_fragments: int,
) -> dict[str, Any]:
    """Run metadata incl. the model fingerprint, the configuration hash and an input hash over
    everything that determines the forecast (model, config, races, context, polls, EV map …)."""
    signature = {
        "model": model.fingerprint,
        "config": config.hash,
        "races": [_race_signature(r, u) for r, u in zip(sim_races, sim_units, strict=True)],
        "derived_parent": derived_parent.key if derived_parent is not None else None,
        "context": _context_signature(ctx),
        "poll_shift": dict(applied_polls),
        "ev": None
        if president is None
        else dict(zip(president.province_codes, president.ev.tolist(), strict=True)),
        "method": str(method),
        "holdover": dict(holdover or {}),
        "constitution": const.model_dump(mode="json"),
    }
    n_races = len(sim_races) + (1 if derived_parent is not None else 0)
    return {
        "model_fingerprint": model.fingerprint,
        "config_hash": config.hash,
        "input_hash": config_hash(json.dumps(signature, sort_keys=True, default=str)),
        "scenario_slug": model.scenario.scenario.slug,
        "year": ctx.year,
        "election_type": ctx.election_type,
        "president_party": ctx.president_party,
        "national_shifts": {k: round(float(v), 6) for k, v in ctx.national_shifts.items()},
        "poll_shift": {k: round(float(v), 6) for k, v in applied_polls.items()},
        "ev_method": str(method),
        "n_races": n_races,
        "cells": {
            "units": model.frame.n_units,
            "fragments": n_fragments,
            "layers": [
                {"race_type": lp.race_type, "races": lp.n_races, "cells": lp.n_cells, "cell": lp.source}
                for lp in layers
            ],
        },
        "errors": shocks.to_dict(),
        "events": list(event_names),
    }


def _municipal_cholesky(xy: np.ndarray, shocks: ShockParams) -> np.ndarray | None:
    """Cholesky factor of ``municipality_sd² I + spatial_sd² K``: the iid and the spatially
    correlated municipal errors drawn as one Gaussian field (``K`` is the simulation model's GP
    kernel matrix).  ``None`` without a spatial component (the iid part is then drawn directly)."""
    if len(xy) == 0 or shocks.spatial_sd <= 0:
        return None
    L = gp_cholesky(xy, shocks.spatial_length_km, shocks.kernel)
    if shocks.municipality_sd <= 0:
        return np.ascontiguousarray(shocks.spatial_sd * L, dtype=_F32)
    cov = shocks.municipality_sd**2 * np.eye(len(xy)) + shocks.spatial_sd**2 * (L @ L.T)
    return np.ascontiguousarray(np.linalg.cholesky(cov), dtype=_F32)


def _line_shock_index(races: Sequence[RaceSpec], L: int) -> tuple[np.ndarray, int]:
    """Index (R, L) of each race line's performance shock, mirroring
    ``app.simulation.voting.race_line_shocks``: one shock per race × line, except that every
    presidential contest shares its ticket's national shock.  Padding points at the extra slot."""
    keys: dict[tuple[str, str], int] = {}
    idx = np.full((len(races), L), -1, dtype=np.int64)
    for r, race in enumerate(races):
        presidential = RaceType(race.race_type) in (RaceType.PRESIDENT, RaceType.PRESIDENT_PROVINCE)
        scope = PRESIDENTIAL_LINE_SCOPE if presidential else race.key
        for j, ln in enumerate(race.lines):
            idx[r, j] = keys.setdefault((scope, ln.key), len(keys))
    idx[idx < 0] = len(keys)
    return idx, len(keys)


def _race_info(
    race: RaceSpec, layer: int, local: int, expected: np.ndarray, expected_valid: float
) -> RaceInfo:
    return RaceInfo(
        key=race.key,
        race_type=str(RaceType(race.race_type)),
        line_keys=tuple(ln.key for ln in race.lines),
        line_parties=tuple(ln.party_code for ln in race.lines),
        line_labels=tuple(ln.label or ln.key for ln in race.lines),
        layer=layer,
        local=local,
        province_code=race.province_code,
        district_code=race.district_code,
        municipality_code=race.municipality_code,
        electoral_votes=race.electoral_votes,
        expected_shares=tuple(float(x) for x in expected),
        expected_valid=float(expected_valid),
    )


def _presidential_plan(
    sim_races: Sequence[RaceSpec],
    infos: Sequence[RaceInfo],
    layers: Sequence[LayerPlan],
    parent: RaceSpec | None,
    ev_by_province: Mapping[str, int] | None,
    const: ConstitutionConfig,
    method: EVAllocationMethod,
    frag_of_unit: np.ndarray,
    sim_units: Sequence[np.ndarray],
) -> PresidentialPlan:
    child_idx = [i for i, r in enumerate(sim_races) if RaceType(r.race_type) == RaceType.PRESIDENT_PROVINCE]
    by_prov: dict[str, int] = {}
    for i in child_idx:
        pv = sim_races[i].province_code
        if pv is None:
            raise ElectionError(f"{sim_races[i].key}: a province EV contest needs province_code")
        if pv in by_prov:
            raise ElectionError(f"two presidential contests for province {pv}")
        by_prov[pv] = i
    if ev_by_province is None:
        ev_map = {}
        for pv, i in by_prov.items():
            ev = sim_races[i].electoral_votes
            if ev is None:
                raise ElectionError(f"{sim_races[i].key}: electoral votes unknown (pass ev_by_province)")
            ev_map[pv] = int(ev)
    else:
        ev_map = {str(k): int(v) for k, v in ev_by_province.items()}
    missing = sorted(set(ev_map) - set(by_prov))
    extra = sorted(set(by_prov) - set(ev_map))
    if missing or extra:
        raise ElectionError(
            f"presidential contests do not match the EV map (missing {missing}, extra {extra})"
        )
    if any(v < 0 for v in ev_map.values()):
        raise ElectionError("negative electoral votes")
    provinces = list(ev_map)
    total = int(sum(ev_map.values()))
    if total <= 0:
        raise ElectionError("electoral-vote map is empty")
    majority = const.presidential_majority if total == const.electoral_votes else majority_of(total)
    layer_ids = {int(infos[by_prov[pv]].layer) for pv in provinces}
    if len(layer_ids) != 1:
        raise ElectionError("presidential province contests overlap")
    li = layer_ids.pop()
    lp = layers[li]
    if parent is not None:
        tickets = [ln.key for ln in parent.lines]
        t_party = [ln.party_code for ln in parent.lines]
        t_label = [ln.label or ln.key for ln in parent.lines]
    else:
        tickets, t_party, t_label = [], [], []
    for pv in provinces:
        for ln in sim_races[by_prov[pv]].lines:
            if ln.key not in tickets:
                if parent is not None:
                    raise ElectionError(f"line {ln.key} of PRES-{pv} is not on the national ballot")
                tickets.append(ln.key)
                t_party.append(ln.party_code)
                t_label.append(ln.label or ln.key)
    tix = {k: t for t, k in enumerate(tickets)}
    line_ticket = np.full((len(provinces), lp.L), -1, dtype=np.int64)
    for p, pv in enumerate(provinces):
        for j, ln in enumerate(sim_races[by_prov[pv]].lines):
            line_ticket[p, j] = tix[ln.key]
    races = np.array([by_prov[pv] for pv in provinces], dtype=np.int64)
    local = np.array([infos[i].local for i in races], dtype=np.int64)
    # municipality groups of the presidential cells
    if lp.source == SOURCE_MUNICIPALITY:
        muni_perm = None
        order_muni = lp.cell_muni
    else:
        muni_perm = np.argsort(lp.cell_muni, kind="stable")
        order_muni = lp.cell_muni[muni_perm]
        if np.array_equal(muni_perm, np.arange(len(muni_perm))):
            muni_perm = None
    muni_starts = _starts(order_muni)
    plan = PresidentialPlan(
        layer=li,
        races=races,
        local=local,
        province_codes=provinces,
        ev=np.array([ev_map[pv] for pv in provinces], dtype=np.int64),
        tickets=tickets,
        ticket_parties=t_party,
        ticket_labels=t_label,
        line_ticket=line_ticket,
        method=str(method),
        majority=int(majority),
        total_ev=total,
        statewide_ev=int(const.senators_per_province),
        parent=None,
        muni_perm=muni_perm,
        muni_starts=muni_starts,
        muni_index=order_muni[muni_starts] if len(order_muni) else np.zeros(0, dtype=np.int64),
    )
    if method is EVAllocationMethod.DISTRICT:
        house = [i for i, r in enumerate(sim_races) if RaceType(r.race_type) == RaceType.HOUSE]
        if not house:
            raise ElectionError("DISTRICT electoral-vote allocation needs the House races")
        frag_district = np.full(int(frag_of_unit.max()) + 1, -1, dtype=np.int64)
        codes: list[str] = []
        dprov: list[int] = []
        for d, i in enumerate(house):
            frag_district[frag_of_unit[sim_units[i]]] = d
            r = sim_races[i]
            codes.append(r.district_code or r.key.split("-", 1)[-1])
            pv = r.province_code or codes[-1].split("-")[0]
            if pv not in ev_map:
                raise ElectionError(f"{r.key}: province {pv} is not in the EV map")
            dprov.append(provinces.index(pv))
        cell_d = frag_district[lp.cell_src]
        if (cell_d < 0).any():
            raise ElectionError("DISTRICT allocation: some presidential cells lie in no House district")
        perm = np.argsort(cell_d, kind="stable")
        starts = _starts(cell_d[perm])
        present = cell_d[perm][starts]
        if len(present) != len(house):
            raise ElectionError("DISTRICT allocation: some House districts have no presidential votes")
        dprov_arr = np.array(dprov, dtype=np.int64)
        for p, pv in enumerate(provinces):
            nd = int((dprov_arr == p).sum())
            if nd + plan.statewide_ev != ev_map[pv]:
                raise ElectionError(
                    f"{pv}: {nd} districts + {plan.statewide_ev} statewide EV != {ev_map[pv]} electoral votes"
                )
        plan.district_perm = perm
        plan.district_starts = starts
        plan.district_province = dprov_arr
        plan.district_codes = codes
    return plan


def _national_expectation(pres: PresidentialPlan, infos: Sequence[RaceInfo]) -> tuple[np.ndarray, float]:
    """Expected national popular-vote shares per ticket (province contests weighted by their
    expected valid votes) and the expected national valid votes."""
    tot = np.zeros(len(pres.tickets))
    valid = 0.0
    for p, i in enumerate(pres.races):
        info = infos[int(i)]
        for j, share in enumerate(info.expected_shares):
            tot[pres.line_ticket[p, j]] += share * info.expected_valid
        valid += info.expected_valid
    return (tot / tot.sum() if tot.sum() > 0 else tot), valid


def _derived_parent_info(parent: RaceSpec, pres: PresidentialPlan, valid: float) -> RaceInfo:
    """The national ``PRES`` race: the sum of the province contests (national popular vote)."""
    return RaceInfo(
        key=parent.key,
        race_type=str(RaceType(parent.race_type)),
        line_keys=tuple(pres.tickets),
        line_parties=tuple(pres.ticket_parties),
        line_labels=tuple(pres.ticket_labels),
        layer=-1,
        local=-1,
        derived=True,
        expected_shares=tuple(float(x) for x in pres.expected_pv),
        expected_valid=valid,
    )


def _chamber_plan(
    name: str,
    race_type: RaceType,
    sim_races: Sequence[RaceSpec],
    infos: Sequence[RaceInfo],
    layers: Sequence[LayerPlan],
    holdover: Mapping[str, int],
    majority: int | None,
) -> ChamberPlan | None:
    idx = [i for i, r in enumerate(sim_races) if RaceType(r.race_type) == race_type]
    if not idx and not holdover:
        return None
    parties: list[str] = []
    independent = False
    for i in idx:
        for ln in sim_races[i].lines:
            if ln.party_code is None:
                independent = True
            elif ln.party_code not in parties:
                parties.append(ln.party_code)
    for k, v in holdover.items():
        if int(v) < 0:
            raise ElectionError(f"negative holdover seats for {k}")
        if k == INDEPENDENT:
            independent = independent or int(v) > 0
        elif k not in parties:
            parties.append(str(k))
    keys = parties + ([INDEPENDENT] if independent else [])
    kix = {k: j for j, k in enumerate(keys)}
    hold = np.array([int(holdover.get(k, 0)) for k in keys], dtype=np.int64)
    by_layer: dict[int, list[int]] = {}
    for i in idx:
        by_layer.setdefault(infos[i].layer, []).append(i)
    parts = []
    for li, members in sorted(by_layer.items()):
        lp = layers[li]
        table = np.full((len(members), lp.L), -1, dtype=np.int64)
        for r, i in enumerate(members):
            for j, ln in enumerate(sim_races[i].lines):
                table[r, j] = kix[INDEPENDENT if ln.party_code is None else ln.party_code]
        parts.append((li, np.array([infos[i].local for i in members], dtype=np.int64), table))
    return ChamberPlan(
        name=name,
        races=np.asarray(idx, dtype=np.int64),
        keys=keys,
        holdover=hold,
        majority=majority,
        parts=parts,
    )
