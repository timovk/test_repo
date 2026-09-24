"""Election simulation: one draw of election-specific shocks → unit-level votes for every race.

Pipeline of :func:`simulate_election` (docs/SIMULATION.md §5–§9):

1. **Deterministic expectation.**  Party utilities ``U0 + environment + context shifts`` give the
   pre-election expectation (no shocks) of preference shares, turnout and vote shares.
2. **Shocks** (one draw, every component on its own named RNG stream):
   national per party (Student-t, partly a common ideological swing, scaled by unit elasticity),
   province × party, municipality × party, a spatially correlated Gaussian-process field over
   municipality centroids, unit noise, and turnout shocks (national, municipal, unit and a
   differential mobilisation of each party's supporters).  Probabilistic events occur or not.
3. **Turnout is election-wide:** ``ballots_u ~ Binomial(eligible_u, T_u)`` shared by all races.
4. **Races.**  Party vote shares are routed to ballot lines (absent parties transfer by affinity;
   a share abstains = undervote; withdrawn lines keep a small residual; independents get a
   quality-based mass), candidate effects are added (quality, incumbency, home municipality /
   province incl. running mate, midterm penalty, campaign effects, a race-specific line shock),
   FPTP strategic voting moves supporters of non-viable lines to the viable ones, and per race
   ``invalid ~ Binomial``, ``blank ~ Binomial``, ``votes ~ Multinomial(valid, shares)``.

``RaceVotes.expected_shares`` / ``expected_turnout`` hold the deterministic expectation of step 1
(the model's pre-election view used by the race-calling engine); ``ElectionDraw.environment``
records the realised shocks (JSON-serialisable).  All numbers produced here are SIMULATED.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.constitution import ElectoralSystem, RaceType
from app.core.errors import ElectionError
from app.core.logging import Timer, get_logger
from app.core.rng import make_rng
from app.elections.types import BallotLine, ElectionDraw, RaceSpec, RaceVotes, UnitTurnout
from app.scenarios.schema import CandidateSpec, ScenarioDocument
from app.simulation.spatial import gp_cholesky
from app.simulation.structural import (
    IDEOLOGY_DIMS,
    PartyState,
    StructuralModel,
    affinity_distance,
    routing_matrix,
    softmax_rows,
)

log = get_logger(__name__)

_TINY = 1e-300
_PLURALITY_SYSTEMS = {ElectoralSystem.FPTP, ElectoralSystem.WINNER_TAKE_ALL}
_GEO_LEVELS = ("national", "province", "municipality")


# --------------------------------------------------------------------------- context
@dataclass(frozen=True)
class IncumbentInfo:
    """Who holds a seat before the election (``running=False`` → open seat)."""

    party: str | None = None
    candidate_key: str | None = None
    running: bool = True


GeoKey = tuple[str, str]


@dataclass
class ElectionContext:
    """Everything election-specific that is not in the structural model.

    * ``campaign_effects`` — keys are either a race key (``"HOUSE-NB-07"``: effects apply to that
      race only, values keyed by party code or line key) or a geography ``(level, code)`` /
      ``"level:code"`` with level ``national`` | ``province`` | ``municipality`` (party-level
      effects for every race in that area).  Values are logit shifts.
    * ``turnout_effects`` — ``(level, code)`` / ``"level:code"`` → logit shift of turnout.
    * ``national_shifts`` — extra national party shifts (e.g. poll-informed), scaled by elasticity.
    """

    year: int
    election_type: str = "general"
    president_party: str | None = None
    incumbents: Mapping[str, IncumbentInfo] = field(default_factory=dict)
    candidates: Mapping[str, CandidateSpec] = field(default_factory=dict)
    campaign_effects: Mapping[str | GeoKey, Mapping[str, float]] = field(default_factory=dict)
    turnout_effects: Mapping[str | GeoKey, float] = field(default_factory=dict)
    national_shifts: Mapping[str, float] = field(default_factory=dict)
    #: Presidential home-province bonuses (default: the scenario's ``president`` section).
    home_province_bonus: float | None = None
    vp_home_province_bonus: float | None = None
    #: Include the scenario's deterministic environment (national/province/event shifts).
    include_environment: bool = True

    @classmethod
    def from_scenario(
        cls,
        doc: ScenarioDocument,
        *,
        president_party: str | None = None,
        incumbents: Mapping[str, IncumbentInfo] | None = None,
        extra_candidates: Sequence[CandidateSpec] = (),
        **kwargs: Any,
    ) -> ElectionContext:
        """Context for the scenario's election (candidate lookup from ``doc.candidates``).

        ``president_party`` defaults to the party of the candidate whose ``incumbent_office`` is
        ``PRES`` (the sitting president assumed by the scenario); services chaining elections
        through the history pass the actual office holder's party instead.
        """
        cands = {c.key: c for c in doc.candidates}
        cands.update({c.key: c for c in extra_candidates})
        if president_party is None:
            president_party = next((c.party for c in doc.candidates if c.incumbent_office == "PRES"), None)
        return cls(
            year=doc.scenario.year,
            election_type=doc.scenario.election_type,
            president_party=president_party,
            incumbents=dict(incumbents or {}),
            candidates=cands,
            **{
                "home_province_bonus": doc.president.home_province_bonus,
                "vp_home_province_bonus": doc.president.vp_home_province_bonus,
                **kwargs,
            },
        )


def _geo_key(key: str | GeoKey) -> GeoKey | None:
    """Normalise a geography key; ``None`` for race keys."""
    if isinstance(key, tuple):
        level, code = key
    elif key == "national":
        level, code = "national", "NL"
    elif ":" in key:
        level, code = key.split(":", 1)
    else:
        return None
    if level not in _GEO_LEVELS:
        raise ElectionError(f"unknown campaign/turnout level {level!r} (expected {_GEO_LEVELS})")
    return level, code


def _geo_mask(model: StructuralModel, level: str, code: str) -> np.ndarray | None:
    f = model.frame
    if level == "national":
        return None
    if level == "province":
        if code not in f.province_codes:
            raise ElectionError(f"unknown province {code!r}")
        return f.unit_province == f.province_index(code)
    m = f._muni_lookup.get(code)
    if m is None:
        raise ElectionError(f"unknown municipality {code!r}")
    return f.unit_muni == m


def context_shifts(model: StructuralModel, ctx: ElectionContext) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic party utility shift (U, P) and turnout shift (U,) implied by the context
    (national shifts, geography-level campaign effects, turnout effects)."""
    f = model.frame
    U, P = f.n_units, model.n_parties
    shift = np.zeros((U, P))
    tshift = np.zeros(U)
    for code, v in ctx.national_shifts.items():
        shift[:, model.party_idx([code])[0]] += model.elasticity * v
    for key, effects in ctx.campaign_effects.items():
        gk = _geo_key(key)
        if gk is None:
            continue
        mask = _geo_mask(model, *gk)
        for code, v in effects.items():
            j = model.party_idx([code])[0]
            if mask is None:
                shift[:, j] += v
            else:
                shift[mask, j] += v
    for key, v in ctx.turnout_effects.items():
        gk = _geo_key(key)
        if gk is None:
            raise ElectionError(f"turnout effect key {key!r} must be a geography")
        mask = _geo_mask(model, *gk)
        if mask is None:
            tshift += v
        else:
            tshift[mask] += v
    return shift, tshift


