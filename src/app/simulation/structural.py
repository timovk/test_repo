"""Structural (persistent) political model of the simulated electorate.

The model is a multinomial logit over parties at the level of CBS neighbourhoods (units).  For
unit ``u`` and party ``p`` the *persistent* preference utility is::

    U0[u,p] = α_p + Σ_k β_pk z_uk + urb[p, class_u] + prov[p, prov_u] + Σ_r reg[p,r]·1[u∈r]
              + muni[p, m(u)] + lean[u,p] + δ[u,p]

* ``z`` — REAL CBS demographics, population-weighted z-scores (``GeographyFrame.unit_demo_z``);
* ``β``, ``urb``, ``prov``, ``reg``, ``muni`` — FICTIONAL party parameters (``PartySpec``);
* ``lean`` — FICTIONAL persistent local character: spatial Gaussian-process field over
  municipality centroids + municipality iid + unit iid, drawn from the scenario's
  ``political_geography_seed`` (stable across elections);
* ``α`` — party intercepts calibrated so turnout-weighted national shares match ``base_share``;
* ``δ`` — optional calibration adjustments pinning province / municipality baselines.

Preference shares among eligible voters are ``s = softmax(U0)``.  Supporters of ``p`` turn out
with probability ``q[u,p] = σ(τ_u + t_p)`` (``τ_u`` persistent turnout logit, ``t_p`` the party's
``turnout_propensity``), so unit turnout is ``T_u = Σ_p s_up q_up`` and vote shares are
``π_up = s_up q_up / T_u``.  See docs/SIMULATION.md for the full specification.

This module is a pure engine (NumPy in, arrays/dataclasses out); it never touches the database.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import config_path
from app.core.errors import ScenarioError
from app.core.logging import Timer, get_logger
from app.core.rng import config_hash, make_rng
from app.geography.frame import GeographyFrame
from app.scenarios.schema import ScenarioDocument
from app.simulation.config import ModelConfig, load_model_config
from app.simulation.regions import RegionsConfig, ResolvedRegions, resolve_regions
from app.simulation.spatial import draw_field, gp_cholesky

log = get_logger(__name__)

IDEOLOGY_DIMS: tuple[str, ...] = ("economic", "social", "europe")
_EPS = 1e-300


# --------------------------------------------------------------------------- small numerics
def softmax_rows(V: np.ndarray) -> np.ndarray:
    """Row-wise softmax of a 2-D utility matrix (numerically stable)."""
    m = V.max(axis=1, keepdims=True)
    e = np.exp(V - m)
    return e / e.sum(axis=1, keepdims=True)


def expit(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * np.asarray(x, dtype=float)))


def logit(p: float | np.ndarray) -> float | np.ndarray:
    p = np.clip(p, 1e-12, 1 - 1e-12)
    return np.log(p / (1.0 - p))


def weighted_standardize(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted z-score of a vector (0 when constant)."""
    w = np.asarray(w, dtype=float)
    if w.sum() <= 0:
        w = np.ones_like(w)
    mu = float((x * w).sum() / w.sum())
    sd = float(np.sqrt((((x - mu) ** 2) * w).sum() / w.sum()))
    return (x - mu) / sd if sd > 1e-12 else np.zeros_like(x, dtype=float)