# --------------------------------------------------------------------------- race plans
_INCUMBENCY_FIELD = {
    RaceType.PRESIDENT: "president",
    RaceType.PRESIDENT_PROVINCE: "president",
    RaceType.HOUSE: "house",
    RaceType.SENATE: "senate",
    RaceType.GOVERNOR: "governor",
    RaceType.MAYOR: "mayor",
}


@dataclass
class RacePlan:
    """Deterministic, draw-independent parts of one race (reusable across Monte Carlo draws).

    ``shares(vote_share, line_shock)`` maps party vote shares of the race's units (n, P) to line
    shares (n, L); ``abstain(vote_share)`` gives the undervote share (n,).
    """

    race: RaceSpec
    units: np.ndarray  # (n,)
    line_party: np.ndarray  # (L,) party index, −1 = independent
    R: np.ndarray  # (P, L) routing matrix
    a: np.ndarray  # (P,) undervote share per party supporter
    indep_mass: np.ndarray  # (L,)
    delta: np.ndarray  # (n, L) deterministic candidate / campaign effects
    G: np.ndarray  # (L, L) strategic-voting transfer matrix
    defect: np.ndarray  # (L,) strategic defection fractions
    blank_base: np.ndarray  # (n,)
    invalid_p: np.ndarray  # (n,)
    expected_shares: np.ndarray  # (n, L)
    expected_turnout: np.ndarray  # (n,)
    jurisdiction_shares: np.ndarray  # (L,) expected race-level shares (after strategic voting)

    def sincere_shares(self, vote_share: np.ndarray, line_shock: np.ndarray | None = None) -> np.ndarray:
        m = vote_share @ self.R + self.indep_mass[None, :]
        W = np.log(np.maximum(m, _TINY)) + self.delta
        if line_shock is not None:
            W = W + line_shock[None, :]
        return softmax_rows(W)

    def shares(self, vote_share: np.ndarray, line_shock: np.ndarray | None = None) -> np.ndarray:
        s = self.sincere_shares(vote_share, line_shock) @ self.G
        s = np.maximum(s, 0.0)
        return s / s.sum(axis=1, keepdims=True)

    def abstain(self, vote_share: np.ndarray) -> np.ndarray:
        return vote_share @ self.a

    def blank_probability(self, vote_share: np.ndarray) -> np.ndarray:
        ab = self.abstain(vote_share)
        return np.clip(self.blank_base + (1.0 - self.blank_base - self.invalid_p) * ab, 0.0, 0.95)


def _line_ideology(model: StructuralModel, line: BallotLine, pidx: int, ctx: ElectionContext) -> np.ndarray:
    cand = ctx.candidates.get(line.candidate_key or "") if line.candidate_key else None
    if cand is not None and cand.ideology is not None:
        return np.array([getattr(cand.ideology, d) for d in IDEOLOGY_DIMS], dtype=float)
    if pidx >= 0:
        return model.ideology[pidx].copy()
    return np.zeros(len(IDEOLOGY_DIMS))


def _race_effects(
    model: StructuralModel, race: RaceSpec, ctx: ElectionContext, units: np.ndarray, line_party: np.ndarray
) -> np.ndarray:
    """Deterministic candidate / campaign effects Δ (n, L)."""
    f = model.frame
    cc = model.config.candidates
    env = model.scenario.environment
    rt = RaceType(race.race_type)
    n, L = len(units), len(race.lines)
    delta = np.zeros((n, L))
    q_util = cc.quality_utility.get(rt.value, 0.0)
    inc_field = _INCUMBENCY_FIELD.get(rt)
    inc_bonus = getattr(env.incumbency, inc_field) if inc_field else 0.0
    home_m_bonus = cc.home_municipality_bonus.get(rt.value, 0.0)
    presidential = rt in (RaceType.PRESIDENT, RaceType.PRESIDENT_PROVINCE)
    if presidential:
        pres = model.scenario.president
        home_p_bonus = (
            ctx.home_province_bonus if ctx.home_province_bonus is not None else pres.home_province_bonus
        )
        vp_bonus = (
            ctx.vp_home_province_bonus
            if ctx.vp_home_province_bonus is not None
            else pres.vp_home_province_bonus
        )
    else:
        home_p_bonus = cc.home_province_bonus.get(rt.value, 0.0)
        vp_bonus = 0.0
    unit_muni = f.unit_muni[units]
    unit_prov = f.unit_province[units]
    incumbent_running = any(ln.incumbent and not ln.withdrawn for ln in race.lines)
    inc_info = ctx.incumbents.get(race.key)
    incumbent_party = race.incumbent_party or (inc_info.party if inc_info else None)
    midterm = (
        ctx.election_type == "midterm"
        and ctx.president_party is not None
        and rt in (RaceType.HOUSE, RaceType.SENATE)
    )
    race_campaign = ctx.campaign_effects.get(race.key, {})
    e = model.elasticity[units]
    for j, line in enumerate(race.lines):
        col = delta[:, j]
        independent = line_party[j] < 0
        col += (cc.independent_quality_utility if independent else q_util) * line.quality
        if line.incumbent or (
            inc_info is not None
            and inc_info.running
            and inc_info.candidate_key is not None
            and inc_info.candidate_key == line.candidate_key
        ):
            col += inc_bonus
        elif (
            not incumbent_running
            and incumbent_party is not None
            and line.party_code == incumbent_party
            and not independent
        ):
            col += cc.open_seat_party_bonus
        if midterm and line.party_code == ctx.president_party:
            col -= env.incumbency.midterm_penalty * e
        home_m = line.home_municipality
        home_p = line.home_province
        cand = ctx.candidates.get(line.candidate_key) if line.candidate_key else None
        if cand is not None:
            home_m = home_m or cand.home_municipality
            home_p = home_p or cand.home_province
        if home_m is not None and home_m in f._muni_lookup:
            mi = f._muni_lookup[home_m]
            col += home_m_bonus * (unit_muni == mi)
            if home_p is None:
                home_p = f.province_codes[int(f.muni_province[mi])]
        if home_p is not None and home_p in f.province_codes and home_p_bonus:
            col += home_p_bonus * (unit_prov == f.province_index(home_p))
        if line.running_mate_key:
            mate = ctx.candidates.get(line.running_mate_key)
            mate_q = mate.quality if mate is not None else 0.0
            col += q_util * cc.running_mate_quality_share * mate_q
            mate_p = line.running_mate_home_province or (mate.home_province if mate else None)
            mate_m = mate.home_municipality if mate else None
            if mate_m is not None and mate_m in f._muni_lookup:
                mi = f._muni_lookup[mate_m]
                col += cc.running_mate_home_municipality_bonus * (unit_muni == mi)
                if mate_p is None:
                    mate_p = f.province_codes[int(f.muni_province[mi])]
            if mate_p is not None and mate_p in f.province_codes and vp_bonus:
                col += vp_bonus * (unit_prov == f.province_index(mate_p))
        for k in (line.key, line.party_code):
            if k is not None and k in race_campaign:
                col += race_campaign[k]
    return delta