# --------------------------------------------------------------------------- environment events
class EventSpec(BaseModel):
    """A documented election-specific event (``environment.events`` entry).

    Recognised keys: ``name``, ``description``, ``national`` (party → logit shift), ``provinces``
    (province → party → shift), ``turnout`` (logit shift of national turnout) and ``probability``
    (< 1: the event happens only in some draws; the model expectation includes probability × effect).
    Other keys (dates, notes …) are kept for other subsystems and ignored here.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    description: str | None = None
    national: dict[str, float] = Field(default_factory=dict)
    provinces: dict[str, dict[str, float]] = Field(default_factory=dict)
    turnout: float = 0.0
    probability: float = Field(1.0, ge=0, le=1)


def parse_events(events: Sequence[Mapping[str, Any]]) -> list[EventSpec]:
    """Validate ``environment.events`` (raises :class:`ScenarioError`)."""
    out: list[EventSpec] = []
    for i, ev in enumerate(events):
        try:
            out.append(EventSpec.model_validate(ev))
        except Exception as exc:
            raise ScenarioError(f"environment.events[{i}] is invalid: {exc}") from exc
    names = [e.name for e in out]
    if len(set(names)) != len(names):
        raise ScenarioError("environment.events: duplicate event names")
    return out


# --------------------------------------------------------------------------- results
@dataclass
class PartyState:
    """Party-level state for a set of units (all parties on the ballot)."""

    preference: np.ndarray  # (n, P) s — support among eligible voters
    turnout: np.ndarray  # (n,) T — expected share of eligible voters casting a ballot
    vote_share: np.ndarray  # (n, P) π — support among voters


@dataclass
class CalibrationReport:
    """Outcome of the intercept / baseline calibration (stored with the model for audit)."""

    iterations: int
    converged: bool
    national_target: dict[str, float]
    national_achieved: dict[str, float]
    turnout_target: float
    turnout_achieved: float
    province_targets: dict[str, dict[str, float]] = field(default_factory=dict)
    province_achieved: dict[str, dict[str, float]] = field(default_factory=dict)
    municipality_targets: dict[str, dict[str, float]] = field(default_factory=dict)
    municipality_achieved: dict[str, dict[str, float]] = field(default_factory=dict)
    imported_baseline: str | None = None
    imported_provenance: str | None = None
    max_abs_error: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "iterations": self.iterations,
            "converged": self.converged,
            "national_target": self.national_target,
            "national_achieved": self.national_achieved,
            "turnout_target": self.turnout_target,
            "turnout_achieved": self.turnout_achieved,
            "province_targets": self.province_targets,
            "province_achieved": self.province_achieved,
            "municipality_targets_count": len(self.municipality_targets),
            "imported_baseline": self.imported_baseline,
            "imported_provenance": self.imported_provenance,
            "max_abs_error": self.max_abs_error,
        }


# --------------------------------------------------------------------------- ballot routing
def affinity_distance(a: np.ndarray, b: np.ndarray, weights: np.ndarray) -> np.ndarray:
    """Weighted Euclidean ideology distance between rows of ``a`` (n,3) and ``b`` (m,3) → (n, m)."""
    d = (a[:, None, :] - b[None, :, :]) * weights[None, None, :]
    return np.sqrt((d * d).sum(axis=-1))


def routing_matrix(
    party_ideology: np.ndarray,
    line_party: np.ndarray,
    line_ideology: np.ndarray,
    line_withdrawn: np.ndarray,
    *,
    dim_weights: np.ndarray,
    temperature: float,
    abstain_share: float,
    withdrawn_residual: float,
    independent_extra_distance: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Linear map from party vote shares to ballot-line support masses.

    Returns ``R`` (P, L) and ``a`` (P,) such that for vote shares ``π`` (n, P) the line masses are
    ``π @ R`` and the share of voters abstaining in this race (undervote) is ``π @ a``.

    * a party with active lines splits its support equally over them;
    * a withdrawn line keeps ``withdrawn_residual`` of its party's support, the rest is orphaned;
    * orphaned support (party absent or withdrawn) moves to active lines by affinity
      ``softmax(−d/temperature)`` (``independent_extra_distance`` added for independents) except
      a share ``abstain_share`` that abstains.
    """
    P = party_ideology.shape[0]
    L = len(line_party)
    R = np.zeros((P, L))
    a = np.zeros(P)
    active = ~line_withdrawn
    orphan = np.zeros(P)
    for p in range(P):
        own = np.flatnonzero(line_party == p)
        own_active = own[active[own]]
        own_wd = own[~active[own]]
        if own_active.size:
            wd_mass = withdrawn_residual / (own_active.size + own_wd.size)
            R[p, own_wd] = wd_mass
            R[p, own_active] = (1.0 - wd_mass * own_wd.size) / own_active.size
        elif own_wd.size:
            R[p, own_wd] = withdrawn_residual / own_wd.size
            orphan[p] = 1.0 - withdrawn_residual
        else:
            orphan[p] = 1.0
    if orphan.any():
        targets = np.flatnonzero(active) if active.any() else np.arange(L)
        if targets.size:
            d = affinity_distance(party_ideology, line_ideology[targets], dim_weights)
            d = d + independent_extra_distance * (line_party[targets] < 0)[None, :]
            logits = -d / temperature
            w = softmax_rows(logits)  # (P, |targets|)
            R[:, targets] += (orphan * (1.0 - abstain_share))[:, None] * w
            a = orphan * abstain_share
        else:  # pragma: no cover - a race always has at least one line
            a = orphan.copy()
    return R, a


# --------------------------------------------------------------------------- the model
class StructuralModel:
    """Persistent unit × party political model built from a frame and a scenario.

    Build with :meth:`build`; all arrays are aligned with ``frame`` units (U) and
    ``party_codes`` (P).
    """

    def __init__(
        self,
        *,
        frame: GeographyFrame,
        scenario: ScenarioDocument,
        config: ModelConfig,
        regions: ResolvedRegions,
        party_codes: list[str],
        ideology: np.ndarray,
        turnout_propensity: np.ndarray,
        structural_utility: np.ndarray,
        components: dict[str, np.ndarray],
        tau_structural: np.ndarray,
        demo_z: np.ndarray,
        warnings: list[str],
    ) -> None:
        self.frame = frame
        self.scenario = scenario
        self.config = config
        self.regions = regions
        self.party_codes = party_codes
        self.party_index: dict[str, int] = {c: i for i, c in enumerate(party_codes)}
        self.ideology = ideology
        self.turnout_propensity = turnout_propensity
        self.components = components
        self.demo_z = demo_z
        self.warnings = warnings
        self.eligible = frame.unit_eligible.astype(float)
        self._S = structural_utility  # U0 without α and calibration adjustments
        self._tau_s = tau_structural  # τ without τ0
        P = len(party_codes)
        self.alpha = np.zeros(P)
        self.tau0 = 0.0
        self.calib_adjust = np.zeros_like(structural_utility)  # δ (U, P)
        self.elasticity = np.ones(frame.n_units)
        self.env_utility = np.zeros_like(structural_utility)
        self.env_turnout = 0.0
        self.events: list[EventSpec] = []
        self.env_national = np.zeros(P)
        self.env_province = np.zeros((frame.n_provinces, P))
        self.calibration: CalibrationReport | None = None
        self.dim_weights = np.array([config.affinity.dimension_weights[d] for d in IDEOLOGY_DIMS])
        self.invalid_multiplier = np.ones(frame.n_units)
        self.blank_multiplier = np.ones(frame.n_units)
        self.fingerprint = ""

    # ------------------------------------------------------------------ construction
    @classmethod
    def build(
        cls,
        frame: GeographyFrame,
        scenario: ScenarioDocument,
        model_config: ModelConfig | None = None,
        regions: RegionsConfig | ResolvedRegions | None = None,
        *,
        strict: bool = False,
    ) -> StructuralModel:
        """Build and calibrate the structural model.

        ``regions`` may be a :class:`RegionsConfig` (resolved against ``frame``), an already
        :class:`ResolvedRegions`, or ``None`` (``config/regions.yaml``).  With ``strict=True``
        references to municipalities that do not exist in ``frame`` raise :class:`ScenarioError`;
        otherwise they are skipped with a warning (``model.warnings``), which lets the REAL demo
        scenarios run on the synthetic test frame.
        """
        cfg = model_config if model_config is not None else load_model_config()
        with Timer(log, "structural model build"):
            if isinstance(regions, ResolvedRegions):
                resolved = regions
            else:
                resolved = resolve_regions(frame, regions, log_warnings=False)
            model = _assemble(frame, scenario, cfg, resolved, strict=strict)
            model._calibrate()
            model._finalise()
        return model

    # ------------------------------------------------------------------ basic properties
    @property
    def n_parties(self) -> int:
        return len(self.party_codes)

    @property
    def U0(self) -> np.ndarray:
        """Persistent preference utility (U, P): structure + intercepts + calibration adjustments."""
        return self._S + self.alpha[None, :] + self.calib_adjust

    @property
    def tau(self) -> np.ndarray:
        """Persistent turnout logit (U,)."""
        return self._tau_s + self.tau0

    def party_idx(self, codes: Iterable[str]) -> np.ndarray:
        try:
            return np.array([self.party_index[c] for c in codes], dtype=np.int64)
        except KeyError as exc:
            raise ScenarioError(f"unknown party {exc.args[0]!r}") from exc

    def _units(self, units: np.ndarray | Sequence[int] | None) -> np.ndarray:
        if units is None:
            return np.arange(self.frame.n_units)
        return np.asarray(units, dtype=np.int64)

    # ------------------------------------------------------------------ party state
    def party_state(
        self,
        units: np.ndarray | Sequence[int] | None = None,
        *,
        environment: bool = True,
        utility_shift: np.ndarray | None = None,
        turnout_shift: np.ndarray | float | None = None,
        party_turnout_shift: np.ndarray | None = None,
    ) -> PartyState:
        """Preference shares, turnout and vote shares for ``units`` (all parties on the ballot).

        ``environment`` adds the scenario's deterministic election environment (national,
        provincial and expected event shifts); ``utility_shift`` (n, P) and ``turnout_shift``
        (n,) add further (e.g. random) components for the same units.
        """
        idx = self._units(units)
        if units is None:
            V = self._S + self.alpha[None, :] + self.calib_adjust
            tau = self._tau_s + self.tau0
        else:
            V = self._S[idx] + self.alpha[None, :] + self.calib_adjust[idx]
            tau = self._tau_s[idx] + self.tau0
        if environment:
            V = V + self.env_utility[idx]
            tau = tau + self.env_turnout
        if utility_shift is not None:
            V = V + utility_shift
        if turnout_shift is not None:
            tau = tau + turnout_shift
        tp = (
            self.turnout_propensity
            if party_turnout_shift is None
            else self.turnout_propensity + party_turnout_shift
        )
        return self._state(V, tau, tp)

    def _state(self, V: np.ndarray, tau: np.ndarray, tp: np.ndarray) -> PartyState:
        s = softmax_rows(V)
        tc = self.config.turnout
        q = np.clip(expit(tau[:, None] + tp[None, :]), tc.min_probability, tc.max_probability)
        w = s * q
        T = w.sum(axis=1)
        return PartyState(preference=s, turnout=T, vote_share=w / T[:, None])

    # ------------------------------------------------------------------ expectations
    def unit_expected_turnout(
        self, units: np.ndarray | Sequence[int] | None = None, *, environment: bool = True
    ) -> np.ndarray:
        """Expected share of eligible voters casting a ballot, per unit (no shocks)."""
        return self.party_state(units, environment=environment).turnout

    def routing(self, parties: Sequence[str]) -> tuple[np.ndarray, np.ndarray]:
        """Routing matrix for a ballot listing exactly ``parties`` (one line each)."""
        idx = self.party_idx(parties)
        ac = self.config.affinity
        return routing_matrix(
            self.ideology,
            idx,
            self.ideology[idx],
            np.zeros(len(idx), dtype=bool),
            dim_weights=self.dim_weights,
            temperature=ac.temperature,
            abstain_share=ac.absent_abstain_share,
            withdrawn_residual=self.config.candidates.withdrawn_residual_share,
            independent_extra_distance=ac.independent_extra_distance,
        )

    def expected_shares(
        self,
        units: np.ndarray | Sequence[int] | None = None,
        parties: Sequence[str] | None = None,
        *,
        environment: bool = True,
    ) -> np.ndarray:
        """Expected valid-vote shares (n, len(parties)) when only ``parties`` are on the ballot.

        Support of absent parties moves to the listed ones by ideological affinity; a configurable
        share abstains.  ``parties=None`` means every party of the scenario.
        """
        parties = list(parties) if parties is not None else self.party_codes
        st = self.party_state(units, environment=environment)
        R, _ = self.routing(parties)
        m = st.vote_share @ R
        return m / m.sum(axis=1, keepdims=True)

    def expected_valid_weights(
        self,
        units: np.ndarray | Sequence[int] | None = None,
        parties: Sequence[str] | None = None,
        *,
        environment: bool = True,
    ) -> tuple[np.ndarray, np.ndarray]:
        """(shares (n, L), expected valid votes (n,)) for a party-only ballot."""
        idx = self._units(units)
        parties = list(parties) if parties is not None else self.party_codes
        st = self.party_state(idx, environment=environment)
        R, a = self.routing(parties)
        m = st.vote_share @ R
        shares = m / m.sum(axis=1, keepdims=True)
        valid = self.eligible[idx] * st.turnout * (1.0 - st.vote_share @ a)
        return shares, valid

    def aggregate_expected_shares(
        self,
        level: str = "national",
        parties: Sequence[str] | None = None,
        *,
        environment: bool = True,
    ) -> pd.DataFrame:
        """Expected shares aggregated to ``national`` | ``province`` | ``municipality`` (rows) × parties.

        Units are weighted by expected valid votes (eligible × turnout × non-abstention).
        """
        parties = list(parties) if parties is not None else list(self.party_codes)
        shares, valid = self.expected_valid_weights(None, parties, environment=environment)
        votes = shares * valid[:, None]
        f = self.frame
        if level == "national":
            tot = votes.sum(axis=0, keepdims=True)
            index = ["NL"]
        elif level == "province":
            tot = f.to_provinces(votes)
            index = list(f.province_codes)
        elif level == "municipality":
            tot = f.to_munis(votes)
            index = list(f.muni_codes)
        else:
            raise ValueError(f"unknown level {level!r}")
        denom = tot.sum(axis=1, keepdims=True)
        denom[denom == 0] = 1.0
        return pd.DataFrame(tot / denom, index=pd.Index(index, name="geo_code"), columns=parties)

    def national_shares(
        self, parties: Sequence[str] | None = None, *, environment: bool = True
    ) -> dict[str, float]:
        df = self.aggregate_expected_shares("national", parties, environment=environment)
        return {c: float(df.iloc[0][c]) for c in df.columns}

    def province_shares(
        self, parties: Sequence[str] | None = None, *, environment: bool = True
    ) -> pd.DataFrame:
        return self.aggregate_expected_shares("province", parties, environment=environment)

    def municipality_shares(
        self, parties: Sequence[str] | None = None, *, environment: bool = True
    ) -> pd.DataFrame:
        return self.aggregate_expected_shares("municipality", parties, environment=environment)

    def jurisdiction_shares(
        self,
        units: np.ndarray | Sequence[int],
        parties: Sequence[str] | None = None,
        *,
        environment: bool = True,
    ) -> np.ndarray:
        """Expected shares (L,) of a jurisdiction (e.g. a House district) for a party-only ballot."""
        shares, valid = self.expected_valid_weights(units, parties, environment=environment)
        tot = (shares * valid[:, None]).sum(axis=0)
        s = tot.sum()
        return tot / s if s > 0 else np.full(len(tot), 1.0 / max(len(tot), 1))

    def expected_turnout_by(self, level: str = "national", *, environment: bool = True) -> pd.Series:
        """Expected turnout (ballots / eligible) aggregated to a level."""
        T = self.unit_expected_turnout(environment=environment)
        ballots = self.eligible * T
        f = self.frame
        if level == "national":
            return pd.Series([ballots.sum() / max(self.eligible.sum(), 1.0)], index=["NL"])
        if level == "province":
            return pd.Series(
                f.to_provinces(ballots) / np.maximum(f.to_provinces(self.eligible), 1.0),
                index=f.province_codes,
            )
        if level == "municipality":
            return pd.Series(
                f.to_munis(ballots) / np.maximum(f.to_munis(self.eligible), 1.0), index=f.muni_codes
            )
        raise ValueError(f"unknown level {level!r}")

    def affinity_matrix(self) -> pd.DataFrame:
        """Transfer weights: row party's support → column party when the row party is absent."""
        d = affinity_distance(self.ideology, self.ideology, self.dim_weights)
        np.fill_diagonal(d, np.inf)
        w = softmax_rows(-d / self.config.affinity.temperature)
        return pd.DataFrame(w, index=self.party_codes, columns=self.party_codes)

    def summary(self) -> dict[str, Any]:
        """JSON-serialisable description (used for audit logs and the API)."""
        return {
            "parties": self.party_codes,
            "alpha": {c: round(float(a), 6) for c, a in zip(self.party_codes, self.alpha, strict=True)},
            "tau0": round(float(self.tau0), 6),
            "calibration": self.calibration.to_dict() if self.calibration else None,
            "political_geography_seed": self.scenario.scenario.political_geography_seed,
            "fingerprint": self.fingerprint,
            "warnings": list(self.warnings),
            "regions": {r: int(self.regions.muni_mask(r).sum()) for r in self.regions.names},
        }

    # ------------------------------------------------------------------ calibration
    def _votes(self, st: PartyState, idx: np.ndarray) -> np.ndarray:
        return st.vote_share * (self.eligible[idx] * st.turnout)[:, None]

    def _calibrate(self) -> None:
        doc = self.scenario
        cc = self.config.calibration
        f = self.frame
        P = self.n_parties
        base = np.array([doc.party(c).base_share for c in self.party_codes], dtype=float)
        base = base / base.sum()
        national_target = _complete_national(doc.calibration.national, base, self.party_index)
        turnout_target = float(doc.environment.turnout_base)
        tol = min(cc.tolerance, float(doc.calibration.tolerance))
        max_iter = max(cc.max_iterations, int(doc.calibration.max_iterations))

        # --- pinned geographies (targets as (G, P) matrices, NaN = not listed) ---------------
        prov_t = np.full((f.n_provinces, P), np.nan)
        for pcode, tgt in doc.calibration.provinces.items():
            for k, v in tgt.items():
                prov_t[f.province_index(pcode), self.party_index[k]] = float(v)
        muni_t = np.full((f.n_munis, P), np.nan)
        imported_label = imported_prov = None
        self.alpha = np.log(national_target)
        it0, _, _ = self._calibrate_loop(national_target, turnout_target, prov_t, muni_t, tol, max_iter)
        if doc.calibration.imported_baseline:
            imported, imported_prov = _load_imported(doc.calibration.imported_baseline, f, self.party_codes)
            imported_label = doc.calibration.imported_baseline
            w = float(doc.calibration.imported_baseline_weight)
            current = self.aggregate_expected_shares("municipality", environment=False).to_numpy()
            for m, vec in imported.items():
                muni_t[m] = w * vec + (1.0 - w) * current[m]
            log.info(
                "imported baseline %s blended into %d municipalities (weight %.2f)",
                imported_label,
                len(imported),
                w,
            )
        for mcode, tgt in doc.calibration.municipalities.items():
            m = f._muni_lookup.get(mcode)
            if m is None:
                continue  # reported by _assemble
            muni_t[m] = np.nan
            for k, v in tgt.items():
                muni_t[m, self.party_index[k]] = float(v)

        it, converged, national_active = self._calibrate_loop(
            national_target, turnout_target, prov_t, muni_t, tol, max_iter
        )

        # --- report --------------------------------------------------------------------
        nat = self.national_shares(environment=False)
        prov_df = self.aggregate_expected_shares("province", environment=False)
        muni_df = self.aggregate_expected_shares("municipality", environment=False)
        errors: list[float] = []
        if national_active:
            errors.extend(abs(nat[c] - national_target[i]) for i, c in enumerate(self.party_codes))
        report: dict[str, tuple[dict, dict]] = {}
        for level, tmat, df, codes in (
            ("province", prov_t, prov_df, f.province_codes),
            ("municipality", muni_t, muni_df, f.muni_codes),
        ):
            rows = np.flatnonzero(~np.isnan(tmat).all(axis=1))
            full = _complete_local_matrix(tmat[rows], df.to_numpy()[rows])
            tgt_rep = {
                codes[g]: dict(zip(self.party_codes, map(float, full[i]), strict=True))
                for i, g in enumerate(rows)
            }
            ach_rep = {
                codes[g]: dict(zip(self.party_codes, map(float, df.to_numpy()[g]), strict=True)) for g in rows
            }
            if rows.size:
                errors.append(float(np.abs(full - df.to_numpy()[rows]).max()))
            report[level] = (tgt_rep, ach_rep)
        T_nat = float(self.expected_turnout_by("national", environment=False).iloc[0])
        errors.append(abs(T_nat - turnout_target))
        self.calibration = CalibrationReport(
            iterations=it0 + it,
            converged=converged,
            national_target={c: float(national_target[i]) for i, c in enumerate(self.party_codes)},
            national_achieved=nat,
            turnout_target=turnout_target,
            turnout_achieved=T_nat,
            province_targets=report["province"][0],
            province_achieved=report["province"][1],
            municipality_targets=report["municipality"][0],
            municipality_achieved=report["municipality"][1],
            imported_baseline=imported_label,
            imported_provenance=imported_prov,
            max_abs_error=float(max(errors) if errors else 0.0),
        )
        if not national_active:
            self.warnings.append(
                "all eligible voters live in pinned areas: national base shares are not enforced"
            )
        if not converged:
            msg = f"calibration did not converge after {it} iterations (max error {self.calibration.max_abs_error:.2e})"
            self.warnings.append(msg)
            log.warning(msg)
        else:
            log.debug("calibration converged in %d iterations", it)

    def _calibrate_loop(
        self,
        national_target: np.ndarray,
        turnout_target: float,
        prov_t: np.ndarray,
        muni_t: np.ndarray,
        tol: float,
        max_iter: int,
    ) -> tuple[int, bool, bool]:
        """Gauss–Seidel fixed-point iteration on α, τ0 and the pinned δ's (vectorised).

        Each geography is steered through the units it *controls*: pinned municipalities through
        their own units, pinned provinces through their units outside pinned municipalities and
        the national intercepts through all remaining units (pinned areas count as fixed).
        Returns (iterations, converged, national step active).
        """
        f = self.frame
        all_idx = np.arange(f.n_units)
        E = self.eligible
        pinned_m = ~np.isnan(muni_t).all(axis=1)
        pinned_p = ~np.isnan(prov_t).all(axis=1)
        unit_pm = pinned_m[f.unit_muni]
        unit_pp = pinned_p[f.unit_province]
        national_group = ~(unit_pm | unit_pp)
        unpinned_share = float(E[national_group].sum() / max(E.sum(), 1.0))
        do_national = unpinned_share >= self.config.calibration.min_unpinned_share
        prov_rows = np.flatnonzero(pinned_p)
        muni_rows = np.flatnonzero(pinned_m)
        log_target = np.log(national_target)
        tc = self.config.turnout
        clip = 3.0
        it = 0
        for it in range(1, max_iter + 1):
            max_err = 0.0
            # --- national intercepts α and turnout τ0 ----------------------------------------
            st = self.party_state(all_idx, environment=False)
            votes = self._votes(st, all_idx)
            total = votes.sum()
            if do_national and total > 0:
                outside = votes[~national_group].sum(axis=0)
                inside = votes[national_group].sum(axis=0)
                desired = np.maximum(national_target * total - outside, 1e-9 * total)
                step = np.clip(np.log(desired) - np.log(np.maximum(inside, _EPS)), -clip, clip)
                nat_share = votes.sum(axis=0) / total
                max_err = max(max_err, float(np.abs(np.log(nat_share) - log_target).max()))
                self.alpha = self.alpha + step
                self.alpha -= self.alpha.mean()
            T_nat = float((E * st.turnout).sum() / max(E.sum(), 1.0))
            q = np.clip(
                expit(self.tau[:, None] + self.turnout_propensity[None, :]),
                tc.min_probability,
                tc.max_probability,
            )
            slope = float((E * (st.preference * q * (1.0 - q)).sum(axis=1)).sum() / max(E.sum(), 1.0))
            t_err = turnout_target - T_nat
            self.tau0 += float(np.clip(t_err / max(slope, 1e-6), -clip, clip))
            max_err = max(max_err, abs(t_err))
            # --- pinned provinces ------------------------------------------------------------
            if prov_rows.size:
                st = self.party_state(all_idx, environment=False)
                v = self._votes(st, all_idx)
                tot = f.to_provinces(v)[prov_rows]  # (G, P)
                out = f.to_provinces(v * unit_pm[:, None])[prov_rows]
                ins = tot - out
                share = tot / np.maximum(tot.sum(axis=1, keepdims=True), _EPS)
                full = _complete_local_matrix(prov_t[prov_rows], share)
                desired = np.maximum(
                    full * tot.sum(axis=1, keepdims=True) - out, 1e-9 * tot.sum(axis=1, keepdims=True)
                )
                step = np.clip(np.log(desired) - np.log(np.maximum(ins, _EPS)), -clip, clip)
                step[ins.sum(axis=1) <= 0] = 0.0
                step_full = np.zeros((f.n_provinces, self.n_parties))
                step_full[prov_rows] = step
                self.calib_adjust += step_full[f.unit_province]
                max_err = max(max_err, float(np.abs(np.log(np.maximum(share, _EPS)) - np.log(full)).max()))
            # --- pinned municipalities ------------------------------------------------------
            if muni_rows.size:
                st = self.party_state(all_idx, environment=False)
                v = f.to_munis(self._votes(st, all_idx))[muni_rows]
                vs = v.sum(axis=1, keepdims=True)
                share = v / np.maximum(vs, _EPS)
                full = _complete_local_matrix(muni_t[muni_rows], share)
                step = np.clip(np.log(full) - np.log(np.maximum(share, _EPS)), -clip, clip)
                step[vs[:, 0] <= 0] = 0.0
                step_full = np.zeros((f.n_munis, self.n_parties))
                step_full[muni_rows] = step
                self.calib_adjust += step_full[f.unit_muni]
                max_err = max(max_err, float(np.abs(step).max()))
            if max_err < tol:
                return it, True, do_national
        return it, False, do_national

    # ------------------------------------------------------------------ post-calibration
    def _finalise(self) -> None:
        doc = self.scenario
        f = self.frame
        cfg = self.config
        pgs = doc.scenario.political_geography_seed
        # --- elasticity -----------------------------------------------------------------
        st = self.party_state(environment=False)
        s = st.preference
        H = -(s * np.log(np.maximum(s, _EPS))).sum(axis=1) / np.log(max(self.n_parties, 2))
        w = self.eligible if self.eligible.sum() > 0 else np.ones(f.n_units)
        ec = cfg.elasticity
        sub_map = np.array(
            [ec.suburban_by_class.get(str(k), ec.suburban_by_class.get("0", 0.5)) for k in range(6)]
        )
        urb = np.clip(f.unit_urbanity_class, 0, 5)
        suburban = sub_map[urb]
        noise_m = make_rng(pgs, "elasticity", "municipality").standard_normal(f.n_munis)
        raw = (
            ec.entropy_weight * weighted_standardize(H, w)
            + ec.suburban_weight * weighted_standardize(suburban, w)
            + ec.noise_weight * noise_m[f.unit_muni]
        )
        raw = weighted_standardize(raw, w) * ec.log_sd
        e = np.exp(raw)
        e = e / (e * w).sum() * w.sum()
        e = e ** float(doc.environment.elasticity_strength)
        e = np.clip(e / ((e * w).sum() / w.sum()), ec.min, ec.max)
        self.elasticity = e / ((e * w).sum() / w.sum())
        # --- deterministic election environment ----------------------------------------------
        self.events = parse_events(doc.environment.events)
        nat = np.zeros(self.n_parties)
        prov = np.zeros((f.n_provinces, self.n_parties))
        for code, v in doc.environment.national.items():
            nat[self.party_index[code]] += v
        for pcode, shifts in doc.environment.provinces.items():
            for code, v in shifts.items():
                prov[f.province_index(pcode), self.party_index[code]] += v
        turnout_shift = 0.0
        for ev in self.events:
            for code, v in ev.national.items():
                nat[self.party_index[code]] += ev.probability * v
            for pcode, shifts in ev.provinces.items():
                for code, v in shifts.items():
                    prov[f.province_index(pcode), self.party_index[code]] += ev.probability * v
            turnout_shift += ev.probability * ev.turnout
        self.env_national = nat
        self.env_province = prov
        self.env_utility = self.elasticity[:, None] * nat[None, :] + prov[f.unit_province]
        self.env_turnout = turnout_shift
        # --- blank / invalid multipliers (mean 1 over eligible voters) -----------------------
        tc = cfg.turnout
        names = list(f.demo_names)
        if "pct_education_low" in names:
            inv = np.exp(tc.invalid_low_education_effect * self.demo_z[:, names.index("pct_education_low")])
            self.invalid_multiplier = inv / ((inv * w).sum() / w.sum())
        if "pct_age_65_plus" in names:
            bl = np.exp(tc.blank_age_effect * self.demo_z[:, names.index("pct_age_65_plus")])
            self.blank_multiplier = bl / ((bl * w).sum() / w.sum())
        payload = json.dumps(
            {
                "scenario": self.scenario.model_dump(mode="json"),
                "model": cfg.model_dump(mode="json"),
                "U": f.n_units,
            },
            sort_keys=True,
            default=str,
        )
        self.fingerprint = config_hash(payload)