def _strategic_matrix(
    model: StructuralModel,
    race: RaceSpec,
    jshares: np.ndarray,
    line_party: np.ndarray,
    line_ideo: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Strategic-voting transfer matrix G (L, L) and defection fractions (L,).

    A line ``k`` whose expected share trails the ``viable_lines``-th line by more than
    ``viability_gap_start`` loses ``f_k = sv · ramp_k`` of its supporters.  They move to the lines
    ranked *ahead* of ``k``, weighted by affinity and by each target's perceived viability
    ``1 − ramp_j`` (1 for the leading lines, falling to 0 over the same gap ramp).  Weighting by
    viability instead of taking a hard top-N set keeps ``G`` continuous in the expected shares:
    when two lines are near-tied for the last viable place both receive defectors, so a small
    change in expectations never swaps the destination of every defector at once.
    """
    L = len(race.lines)
    G = np.eye(L)
    fdef = np.zeros(L)
    sc = model.config.strategic
    sv = float(model.scenario.environment.strategic_voting) * sc.race_type_weights.get(
        RaceType(race.race_type).value, 0.0
    )
    active = np.array([not ln.withdrawn for ln in race.lines])
    n_viable = sc.viable_lines
    if (
        sv <= 0
        or race.seats != 1
        or ElectoralSystem(race.electoral_system) not in _PLURALITY_SYSTEMS
        or active.sum() <= n_viable
    ):
        return G, fdef
    act = np.flatnonzero(active)
    order = act[np.argsort(-jshares[act], kind="stable")]
    ref = jshares[order[n_viable - 1]]
    gap = ref - jshares
    ramp = np.clip(
        (gap - sc.viability_gap_start) / (sc.viability_gap_full - sc.viability_gap_start), 0.0, 1.0
    )
    viability = 1.0 - ramp
    extra = model.config.affinity.independent_extra_distance
    for pos in range(n_viable, len(order)):
        k = order[pos]
        fk = sv * ramp[k]
        if fk <= 0:
            continue
        ahead = order[:pos]
        d = affinity_distance(line_ideo[[k]], line_ideo[ahead], model.dim_weights)[0]
        d = d + extra * ((line_party[ahead] < 0) | (line_party[k] < 0))
        w = viability[ahead] * np.exp(-(d - d.min()) / sc.temperature)
        w /= w.sum()
        G[k, k] = 1.0 - fk
        G[k, ahead] += fk * w
        fdef[k] = fk
    return G, fdef


def prepare_race(
    model: StructuralModel,
    race: RaceSpec,
    ctx: ElectionContext,
    expected_state: PartyState,
    *,
    state_units: np.ndarray | None = None,
) -> RacePlan:
    """Pre-compute the deterministic parts of ``race`` given the expected party state.

    ``expected_state`` covers either all units (``state_units=None``) or exactly ``state_units``.
    """
    units = np.asarray(race.unit_index, dtype=np.int64)
    if len(race.lines) == 0:
        raise ElectionError(f"race {race.key} has no ballot lines")
    keys = [ln.key for ln in race.lines]
    if len(set(keys)) != len(keys):
        raise ElectionError(f"race {race.key} has duplicate line keys")
    if state_units is None:
        rows = units
    else:
        lookup = np.full(model.frame.n_units, -1, dtype=np.int64)
        lookup[np.asarray(state_units, dtype=np.int64)] = np.arange(len(state_units))
        rows = lookup[units]
        if (rows < 0).any():
            raise ElectionError(f"race {race.key}: expected state does not cover all race units")
    pi0 = expected_state.vote_share[rows]
    T0 = expected_state.turnout[rows]
    for ln in race.lines:
        if ln.party_code is not None and ln.party_code not in model.party_index:
            raise ElectionError(f"race {race.key}: unknown party {ln.party_code}")
    line_party = np.array(
        [model.party_index[ln.party_code] if ln.party_code is not None else -1 for ln in race.lines],
        dtype=np.int64,
    )
    withdrawn = np.array([ln.withdrawn for ln in race.lines], dtype=bool)
    line_ideo = np.vstack(
        [_line_ideology(model, ln, int(line_party[j]), ctx) for j, ln in enumerate(race.lines)]
    )
    ac = model.config.affinity
    cc = model.config.candidates
    R, a = routing_matrix(
        model.ideology,
        line_party,
        line_ideo,
        withdrawn,
        dim_weights=model.dim_weights,
        temperature=ac.temperature,
        abstain_share=ac.absent_abstain_share,
        withdrawn_residual=cc.withdrawn_residual_share,
        independent_extra_distance=ac.independent_extra_distance,
    )
    ind_base = cc.independent_base_share_by_race.get(
        RaceType(race.race_type).value, cc.independent_base_share
    )
    indep = np.where(line_party < 0, ind_base, 0.0)
    indep[withdrawn & (line_party < 0)] *= cc.withdrawn_residual_share
    delta = _race_effects(model, race, ctx, units, line_party)
    env = model.scenario.environment
    invalid_p = np.clip(env.invalid_rate * model.invalid_multiplier[units], 0.0, 0.2)
    blank_base = np.clip(env.blank_rate * model.blank_multiplier[units], 0.0, 0.2)
    plan = RacePlan(
        race=race,
        units=units,
        line_party=line_party,
        R=R,
        a=a,
        indep_mass=indep,
        delta=delta,
        G=np.eye(len(race.lines)),
        defect=np.zeros(len(race.lines)),
        blank_base=blank_base,
        invalid_p=invalid_p,
        expected_shares=np.zeros((len(units), len(race.lines))),
        expected_turnout=T0,
        jurisdiction_shares=np.zeros(len(race.lines)),
    )
    sincere = plan.sincere_shares(pi0)
    L = len(race.lines)
    if len(units) == 0:  # degenerate jurisdiction: nothing to expect or to vote
        plan.jurisdiction_shares = np.full(L, 1.0 / L)
        return plan
    valid_w = model.eligible[units] * T0 * (1.0 - plan.blank_probability(pi0) - invalid_p)
    if valid_w.sum() <= 0:
        valid_w = np.ones(len(units))
    js = (sincere * valid_w[:, None]).sum(axis=0) / valid_w.sum()
    plan.G, plan.defect = _strategic_matrix(model, race, js, line_party, line_ideo)
    plan.expected_shares = plan.shares(pi0)
    plan.jurisdiction_shares = (plan.expected_shares * valid_w[:, None]).sum(axis=0) / valid_w.sum()
    return plan


def _expected_state(
    model: StructuralModel, ctx: ElectionContext, units: np.ndarray | None = None
) -> PartyState:
    shift, tshift = context_shifts(model, ctx)
    if units is not None:
        shift, tshift = shift[units], tshift[units]
    return model.party_state(
        units, environment=ctx.include_environment, utility_shift=shift, turnout_shift=tshift
    )


def expected_party_state(
    model: StructuralModel,
    context: ElectionContext | None = None,
    units: np.ndarray | Sequence[int] | None = None,
) -> PartyState:
    """Deterministic (no-shock) party state of ``units`` (default: all) under ``context``:
    structural model + scenario environment + context shifts.  This is the expectation every
    :class:`RacePlan` is prepared from."""
    ctx = context if context is not None else ElectionContext.from_scenario(model.scenario)
    idx = None if units is None else np.asarray(units, dtype=np.int64)
    return _expected_state(model, ctx, idx)


def expected_race_shares(
    model: StructuralModel, race: RaceSpec, context: ElectionContext | None = None
) -> np.ndarray:
    """Deterministic (no-shock) expected line shares (n, L) of ``race`` — the model's pre-election
    expectation used by the race-calling and forecasting engines."""
    ctx = context if context is not None else ElectionContext.from_scenario(model.scenario)
    units = np.asarray(race.unit_index, dtype=np.int64)
    st = _expected_state(model, ctx, units)
    return prepare_race(model, race, ctx, st, state_units=units).expected_shares


def race_expectation(
    model: StructuralModel, race: RaceSpec, context: ElectionContext | None = None
) -> RacePlan:
    """Full deterministic plan of a race (expected unit shares, turnout, jurisdiction shares,
    strategic defections) — see :class:`RacePlan`."""
    ctx = context if context is not None else ElectionContext.from_scenario(model.scenario)
    units = np.asarray(race.unit_index, dtype=np.int64)
    st = _expected_state(model, ctx, units)
    return prepare_race(model, race, ctx, st, state_units=units)


# --------------------------------------------------------------------------- shocks
@dataclass
class ShockDraw:
    """One realisation of the election-specific random components."""

    utility: np.ndarray  # (U, P)
    turnout: np.ndarray  # (U,)
    party_turnout: np.ndarray  # (P,)
    record: dict[str, Any]


def _r(x: float) -> float:
    return round(float(x), 6)


def draw_shocks(model: StructuralModel, seed: int, ctx: ElectionContext | None = None) -> ShockDraw:
    """Draw every election-specific random component (each on its own keyed RNG stream)."""
    f = model.frame
    sc = model.scenario.environment.shocks
    mc = model.config
    codes = model.party_codes
    P, U, M, Pv = model.n_parties, f.n_units, f.n_munis, f.n_provinces
    # --- national: Student-t per party + common ideological swing --------------------------
    df = sc.tail_df
    t_scale = np.sqrt((df - 2.0) / df)
    idio = np.array([make_rng(seed, "shock", "national", c).standard_t(df) * t_scale for c in codes])
    swing = make_rng(seed, "shock", "national", "ideological-swing").standard_normal(len(IDEOLOGY_DIMS))
    norm = float(np.sqrt((model.ideology**2).sum(axis=1).mean())) or 1.0
    common = model.ideology @ swing / norm
    rho = mc.shocks.ideological_swing_share
    national = sc.national_sd * (np.sqrt(1.0 - rho) * idio + np.sqrt(rho) * common)
    # --- events (probabilistic ones occur or not) -----------------------------------------------
    ev_nat = np.zeros(P)
    ev_prov = np.zeros((Pv, P))
    ev_turnout = 0.0
    ev_record = []
    for ev in model.events:
        occurred = (
            True
            if ev.probability >= 1.0
            else bool(make_rng(seed, "event", ev.name).random() < ev.probability)
        )
        ev_record.append({"name": ev.name, "probability": ev.probability, "occurred": occurred})
        # deviation from the expectation already contained in the deterministic environment
        factor = (1.0 if occurred else 0.0) - ev.probability
        if factor == 0.0:
            continue
        for c, v in ev.national.items():
            ev_nat[model.party_index[c]] += factor * v
        for pc, shifts in ev.provinces.items():
            for c, v in shifts.items():
                ev_prov[f.province_index(pc), model.party_index[c]] += factor * v
        ev_turnout += factor * ev.turnout
    # --- geographic components ------------------------------------------------------------------
    prov = (
        np.array(
            [
                [make_rng(seed, "shock", "province", pc, c).standard_normal() for c in codes]
                for pc in f.province_codes
            ]
        ).reshape(Pv, P)
        * sc.province_sd
    )
    muni_iid = (
        np.column_stack([make_rng(seed, "shock", "municipality", c).standard_normal(M) for c in codes])
        * sc.municipality_sd
    )
    if sc.spatial_sd > 0 and M > 0:
        chol = gp_cholesky(f.muni_xy, sc.spatial_length_km, mc.lean.kernel)
        z = np.column_stack([make_rng(seed, "shock", "spatial", c).standard_normal(M) for c in codes])
        spatial = (chol @ z) * sc.spatial_sd
    else:
        spatial = np.zeros((M, P))
    unit = (
        np.column_stack([make_rng(seed, "shock", "unit", c).standard_normal(U) for c in codes]) * sc.unit_sd
    )
    e = model.elasticity[:, None]
    utility = (
        e * (national + ev_nat)[None, :]
        + (prov + ev_prov)[f.unit_province]
        + (muni_iid + spatial)[f.unit_muni]
        + unit
    )
    # --- turnout --------------------------------------------------------------------------------
    tc = mc.turnout
    t_nat = float(make_rng(seed, "turnout", "national").standard_normal() * sc.turnout_national_sd)
    t_muni = make_rng(seed, "turnout", "municipality").standard_normal(M) * sc.turnout_local_sd
    t_unit = make_rng(seed, "turnout", "unit").standard_normal(U) * sc.turnout_local_sd * tc.unit_shock_share
    turnout = t_nat + ev_turnout + t_muni[f.unit_muni] + t_unit
    party_t = (
        np.array([make_rng(seed, "turnout", "party", c).standard_normal() for c in codes])
        * tc.party_turnout_shock_sd
    )
    record: dict[str, Any] = {
        "parties": list(codes),
        "national": {c: _r(v) for c, v in zip(codes, national, strict=True)},
        "ideological_swing": dict(zip(IDEOLOGY_DIMS, (_r(v) for v in swing), strict=True)),
        "provinces": {
            pc: {c: _r(prov[i, j]) for j, c in enumerate(codes)} for i, pc in enumerate(f.province_codes)
        },
        "events": ev_record,
        "turnout": {
            "national_shock": _r(t_nat),
            "event_shift": _r(ev_turnout),
            "party": {c: _r(v) for c, v in zip(codes, party_t, strict=True)},
        },
        "municipalities": {
            "codes": list(f.muni_codes),
            "iid": np.round(muni_iid, 5).tolist(),
            "spatial": np.round(spatial, 5).tolist(),
            "turnout": np.round(t_muni, 5).tolist(),
        },
        "sd": sc.model_dump(),
    }
    return ShockDraw(utility=utility, turnout=turnout, party_turnout=party_t, record=record)


# --------------------------------------------------------------------------- simulation
def _stitch_parent(parent: RaceSpec, children: list[RaceVotes]) -> RaceVotes:
    """National presidential race = union of its province contests (line keys aligned)."""
    keys = parent.line_keys
    col = {k: i for i, k in enumerate(keys)}
    units = np.concatenate([c.unit_index for c in children])
    order = np.argsort(units, kind="stable")
    n = len(units)
    votes = np.zeros((n, len(keys)), dtype=np.int64)
    exp = np.zeros((n, len(keys)))
    row = 0
    for c in children:
        k = len(c.unit_index)
        for j, lk in enumerate(c.line_keys):
            if lk in col:
                votes[row : row + k, col[lk]] = c.votes[:, j]
                if c.expected_shares is not None:
                    exp[row : row + k, col[lk]] = c.expected_shares[:, j]
            elif c.votes[:, j].any():
                raise ElectionError(f"{parent.key}: line {lk} of {c.race_key} is not on the national ballot")
        row += k
    cat = lambda name: np.concatenate([getattr(c, name) for c in children])  # noqa: E731
    return RaceVotes(
        race_key=parent.key,
        line_keys=list(keys),
        unit_index=units[order],
        votes=votes[order],
        ballots_cast=cat("ballots_cast")[order],
        blank=cat("blank")[order],
        invalid=cat("invalid")[order],
        eligible=cat("eligible")[order],
        expected_shares=exp[order],
        expected_turnout=cat("expected_turnout")[order],
    )


def simulate_election(
    model: StructuralModel,
    races: Sequence[RaceSpec],
    seed: int,
    context: ElectionContext | None = None,
) -> ElectionDraw:
    """Simulate one complete election (turnout + unit-level votes of every race).

    Deterministic: the same model, races, seed and context give an identical draw.  A
    ``PRESIDENT`` parent race whose ``PRESIDENT_PROVINCE`` children are also passed is assembled
    from the children (so the national popular vote equals the sum of the province contests).
    """
    ctx = context if context is not None else ElectionContext.from_scenario(model.scenario)
    f = model.frame
    keys = [r.key for r in races]
    if len(set(keys)) != len(keys):
        raise ElectionError("duplicate race keys")
    with Timer(log, f"simulate election ({len(races)} races)"):
        shift, tshift = context_shifts(model, ctx)
        expected = model.party_state(
            None, environment=ctx.include_environment, utility_shift=shift, turnout_shift=tshift
        )
        shocks = draw_shocks(model, seed, ctx)
        realised = model.party_state(
            None,
            environment=ctx.include_environment,
            utility_shift=shift + shocks.utility,
            turnout_shift=tshift + shocks.turnout,
            party_turnout_shift=shocks.party_turnout,
        )
        eligible = f.unit_eligible.astype(np.int64)
        p_turn = np.clip(realised.turnout, 0.0, 1.0)
        ballots = make_rng(seed, "ballots").binomial(eligible, p_turn).astype(np.int64)
        draw = ElectionDraw(
            seed=int(seed), turnout=UnitTurnout(eligible=eligible.copy(), ballots_cast=ballots)
        )
        line_shock_rec: dict[str, dict[str, float]] = {}
        strategic_rec: dict[str, dict[str, float]] = {}
        child_types = {RaceType.PRESIDENT_PROVINCE}
        has_children = any(RaceType(r.race_type) in child_types for r in races)
        parents: list[RaceSpec] = []
        lsd = model.config.candidates.race_line_sd
        for race in races:
            if RaceType(race.race_type) == RaceType.PRESIDENT and has_children:
                parents.append(race)
                continue
            plan = prepare_race(model, race, ctx, expected)
            units = plan.units
            shocks_l = np.array(
                [
                    make_rng(seed, "race", race.key, "line", ln.key).standard_normal() * lsd
                    for ln in race.lines
                ]
            )
            line_shock_rec[race.key] = {ln.key: _r(v) for ln, v in zip(race.lines, shocks_l, strict=True)}
            if plan.defect.any():
                strategic_rec[race.key] = {
                    ln.key: _r(v) for ln, v in zip(race.lines, plan.defect, strict=True) if v > 0
                }
            pi = realised.vote_share[units]
            shares = plan.shares(pi, shocks_l)
            blank_p = plan.blank_probability(pi)
            rng = make_rng(seed, "race", race.key, "votes")
            b = ballots[units]
            invalid = rng.binomial(b, plan.invalid_p).astype(np.int64)
            cond_blank = np.clip(blank_p / np.maximum(1.0 - plan.invalid_p, 1e-12), 0.0, 1.0)
            blank = rng.binomial(b - invalid, cond_blank).astype(np.int64)
            valid = b - invalid - blank
            votes = _multinomial(rng, valid, shares)
            rv = RaceVotes(
                race_key=race.key,
                line_keys=race.line_keys,
                unit_index=units.copy(),
                votes=votes,
                ballots_cast=b.copy(),
                blank=blank,
                invalid=invalid,
                eligible=eligible[units].copy(),
                expected_shares=plan.expected_shares,
                expected_turnout=plan.expected_turnout,
            )
            draw.races[race.key] = rv
        for parent in parents:
            children = [
                draw.races[r.key]
                for r in races
                if RaceType(r.race_type) == RaceType.PRESIDENT_PROVINCE and r.key in draw.races
            ]
            draw.races[parent.key] = _stitch_parent(parent, children)
        total_b, total_e = int(ballots.sum()), int(eligible.sum())
        shocks.record["turnout"]["realised"] = _r(total_b / total_e) if total_e else 0.0
        shocks.record["turnout"]["expected"] = _r(
            float((eligible * expected.turnout).sum() / max(total_e, 1))
        )
        draw.environment = {
            "seed": int(seed),
            "year": ctx.year,
            "election_type": ctx.election_type,
            "president_party": ctx.president_party,
            "model_fingerprint": model.fingerprint,
            "political_geography_seed": model.scenario.scenario.political_geography_seed,
            "environment": {
                "national": dict(model.scenario.environment.national),
                "context_national_shifts": dict(ctx.national_shifts),
            },
            "shocks": shocks.record,
            "race_line_shocks": line_shock_rec,
            "strategic_defection": strategic_rec,
        }
    return draw


def _multinomial(rng: np.random.Generator, n: np.ndarray, p: np.ndarray) -> np.ndarray:
    """Vectorised multinomial draws with row-normalised probabilities (n,) × (n, L) → (n, L)."""
    if p.shape[1] == 1:
        return n.astype(np.int64)[:, None]
    p = np.clip(p, 0.0, None)
    p = p / p.sum(axis=1, keepdims=True)
    # guard against floating-point sums marginally above 1 in the first L−1 categories
    head = p[:, :-1].sum(axis=1)
    over = head > 1.0
    if over.any():
        p[over, :-1] /= head[over, None]
        p[over, -1] = 0.0
    return rng.multinomial(n.astype(np.int64), p).astype(np.int64)