# --------------------------------------------------------------------------- assembly helpers
def _complete_national(listed: Mapping[str, float], base: np.ndarray, pidx: Mapping[str, int]) -> np.ndarray:
    """National target vector: listed shares (normalised if they cover every party or exceed 1);
    unlisted parties share the remainder in proportion to their ``base_share``."""
    P = len(base)
    if not listed:
        return base.copy()
    t = np.zeros(P)
    mask = np.zeros(P, dtype=bool)
    for k, v in listed.items():
        t[pidx[k]] = float(v)
        mask[pidx[k]] = True
    total = t[mask].sum()
    if mask.all() or total >= 1.0:
        if not mask.all():
            log.warning("calibration.national shares sum to %.3f ≥ 1; unlisted parties get 1%%", total)
            t[mask] *= 0.99 / total
            rest = base[~mask] / base[~mask].sum() * 0.01
            t[~mask] = rest
            return t
        return t / total
    t[~mask] = base[~mask] / base[~mask].sum() * (1.0 - total)
    return t


def _complete_local_matrix(targets: np.ndarray, current: np.ndarray) -> np.ndarray:
    """Complete local target rows (G, P) with NaN for unlisted parties.

    Listed parties keep their target (rows listing every party are normalised; rows whose listed
    shares exceed 99.5 % are scaled down); unlisted parties share the remainder in proportion to
    their ``current`` shares.
    """
    t = np.asarray(targets, dtype=float)
    cur = np.maximum(np.asarray(current, dtype=float), 1e-9)
    listed = ~np.isnan(t)
    out = np.where(listed, np.maximum(np.nan_to_num(t), 1e-6), 0.0)
    total = out.sum(axis=1, keepdims=True)
    full_rows = listed.all(axis=1, keepdims=True)
    scale = np.where(
        full_rows,
        1.0 / np.maximum(total, _EPS),
        np.where(total > 0.995, 0.995 / np.maximum(total, _EPS), 1.0),
    )
    out = out * scale
    remainder = 1.0 - out.sum(axis=1, keepdims=True)
    unl = np.where(listed, 0.0, cur)
    unl_sum = unl.sum(axis=1, keepdims=True)
    out = out + np.where(unl_sum > 0, unl / np.maximum(unl_sum, _EPS) * remainder, 0.0)
    return out


def _demographic_z(frame: GeographyFrame, shrinkage: float) -> tuple[np.ndarray, int]:
    """Population-weighted z-scores robust to missing values; imputed cells shrunk to the mean."""
    X = np.asarray(frame.unit_demo, dtype=float)
    U, K = frame.n_units, len(frame.demo_names)
    if X.size == 0 or X.shape != (U, K):
        return np.zeros((U, K)), 0
    finite = np.isfinite(X)
    n_missing = int((~finite).sum())
    if n_missing == 0:
        z = np.array(frame.unit_demo_z, dtype=float, copy=True)
    else:
        w = frame.unit_population.astype(float)
        if w.sum() <= 0:
            w = np.ones(U)
        z = np.zeros((U, K))
        for k in range(K):
            ok = finite[:, k]
            wk = w[ok]
            if not ok.any() or wk.sum() <= 0:
                continue
            mu = float((X[ok, k] * wk).sum() / wk.sum())
            sd = float(np.sqrt((((X[ok, k] - mu) ** 2) * wk).sum() / wk.sum()))
            if sd > 1e-12:
                z[ok, k] = (X[ok, k] - mu) / sd
    imputed = np.asarray(frame.unit_demo_imputed, dtype=bool)
    if imputed.shape == z.shape and imputed.any():
        z[imputed] *= 1.0 - shrinkage
    z[~np.isfinite(z)] = 0.0
    return z, n_missing


def _assemble(
    frame: GeographyFrame,
    doc: ScenarioDocument,
    cfg: ModelConfig,
    regions: ResolvedRegions,
    *,
    strict: bool,
) -> StructuralModel:
    f = frame
    U, M, Pv = f.n_units, f.n_munis, f.n_provinces
    parties = doc.parties
    codes = [p.code for p in parties]
    P = len(codes)
    pidx = {c: i for i, c in enumerate(codes)}
    warnings: list[str] = []
    problems: list[str] = []

    def missing_muni(where: str, code: str) -> None:
        msg = f"{where}: municipality {code} is not in the geography frame (vintage {f.year})"
        (problems if strict else warnings).append(msg)

    # --- references ------------------------------------------------------------------------
    for pcode, shifts in doc.environment.provinces.items():
        if pcode not in f.province_codes:
            problems.append(f"environment.provinces: unknown province {pcode}")
        problems.extend(f"environment.provinces.{pcode}: unknown party {c}" for c in shifts if c not in pidx)
    for pcode, tgt in doc.calibration.provinces.items():
        if pcode not in f.province_codes:
            problems.append(f"calibration.provinces: unknown province {pcode}")
        problems.extend(f"calibration.provinces.{pcode}: unknown party {c}" for c in tgt if c not in pidx)
    for mcode, tgt in doc.calibration.municipalities.items():
        if mcode not in f._muni_lookup:
            missing_muni("calibration.municipalities", mcode)
        problems.extend(
            f"calibration.municipalities.{mcode}: unknown party {c}" for c in tgt if c not in pidx
        )
    try:
        events = parse_events(doc.environment.events)
    except ScenarioError as exc:
        problems.append(str(exc))
        events = []
    for ev in events:
        problems.extend(f"event {ev.name}: unknown party {c}" for c in ev.national if c not in pidx)
        for pcode, shifts in ev.provinces.items():
            if pcode not in f.province_codes:
                problems.append(f"event {ev.name}: unknown province {pcode}")
            problems.extend(f"event {ev.name}: unknown party {c}" for c in shifts if c not in pidx)

    # --- demographics ----------------------------------------------------------------------
    z, n_missing = _demographic_z(f, cfg.lean.imputed_shrinkage)
    if n_missing:
        warnings.append(f"{n_missing} missing demographic values treated as the population mean")
    demo_names = list(f.demo_names)
    B = np.zeros((len(demo_names), P))
    urb = np.zeros((6, P))
    prov = np.zeros((Pv, P))
    reg = np.zeros((len(regions.names), P))
    muni = np.zeros((M, P))
    for j, p in enumerate(parties):
        for k, v in p.demographics.items():
            if k not in demo_names:
                problems.append(
                    f"party {p.code}: unknown demographic variable {k!r} (expected one of {demo_names})"
                )
            else:
                B[demo_names.index(k), j] = v
        for k, v in p.urbanity.items():
            urb[int(k), j] = v
        for k, v in p.provinces.items():
            if k not in f.province_codes:
                problems.append(f"party {p.code}: unknown province {k}")
            else:
                prov[f.province_index(k), j] = v
        for k, v in p.regions.items():
            if k not in regions.names:
                problems.append(f"party {p.code}: unknown region {k!r} (defined: {regions.names})")
            else:
                reg[regions.index(k), j] = v
        for k, v in p.municipalities.items():
            m = f._muni_lookup.get(k)
            if m is None:
                missing_muni(f"party {p.code}", k)
            else:
                muni[m, j] = v
    if problems:
        raise ScenarioError("scenario does not fit the model:\n  - " + "\n  - ".join(problems))
    for r in regions.names:
        if not regions.muni_mask(r).any() and any(p.regions.get(r) for p in parties):
            warnings.append(
                f"region {r} has no municipalities in this frame; its party shifts have no effect"
            )

    ideology = np.array([[getattr(p.ideology, d) for d in IDEOLOGY_DIMS] for p in parties], dtype=float)
    tp = np.array([p.turnout_propensity for p in parties], dtype=float)

    # --- persistent lean field -----------------------------------------------------------------
    lean_muni, lean_unit = _lean_field(f, cfg, codes, ideology, doc.scenario.political_geography_seed)

    demographic = z @ B
    urb_class = np.clip(f.unit_urbanity_class, 0, 5)
    urbanity = urb[urb_class]
    province = prov[f.unit_province]
    region_m = regions.membership.astype(float) @ reg if regions.names else np.zeros((M, P))
    S = demographic + urbanity + province + (region_m + muni + lean_muni)[f.unit_muni] + lean_unit

    # --- persistent turnout logit (without τ0) ---------------------------------------------
    tc = cfg.turnout
    g = np.zeros(len(demo_names))
    for k, v in tc.demographics.items():
        if k in demo_names:
            g[demo_names.index(k)] = v
    t_urb = np.array([tc.urbanity.get(str(k), 0.0) for k in range(6)])
    pgs = doc.scenario.political_geography_seed
    t_muni = make_rng(pgs, "turnout-lean", "municipality").standard_normal(M) * tc.municipality_sd
    t_unit = make_rng(pgs, "turnout-lean", "unit").standard_normal(U) * tc.unit_sd
    tau_s = z @ g + t_urb[urb_class] + t_muni[f.unit_muni] + t_unit

    components = {
        "demographic": demographic,
        "urbanity": urbanity,
        "province": province,
        "region_muni": region_m,
        "municipality_muni": muni,
        "lean_muni": lean_muni,
        "lean_unit": lean_unit,
    }
    if warnings:
        log.warning(
            "%d scenario reference(s) do not fit the frame (see model.warnings), e.g. %s",
            len(warnings),
            warnings[0],
        )
        for w in warnings:
            log.debug(w)
    return StructuralModel(
        frame=f,
        scenario=doc,
        config=cfg,
        regions=regions,
        party_codes=codes,
        ideology=ideology,
        turnout_propensity=tp,
        structural_utility=S,
        components=components,
        tau_structural=tau_s,
        demo_z=z,
        warnings=warnings,
    )


def _lean_field(
    f: GeographyFrame, cfg: ModelConfig, codes: list[str], ideology: np.ndarray, pgs: int
) -> tuple[np.ndarray, np.ndarray]:
    """Persistent lean: municipal (M, P) spatial + iid part and unit-level (U, P) iid part."""
    lc = cfg.lean
    M, U, P = f.n_munis, f.n_units, len(codes)
    rho = lc.ideological_share
    norm = float(np.sqrt((ideology**2).sum(axis=1).mean())) or 1.0
    ideo = ideology / norm  # average squared norm 1 → ideological part has ≈ unit variance
    chol = gp_cholesky(f.muni_xy, lc.spatial_length_km, lc.kernel) if M else np.zeros((0, 0))
    axes_sp = np.column_stack(
        [draw_field(chol, make_rng(pgs, "lean", "spatial", "axis", d), 1)[:, 0] for d in IDEOLOGY_DIMS]
    )
    axes_iid = np.column_stack(
        [make_rng(pgs, "lean", "municipality", "axis", d).standard_normal(M) for d in IDEOLOGY_DIMS]
    )
    party_sp = np.column_stack(
        [draw_field(chol, make_rng(pgs, "lean", "spatial", "party", c), 1)[:, 0] for c in codes]
    )
    party_iid = np.column_stack(
        [make_rng(pgs, "lean", "municipality", "party", c).standard_normal(M) for c in codes]
    )
    spatial = np.sqrt(rho) * axes_sp @ ideo.T + np.sqrt(1.0 - rho) * party_sp
    iid = np.sqrt(rho) * axes_iid @ ideo.T + np.sqrt(1.0 - rho) * party_iid
    lean_muni = lc.spatial_sd * spatial + lc.municipality_sd * iid
    lean_unit = (
        np.column_stack([make_rng(pgs, "lean", "unit", c).standard_normal(U) for c in codes]) * lc.unit_sd
    )
    if P == 0:
        return np.zeros((M, 0)), np.zeros((U, 0))
    return lean_muni, lean_unit


def _load_imported(
    path: str, frame: GeographyFrame, party_codes: list[str]
) -> tuple[dict[int, np.ndarray], str]:
    """Imported historical baseline → {muni index: share vector over model parties}."""
    from app.simulation.baselines import load_imported_baseline

    p = Path(path)
    if not p.is_absolute():
        p = config_path(path)
    imp = load_imported_baseline(p, frame)
    unknown = sorted(set(imp.parties) - set(party_codes))
    if unknown:
        raise ScenarioError(f"imported baseline {path} maps to unknown model parties {unknown}")
    out: dict[int, np.ndarray] = {}
    col = {c: i for i, c in enumerate(imp.parties)}
    for mcode, row in zip(imp.municipality_codes, imp.shares, strict=True):
        m = frame._muni_lookup.get(mcode)
        if m is None:
            continue
        vec = np.zeros(len(party_codes))
        for c in imp.parties:
            vec[party_codes.index(c)] = row[col[c]]
        s = vec.sum()
        if s <= 0:
            continue
        # parties absent from the historical mapping keep a small floor so the logit stays finite
        vec = np.maximum(vec / s, 1e-4)
        out[m] = vec / vec.sum()
    return out, imp.provenance
