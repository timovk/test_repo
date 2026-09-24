"""Probabilistic race-calling engine (SIMULATED election night).

:class:`RaceCaller` looks at a race *as it is being counted* — :class:`RaceProgress` holds only
the counted votes, the reported fraction per unit and the pre-election expectation — and
decides its status (``RaceStatus``) with an auditable, JSON-serialisable evidence snapshot.
The caller never sees final results for unreported ballots.

Method (full description in ``docs/RACE_CALLING.md`` §4):

1. **Reporting.** ``R = Σ f·x / Σ x`` — share of the *expected* ballots ``x`` (expected turnout ×
   eligible) that is counted (``f`` = reported fraction per unit).
2. **Swing.**  A log-linear (multinomial-logit) race swing ``δ`` is fitted so that
   ``Σ_m C_m · softmax(log Ê_m + δ)`` reproduces the counted votes, where ``m`` runs over
   reporting clusters (municipalities), ``C_m`` is their counted valid vote and ``Ê_m`` the
   expected composition of exactly those ballots.  The raw estimate is shrunk toward 0 with a
   normal prior (``swing_prior_sd``); the data variance shrinks with the number and balance of
   reporting clusters (Herfindahl), so the prior tightens as reporting grows.  Each partially
   reported cluster also gets its own shrunk residual ``ρ_m``.
3. **Turnout.**  The ratio of counted ballots to the expected ballots of the counted portion,
   shrunk in log space (``turnout_prior_sd``).
4. **Outstanding vote.**  Unreported ballots per cluster = ratio × expected ballots, with
   composition ``softmax(log e_m + δ + ρ_m)``; the uncounted remainder of partially counted
   units keeps its observed composition.
5. **Simulation.**  The final totals are drawn with correlated uncertainty — a race-level swing
   (Student-t, SD = posterior SD ⊕ ``differential_sd``), race turnout, cluster- and unit-level
   noise (Herfindahl-aggregated), partial-unit noise — linearised around the projection so a
   draw costs O(L²) regardless of the number of units.  ``make_rng(seed, "call", race_key,
   seq)`` drives the draws; win probability = share of draws won.  The number of draws is
   adaptive (``n_draws_min`` → ``n_draws`` → ``n_draws_max``) when an estimate is near a
   threshold — unless exact analytic bounds of the win probabilities (pairwise margins of the
   same linearised model, Student-t mixture tails) already decide every threshold that could
   change the status; the Monte-Carlo estimate is then projected into those bounds.
6. **Mathematical certainty.**  The counted lead exceeds every ballot that could still be
   counted (eligible − counted in every incomplete unit).
7. **State machine** (§3): POLLS_CLOSED (no results) → TOO_EARLY / LEAN / TOO_CLOSE →
   PROJECTED → CALLED; FINAL or RECOUNT at 100 %.  Calls are sticky unless the called line's
   probability drops below ``retraction_threshold`` (→ TOO_CLOSE, ``evidence.retracted``).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np
from scipy.special import chdtri, ndtr

from app.core.constitution import DECIDED_STATUSES, RaceStatus
from app.core.errors import ElectionNightError
from app.core.logging import get_logger
from app.core.rng import make_rng
from app.elections.types import RaceVotes
from app.reporting.config import NightConfig, load_night_config

log = get_logger(__name__)

METHOD_VERSION = "nl-night-caller/1"
#: Statuses in which the engine keeps a winner through later evaluations (sticky calls).
CALL_STATUSES: frozenset[RaceStatus] = frozenset({RaceStatus.PROJECTED, RaceStatus.CALLED})
#: Terminal statuses (reached at 100 % reporting).
LOCKED_STATUSES: frozenset[RaceStatus] = frozenset({RaceStatus.FINAL, RaceStatus.RECOUNT})


# --------------------------------------------------------------------------- helpers
def allocate_counted(final: np.ndarray, fraction: np.ndarray) -> np.ndarray:
    """Counted votes when a fraction of each unit's ballots has been counted.

    ``counted = floor(f × final)`` element-wise (per line), so ``counted ≤ final`` always and
    ``counted == final`` exactly when ``f == 1``.  ``final`` is (n,) or (n, L) integer.
    """
    f = np.clip(np.asarray(fraction, dtype=np.float64), 0.0, 1.0)
    fin = np.asarray(final)
    if fin.ndim == 2:
        f = f[:, None]
    return np.floor(fin * f).astype(np.int64)


def _gsum(idx: np.ndarray, vals: np.ndarray, n: int) -> np.ndarray:
    """Group sums of (k,) or (k, L) values into ``n`` groups (single bincount)."""
    if vals.ndim == 1:
        return np.bincount(idx, weights=vals, minlength=n)
    L = vals.shape[1]
    flat = (idx[:, None] * L + np.arange(L)).ravel()
    return np.bincount(flat, weights=vals.ravel(), minlength=n * L).reshape(n, L)


def _softmax(logits: np.ndarray) -> np.ndarray:
    z = logits - logits.max(axis=-1, keepdims=True)
    ez = np.exp(z)
    return ez / ez.sum(axis=-1, keepdims=True)


def _finite_nonneg(a: np.ndarray) -> np.ndarray:
    """``a`` as float64 with NaN/±inf and negative entries replaced by 0 (input hygiene)."""
    v = np.asarray(a, dtype=np.float64)
    return np.where(np.isfinite(v) & (v > 0.0), v, 0.0)


def _expected_composition(shares: np.ndarray) -> np.ndarray:
    """Row-normalised expected shares, floored at 1e-9; invalid rows (NaN, negative, all
    zero) become uniform, so a corrupt expectation can never make one line a certain winner."""
    raw = np.asarray(shares, dtype=np.float64)
    if raw.size and raw.min() >= 0.0 and np.isfinite(raw.max()):  # fast path (NaN fails min ≥ 0)
        e = np.maximum(raw, 1e-9)
    else:
        bad = ~(np.isfinite(raw) & (raw >= 0.0)).all(axis=1)
        e = np.maximum(np.where(bad[:, None], 1.0, np.nan_to_num(raw, nan=0.0)), 1e-9)
    return e / e.sum(axis=1, keepdims=True)


def _r(x: float, nd: int = 6) -> float:
    """Round for JSON evidence (non-finite values become 0.0)."""
    x = float(x)
    if not math.isfinite(x):
        return 0.0
    return round(x, nd)


def _kv(keys: Sequence[str], arr: np.ndarray, nd: int = 6) -> dict[str, float]:
    vals = np.asarray(arr, dtype=np.float64)
    if not np.isfinite(vals).all():
        vals = np.where(np.isfinite(vals), vals, 0.0)
    return dict(zip(keys, vals.round(nd).tolist(), strict=True))


def _kv_int(keys: Sequence[str], arr: np.ndarray) -> dict[str, int]:
    return dict(zip(keys, np.asarray(arr, dtype=np.int64).tolist(), strict=True))


# --------------------------------------------------------------------------- analytic bounds
#: Absolute allowance for the numerical error of the analytic probabilities (the quadrature
#: below is accurate to ~1e-12 for every tail df > 2).
BOUNDS_EPS = 2e-6


@lru_cache(maxsize=16)
def _chi2_grid(df: float) -> tuple[np.ndarray, np.ndarray]:
    """Nodes ``u`` and weights of ``E[g(χ²_df)] ≈ Σ w·g(u)``: the trapezoid rule on a uniform grid
    in ``log u`` between the 1e-15 quantiles (spectrally accurate for these smooth integrands
    that decay doubly exponentially at both ends; ~90 nodes for df = 5)."""
    lo = max(-40.0, math.log(max(float(chdtri(df, 1.0 - 1e-15)), 1e-300)))
    hi = math.log(float(chdtri(df, 1e-15)))
    h = min(0.2, 0.7 * math.sqrt(2.0 / df))
    w = lo + h * np.arange(math.ceil((hi - lo) / h) + 1)
    u = np.exp(w)
    logc = -(df / 2.0) * math.log(2.0) - math.lgamma(df / 2.0)
    return u, np.exp(logc + (df / 2.0) * w - u / 2.0) * h


def _tail_le(mean: np.ndarray, s1: np.ndarray, s2: np.ndarray, df: float | None) -> np.ndarray:
    """``P(mean + s·s1·Z1 + s2·Z2 ≤ 0)`` element-wise, ``Z1, Z2 ~ N(0, 1)`` independent and
    ``s = sqrt(df / χ²_df)`` (the Student-t scale of the race swing; ``df=None``: ``s = 1``).
    Degenerate entries (``s1 = s2 = 0``) are 1 when ``mean ≤ 0`` and 0 otherwise."""
    degenerate = (s1 <= 0.0) & (s2 <= 0.0)
    if df is None:
        sd = np.sqrt(s1 * s1 + s2 * s2)
        out = ndtr(-mean / np.where(degenerate, 1.0, sd))
    else:
        u, w = _chi2_grid(float(df))
        den = np.sqrt((df * s1 * s1)[:, None] + u * (s2 * s2)[:, None])
        out = ndtr(-mean[:, None] * np.sqrt(u) / np.where(den > 0.0, den, 1.0)) @ w
    out = np.where(degenerate, (mean <= 0.0).astype(np.float64), out)
    return np.clip(out, 0.0, 1.0)


def _win_bounds(
    mu: np.ndarray,
    swing: np.ndarray,
    noise: np.ndarray,
    counted: np.ndarray,
    df: float | None,
) -> tuple[np.ndarray, np.ndarray]:
    """Exact bounds ``lo ≤ P(line k wins) ≤ hi`` of the projection model's draws.

    A draw is ``T' = max(T, counted)`` with ``T = mu + s·(z₁ @ swing) + z₂ @ noise`` (element-wise
    clip at the counted votes; ``swing``/``noise`` are (·, L) factor matrices with the lines as
    columns, ``s`` the Student-t scale).  Every linear functional ``aᵀT`` is
    ``aᵀmu + s·σ₁Z₁ + σ₂Z₂``, whose tail :func:`_tail_le` evaluates exactly.  Then

    * ``P(T'_j ≥ T'_k) ≤ P(T_j ≥ T_k) + 1[c_j ≥ c_k]·P(T_k ≤ c_j)``, so
      ``P(k loses) ≤ Σ_j`` of these (union bound) → ``lo``;
    * ``P(k wins) ≤ P(T'_k ≥ T'_j) ≤ P(T_k ≥ T_j) + 1[c_k ≥ c_j]·P(T_j ≤ c_k)`` for every rival
      ``j`` → ``hi`` (the minimum over ``j``).

    For races decided between two lines the bounds are nearly equal; :data:`BOUNDS_EPS` widens
    them by (much more than) the quadrature error.
    """
    L = len(mu)
    if L == 1:
        return np.ones(1), np.ones(1)
    iu, ju = np.triu_indices(L, 1)
    # P(T_c ≤ c_j) is needed for every ordered pair with c_j ≥ c_k (k ≠ j)
    kk, jj = np.nonzero((counted[None, :] >= counted[:, None]) & ~np.eye(L, dtype=bool))
    d_sw = swing[:, iu] - swing[:, ju]
    d_nz = noise[:, iu] - noise[:, ju]
    sd1 = np.sqrt((swing * swing).sum(axis=0))
    sd2 = np.sqrt((noise * noise).sum(axis=0))
    P = len(iu)
    probs = _tail_le(
        np.concatenate([mu[iu] - mu[ju], mu[kk] - counted[jj]]),
        np.concatenate([np.sqrt((d_sw * d_sw).sum(axis=0)), sd1[kk]]),
        np.concatenate([np.sqrt((d_nz * d_nz).sum(axis=0)), sd2[kk]]),
        df,
    )
    p_le = probs[:P]  # P(T_k ≤ T_j) for k < j
    le = np.zeros((L, L))  # le[k, j] = P(T_k ≤ T_j)
    le[iu, ju] = p_le
    # P(T_j ≤ T_k) = 1 − P(T_k < T_j) = 1 − P(T_k ≤ T_j) for a continuous difference; an exact
    # tie without uncertainty counts as both "≤" (conservative for both bounds)
    tie = (d_sw == 0.0).all(axis=0) & (d_nz == 0.0).all(axis=0) & (mu[iu] == mu[ju])
    le[ju, iu] = np.where(tie, 1.0, 1.0 - p_le)
    below = np.zeros((L, L))  # below[k, j] = P(T_k ≤ c_j) where c_j ≥ c_k, else 0
    below[kk, jj] = probs[P:]
    off = ~np.eye(L, dtype=bool)
    lo = 1.0 - np.where(off, le + below, 0.0).sum(axis=1)
    hi = np.where(off, le.T + below.T, np.inf).min(axis=1)
    lo = np.clip(lo - BOUNDS_EPS, 0.0, 1.0)
    hi = np.clip(hi + BOUNDS_EPS, 0.0, 1.0)
    return lo, np.maximum(hi, lo)


def _project_into(p: np.ndarray, lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """The probability vector closest to ``p`` in the sense ``clip(p + λ, lo, hi)`` that sums to 1
    (``λ`` solved exactly: the sum is piecewise linear in ``λ``).  Returns ``p`` unchanged when the
    bounds cannot hold a probability vector (numerically inconsistent input)."""
    if lo.sum() > 1.0 + 1e-9 or hi.sum() < 1.0 - 1e-9:
        return p
    bps = np.sort(np.concatenate([lo - p, hi - p]))
    g = np.clip(p[None, :] + bps[:, None], lo, hi).sum(axis=1)
    i = int(np.searchsorted(g, 1.0))
    if i == 0:
        lam = float(bps[0])
    elif i >= len(bps):
        lam = float(bps[-1])
    else:
        g0, g1 = float(g[i - 1]), float(g[i])
        lam = (
            float(bps[i]) if g1 <= g0 else float(bps[i - 1] + (bps[i] - bps[i - 1]) * (1.0 - g0) / (g1 - g0))
        )
    out = np.clip(p + lam, lo, hi)
    s = out.sum()
    return out / s if s > 0 else p


# --------------------------------------------------------------------------- per-race invariants
@dataclass
class _Invariants:
    """Quantities that depend only on a race's expectation, eligible voters and clusters —
    computed once per race when the caller passes a ``RaceProgress.cache`` dict."""

    refs: tuple  # the source arrays (identity check of the cache)
    e: np.ndarray  # (n, L) normalised expected shares
    x: np.ndarray  # (n,) expected ballots (finite, ≥ 0)
    x_total: float
    exp_race: np.ndarray  # (L,) expected race shares
    el: np.ndarray  # (n,) int64 eligible voters
    cl: np.ndarray  # (n,) compact cluster id
    M: int
    #: ``np.add.reduceat`` offsets when the units are grouped by cluster (ids 0..M−1 in order,
    #: every cluster non-empty); None → bincount group sums.
    starts: np.ndarray | None
    n_clusters_arg: int | None = None  # ``RaceProgress.n_clusters`` the ids were built with

    def csum(self, vals: np.ndarray) -> np.ndarray:
        """Per-cluster sums of (n,) or (n, K) values → (M,) / (M, K)."""
        if self.starts is not None:
            return np.add.reduceat(vals, self.starts, axis=0)
        return _gsum(self.cl, vals, self.M)


def _invariants(progress: RaceProgress) -> _Invariants:
    refs = (progress.expected_shares, progress.expected_ballots, progress.eligible, progress.unit_cluster)
    cache = progress.cache
    if cache is not None:
        inv = cache.get("invariants")
        if (
            isinstance(inv, _Invariants)
            and len(inv.refs) == len(refs)
            and all(a is b for a, b in zip(inv.refs, refs, strict=True))
            and inv.n_clusters_arg == progress.n_clusters
            and len(inv.x) == len(progress.reported_fraction)
        ):
            return inv
    n = len(progress.reported_fraction)
    if progress.unit_cluster is None:
        cl = np.arange(n, dtype=np.int64)
        M = n
    elif progress.n_clusters is not None:
        cl = np.asarray(progress.unit_cluster, dtype=np.int64)
        M = int(progress.n_clusters)
    else:
        _, cl = np.unique(progress.unit_cluster, return_inverse=True)
        cl = cl.astype(np.int64)
        M = int(cl.max()) + 1 if n else 0
    e = _expected_composition(progress.expected_shares)
    x = _finite_nonneg(progress.expected_ballots)
    L = e.shape[1]
    exp_race = x @ e
    exp_race = exp_race / exp_race.sum() if exp_race.sum() > 0 else np.full(L, 1.0 / L)
    starts = None
    if n and cl[0] == 0 and int(cl[-1]) == M - 1:
        step = np.diff(cl)
        if ((step == 0) | (step == 1)).all():
            starts = np.flatnonzero(np.r_[True, step == 1])
    inv = _Invariants(
        refs=refs,
        e=e,
        x=x,
        x_total=float(x.sum()),
        exp_race=exp_race,
        el=np.asarray(progress.eligible, dtype=np.int64),
        cl=cl,
        M=M,
        starts=starts,
        n_clusters_arg=progress.n_clusters,
    )
    if cache is not None:
        cache["invariants"] = inv
    return inv


# --------------------------------------------------------------------------- inputs / outputs
@dataclass
class RaceProgress:
    """What the caller may see of a race while it is being counted.

    Arrays are per unit of the race (n units, L ballot lines).  ``counted_votes`` must never
    contain votes of uncounted ballots; ``expected_shares``/``expected_ballots`` are the
    pre-election model expectation (not the result).
    """

    race_key: str
    line_keys: list[str]
    counted_votes: np.ndarray  # (n, L) int64 valid votes counted so far
    reported_fraction: np.ndarray  # (n,) share of each unit's ballots counted (0..1)
    counted_ballots: np.ndarray  # (n,) int64 ballots counted (valid + blank + invalid)
    expected_shares: np.ndarray  # (n, L) float
    expected_ballots: np.ndarray  # (n,) float = expected turnout × eligible
    eligible: np.ndarray  # (n,) int64
    #: (n,) cluster id per unit (municipality index); units of a cluster share local shocks.
    unit_cluster: np.ndarray | None = None
    #: When given, ``unit_cluster`` is already compact (0..n_clusters−1).
    n_clusters: int | None = None
    #: RaceType value (optional; forwarded to the recount check).
    race_type: str | None = None
    #: Optional scratch dict for per-race invariants (normalised expectation, clusters, …).  A
    #: caller that evaluates the same race repeatedly may pass the same dict every time; it is
    #: only reused while ``expected_shares``, ``expected_ballots``, ``eligible`` and
    #: ``unit_cluster`` are the *same array objects* (they must not be modified in place).  The
    #: result of an evaluation never depends on it.
    cache: dict | None = field(default=None, repr=False, compare=False)

    @property
    def n_units(self) -> int:
        return len(self.reported_fraction)

    def validate(self) -> None:
        """Raise :class:`ElectionNightError` when the arrays are inconsistent."""
        n, L = self.counted_votes.shape if self.counted_votes.ndim == 2 else (-1, -1)
        if n < 0 or len(self.line_keys) != L:
            raise ElectionNightError(f"{self.race_key}: counted_votes must be (n, {len(self.line_keys)})")
        for name in ("reported_fraction", "counted_ballots", "expected_ballots", "eligible"):
            if len(getattr(self, name)) != n:
                raise ElectionNightError(f"{self.race_key}: {name} must have length {n}")
        if self.expected_shares.shape != (n, L):
            raise ElectionNightError(f"{self.race_key}: expected_shares must be ({n}, {L})")
        if self.unit_cluster is not None and len(self.unit_cluster) != n:
            raise ElectionNightError(f"{self.race_key}: unit_cluster must have length {n}")
        f = self.reported_fraction
        if np.any((f < 0) | (f > 1)):
            raise ElectionNightError(f"{self.race_key}: reported_fraction outside [0, 1]")
        if np.any(self.counted_votes < 0):
            raise ElectionNightError(f"{self.race_key}: negative counted votes")

    @classmethod
    def from_race_votes(
        cls,
        race: RaceVotes,
        reported_fraction: np.ndarray,
        unit_cluster: np.ndarray | None = None,
        *,
        fallback_turnout: float = 0.78,
    ) -> RaceProgress:
        """Counted view of a final :class:`RaceVotes` at the given per-unit reported fractions
        (``floor(f × final)`` per line, see :func:`allocate_counted`)."""
        f = np.clip(np.asarray(reported_fraction, dtype=np.float64), 0.0, 1.0)
        counted = allocate_counted(race.votes, f)
        counted_ballots = (
            counted.sum(axis=1) + allocate_counted(race.blank, f) + allocate_counted(race.invalid, f)
        )
        n, L = race.votes.shape
        exp_shares = race.expected_shares if race.expected_shares is not None else np.full((n, L), 1.0 / L)
        exp_turnout = (
            race.expected_turnout if race.expected_turnout is not None else np.full(n, fallback_turnout)
        )
        return cls(
            race_key=race.race_key,
            line_keys=list(race.line_keys),
            counted_votes=counted,
            reported_fraction=f,
            counted_ballots=counted_ballots.astype(np.int64),
            expected_shares=np.asarray(exp_shares, dtype=np.float64),
            expected_ballots=np.asarray(exp_turnout, dtype=np.float64) * race.eligible,
            eligible=np.asarray(race.eligible, dtype=np.int64),
            unit_cluster=None if unit_cluster is None else np.asarray(unit_cluster),
        )


@dataclass(frozen=True)
class CallState:
    """The published state of a race carried between evaluations (stickiness)."""

    status: RaceStatus
    key: str | None = None  # line the status refers to (called / projected / lean / final)
    seq: int = 0
    is_manual: bool = False

    @property
    def locked(self) -> bool:
        return self.status in LOCKED_STATUSES


@dataclass(frozen=True)
class RecountInput:
    """Final tally handed to an injected recount check."""

    race_key: str
    line_keys: list[str]
    totals: np.ndarray  # (L,) final valid votes
    valid: int
    ballots: int
    winner_key: str | None
    runner_up_key: str | None
    margin_votes: int
    margin_pct: float  # percentage points of valid votes
    race_type: str | None = None  # RaceType value when known (the night engine sets it)


#: ``recount_check(RecountInput) -> bool | (bool, reason)`` — True when an automatic recount is
#: required (e.g. a wrapper around ``app.elections.recount.needs_recount``).
RecountCheck = Callable[[RecountInput], "bool | tuple[bool, str]"]


@dataclass
class CallDecision:
    """Result of one evaluation of a race.

    ``evidence`` (JSON-serialisable, everything the decision used) is materialised lazily on
    first access, so evaluations whose evidence is never stored stay cheap.
    """

    race_key: str
    seq: int
    status: RaceStatus
    leader_key: str | None  # most counted votes so far (None before any vote)
    winner_key: str | None  # line the status refers to (called/projected/lean/final winner)
    win_probability: dict[str, float]
    projected_share_mean: dict[str, float]
    projected_share_p05: dict[str, float]
    projected_share_p95: dict[str, float]
    projected_votes: dict[str, float]
    counted_votes: dict[str, int]
    counted_valid: int
    counted_ballots: int
    outstanding_ballots_est: float
    outstanding_ballots_upper: int
    expected_ballots: float
    reporting_pct: float  # 0–100, by expected ballots
    units_total: int
    units_reported: int
    units_partial: int
    margin_votes: int  # counted leader − runner-up
    margin_pct: float  # percentage points of counted valid votes
    projected_margin_pct: float  # expected final margin, top two projected lines
    comeback: dict
    math_certain: bool
    basis: str
    retracted: bool
    n_draws: int
    _evidence_fn: Callable[[], dict] | None = field(default=None, repr=False, compare=False)
    _evidence: dict | None = field(default=None, repr=False, compare=False)

    @property
    def evidence(self) -> dict:
        """The evidence snapshot (see ``docs/RACE_CALLING.md`` §5)."""
        if self._evidence is None:
            fn = self._evidence_fn
            self._evidence = fn() if fn is not None else {}
            self._evidence_fn = None
        return self._evidence

    @property
    def is_decided(self) -> bool:
        return self.status in DECIDED_STATUSES

    def state(self) -> CallState:
        """The state to pass as ``prior`` to the next evaluation."""
        return CallState(
            status=self.status, key=self.winner_key, seq=self.seq, is_manual=self.basis == "manual"
        )

    def win_probability_of(self, key: str | None) -> float | None:
        return None if key is None else self.win_probability.get(key)

    def to_dict(self, include_evidence: bool = True) -> dict:
        d = {
            "race_key": self.race_key,
            "seq": self.seq,
            "status": self.status.value,
            "leader_key": self.leader_key,
            "winner_key": self.winner_key,
            "win_probability": self.win_probability,
            "projected_share_mean": self.projected_share_mean,
            "projected_share_p05": self.projected_share_p05,
            "projected_share_p95": self.projected_share_p95,
            "projected_votes": self.projected_votes,
            "counted_votes": self.counted_votes,
            "counted_valid": self.counted_valid,
            "counted_ballots": self.counted_ballots,
            "outstanding_ballots_est": _r(self.outstanding_ballots_est, 1),
            "outstanding_ballots_upper": self.outstanding_ballots_upper,
            "expected_ballots": _r(self.expected_ballots, 1),
            "reporting_pct": _r(self.reporting_pct, 3),
            "units_total": self.units_total,
            "units_reported": self.units_reported,
            "units_partial": self.units_partial,
            "margin_votes": self.margin_votes,
            "margin_pct": _r(self.margin_pct, 4),
            "projected_margin_pct": _r(self.projected_margin_pct, 4),
            "comeback": self.comeback,
            "math_certain": self.math_certain,
            "basis": self.basis,
            "retracted": self.retracted,
            "n_draws": self.n_draws,
        }
        if include_evidence:
            d["evidence"] = self.evidence
        return d


# --------------------------------------------------------------------------- the caller
@dataclass
class _Model:
    """Raw output of the projection model (formatted lazily into evidence)."""

    mu: np.ndarray  # (L,) mean final valid votes
    prob: np.ndarray  # (L,) win probabilities (Monte Carlo, projected into the bounds if any)
    prob_mc: np.ndarray  # (L,) share of draws won
    bounds: tuple[np.ndarray, np.ndarray] | None  # analytic (lo, hi) when they were needed
    n_draws: int
    q05: np.ndarray  # (L,) 5th percentile of final shares
    q95: np.ndarray  # (L,) 95th percentile of final shares
    margin_sd: float  # SD of the final margin between the two projected leaders (votes)
    gain_q995: float  # 99.5th percentile of the counted runner-up's net gain on the outstanding vote
    clusters_total: int
    clusters_reporting: int
    clusters_outstanding: int
    exp_race: np.ndarray  # (L,) expected race shares
    expected_ballots: float
    tau_raw: float
    tau: float
    k_tau: float
    sd_tau: float
    valid_rate: float
    delta_raw: np.ndarray
    delta_hat: np.ndarray
    k_sw: np.ndarray
    v_post: np.ndarray
    sd_sw: np.ndarray
    outstanding_ballots: float
    unreported_expected_ballots: float
    partial_remaining_valid: float


class RaceCaller:
    """Evaluates races from partial counts (pure; deterministic given ``seed`` and ``seq``)."""

    def __init__(self, config: NightConfig | None = None, recount_check: RecountCheck | None = None) -> None:
        self.config = config or load_night_config()
        self.recount_check = recount_check

    # ------------------------------------------------------------------ public API
    def evaluate(
        self,
        progress: RaceProgress,
        seed: int,
        seq: int,
        prior: CallState | None = None,
    ) -> CallDecision:
        """Decide the status of a race after reporting event ``seq``.

        Args:
            progress: the counted state of the race (see :class:`RaceProgress`).
            seed: root seed of the night; draws use ``make_rng(seed, "call", race_key, seq)``.
            seq: reporting-event sequence number (0 = polls just closed).
            prior: the previously published state (sticky calls, manual overrides).

        Returns:
            The :class:`CallDecision` with status, probabilities, projections and evidence.
        """
        keys = list(progress.line_keys)
        L = len(keys)
        if L == 0:
            raise ElectionNightError(f"{progress.race_key}: race has no ballot lines")
        counted = np.asarray(progress.counted_votes, dtype=np.int64)
        f = np.asarray(progress.reported_fraction, dtype=np.float64)
        if not (f.size == 0 or (f.min() >= 0.0 and f.max() <= 1.0)):  # NaN fails both tests
            f = np.clip(np.where(np.isfinite(f), f, 0.0), 0.0, 1.0)
        cb = np.asarray(progress.counted_ballots, dtype=np.int64)
        # per-race invariants: cached across evaluations when the caller passes a cache dict
        inv = _invariants(progress) if progress.cache is not None else None
        if inv is not None:
            x, el, X_tot = inv.x, inv.el, inv.x_total
        else:
            x = _finite_nonneg(progress.expected_ballots)
            el = np.asarray(progress.eligible, dtype=np.int64)
            X_tot = float(x.sum())
        n = len(f)

        tot = counted.sum(axis=0)
        C = int(tot.sum())
        cb_tot = int(cb.sum())
        if X_tot > 0:
            rshare = float(f @ x) / X_tot
        elif el.sum() > 0:
            rshare = float(f @ el) / float(el.sum())
        else:
            rshare = float(f.mean()) if n else 1.0
        rshare = min(max(rshare, 0.0), 1.0)  # a dot product and a sum may differ by an ulp
        units_reported = int(np.count_nonzero(f >= 1.0))
        units_partial = int(np.count_nonzero(f > 0)) - units_reported
        has_results = units_reported + units_partial > 0
        all_reported = units_reported == n

        order = np.argsort(-tot, kind="stable")
        lead_i = int(order[0])
        second_i = int(order[1]) if L > 1 else None
        leader_key = keys[lead_i] if C > 0 else None
        margin_votes = int(tot[lead_i] - (tot[second_i] if second_i is not None else 0)) if C > 0 else 0
        margin_pct = 100.0 * margin_votes / C if C > 0 else 0.0
        outstanding_upper = int(np.maximum(el - cb, 0) @ (f < 1.0))
        common = {
            "race_key": progress.race_key,
            "seq": seq,
            "leader_key": leader_key,
            "counted_votes": _kv_int(keys, tot),
            "counted_valid": C,
            "counted_ballots": cb_tot,
            "outstanding_ballots_upper": outstanding_upper,
            "expected_ballots": X_tot,
            "reporting_pct": 100.0 * rshare,
            "units_total": n,
            "units_reported": units_reported,
            "units_partial": units_partial,
            "margin_votes": margin_votes,
            "margin_pct": margin_pct,
        }
        prior_ev = (
            None
            if prior is None
            else {
                "status": prior.status.value,
                "key": prior.key,
                "seq": prior.seq,
                "is_manual": prior.is_manual,
            }
        )
        if all_reported:
            return self._final(progress, keys, tot, C, cb_tot, order, common, prior_ev)

        cfg = self.config.calling
        uncontested = L == 1
        math_certain = has_results and C > 0 and (uncontested or margin_votes > outstanding_upper)
        prior_i = keys.index(prior.key) if prior is not None and prior.key in keys else None
        targets = self._refine_targets(has_results, rshare, prior, prior_i)
        if inv is None:
            inv = _invariants(progress)
        m = self._project(progress, inv, keys, counted, f, cb, tot, C, seed, seq, targets, lead_i, second_i)
        prob = m.prob
        mu = m.mu
        mu_tot = float(mu.sum())
        mu_order = np.argsort(-mu, kind="stable")
        proj_margin = (
            100.0 * float(mu[mu_order[0]] - (mu[mu_order[1]] if L > 1 else 0.0)) / mu_tot
            if mu_tot > 0
            else 0.0
        )
        top_i = int(np.argmax(prob))
        status, key, basis, retracted, retracted_key = self._decide(
            has_results=has_results,
            rshare=rshare,
            prob=prob,
            keys=keys,
            top_i=top_i,
            lead_i=lead_i,
            math_certain=math_certain,
            uncontested=uncontested,
            proj_margin=proj_margin,
            prior=prior,
            prior_i=prior_i,
        )
        win_prob = (
            {k: (1.0 if i == lead_i else 0.0) for i, k in enumerate(keys)}
            if math_certain
            else _kv(keys, prob)
        )
        share_mean = mu / mu_tot if mu_tot > 0 else np.full(L, 1.0 / L)
        outstanding_valid = max(mu_tot - C, 0.0)
        comeback = self._comeback(
            keys, tot, m.gain_q995, lead_i, second_i, outstanding_valid, outstanding_upper, C
        )
        mean_d, p05_d, p95_d = _kv(keys, share_mean), _kv(keys, m.q05), _kv(keys, m.q95)
        votes_d = _kv(keys, mu, 1)
        at_risk = (
            basis == "manual"
            and key in keys
            and float(prob[keys.index(key)]) < cfg.retraction_threshold
            and not math_certain
        )

        def _evidence() -> dict:
            ev = {
                "method": METHOD_VERSION,
                "data_category": "SIMULATED",
                "race_key": progress.race_key,
                "seq": seq,
                "rng_stream": ["call", progress.race_key, seq],
                "basis": basis,
                "status": status.value,
                "key": key,
                "prior": prior_ev,
                "retracted": retracted,
                "retracted_key": retracted_key,
                "reporting": {
                    "pct_expected_ballots": _r(100.0 * rshare, 4),
                    "units_total": n,
                    "units_reported": units_reported,
                    "units_partial": units_partial,
                    "clusters_total": m.clusters_total,
                    "clusters_reporting": m.clusters_reporting,
                    "clusters_outstanding": m.clusters_outstanding,
                },
                "counted": {"votes": common["counted_votes"], "valid": C, "ballots": cb_tot},
                "leader": {"key": leader_key, "margin_votes": margin_votes, "margin_pct": _r(margin_pct, 4)},
                "expected": {"shares": _kv(keys, m.exp_race), "ballots_total": _r(m.expected_ballots, 1)},
                "turnout": {
                    "ratio_raw": _r(m.tau_raw),
                    "ratio": _r(m.tau),
                    "shrink": _r(m.k_tau),
                    "sd_log": _r(m.sd_tau),
                },
                "valid_rate": _r(m.valid_rate),
                "swing": {
                    "space": "log-share",
                    "raw": _kv(keys, m.delta_raw),
                    "estimate": _kv(keys, m.delta_hat),
                    "shrink": _kv(keys, m.k_sw),
                    "posterior_sd": _kv(keys, np.sqrt(m.v_post)),
                    "sd_used": _kv(keys, m.sd_sw),
                    "differential_sd": self.config.calling.model.differential_sd,
                    "tail_df": self.config.calling.model.swing_tail_df,
                },
                "outstanding": {
                    "ballots_est": _r(m.outstanding_ballots, 1),
                    "unreported_expected_ballots": _r(m.unreported_expected_ballots, 1),
                    "partial_remaining_valid": _r(m.partial_remaining_valid, 1),
                    "ballots_upper_bound": outstanding_upper,
                    "valid_est": _r(outstanding_valid, 1),
                },
                "projection": {
                    "votes_mean": votes_d,
                    "share_mean": mean_d,
                    "share_p05": p05_d,
                    "share_p95": p95_d,
                    "margin_pct_mean": _r(proj_margin, 4),
                    "margin_sd_votes": _r(m.margin_sd, 1),
                },
                "win_probability": win_prob,
                "model_win_probability": _kv(keys, prob),
                "n_draws": m.n_draws,
                "math_certain": math_certain,
                "comeback": comeback,
                "thresholds": self._thresholds(),
            }
            if basis == "manual":
                ev["manual_call_at_risk"] = at_risk
            return ev

        return CallDecision(
            status=status,
            winner_key=key,
            win_probability=win_prob,
            projected_share_mean=mean_d,
            projected_share_p05=p05_d,
            projected_share_p95=p95_d,
            projected_votes=votes_d,
            outstanding_ballots_est=m.outstanding_ballots,
            projected_margin_pct=proj_margin,
            comeback=comeback,
            math_certain=math_certain,
            basis=basis,
            retracted=retracted,
            n_draws=m.n_draws,
            _evidence_fn=_evidence,
            **common,
        )

    # ------------------------------------------------------------------ state machine
    def _refine_targets(
        self, has_results: bool, rshare: float, prior: CallState | None, prior_i: int | None
    ) -> list[tuple[int | None, float]]:
        """Thresholds whose crossing could change the status now: ``(line index | None=top, t)``.

        Only these trigger extra Monte-Carlo draws (adaptive ``n_draws``)."""
        cfg = self.config.calling
        if prior is not None and prior.is_manual and not prior.locked:
            return []
        sticky = prior is not None and prior.status in CALL_STATUSES and prior_i is not None
        if not has_results:
            if not cfg.call_at_poll_close:
                return []
            if sticky:
                return [(prior_i, cfg.retraction_threshold)]
            return [(None, cfg.projected_threshold), (None, cfg.called_threshold)]
        if sticky:
            targets: list[tuple[int | None, float]] = [(prior_i, cfg.retraction_threshold)]
            if (
                prior is not None
                and prior.status == RaceStatus.PROJECTED
                and cfg.min_reporting_call <= rshare
            ):
                targets.append((prior_i, cfg.called_threshold))
            return targets
        targets = [(None, cfg.lean_threshold)]
        if prior is not None and prior.status == RaceStatus.LEAN and cfg.lean_hysteresis > 0:
            targets.append((None, cfg.lean_threshold - cfg.lean_hysteresis))
        if cfg.min_reporting_projection <= rshare:
            targets.append((None, cfg.projected_threshold))
        if cfg.min_reporting_call <= rshare:
            targets.append((None, cfg.called_threshold))
        return targets

    def _decide(
        self,
        *,
        has_results: bool,
        rshare: float,
        prob: np.ndarray,
        keys: list[str],
        top_i: int,
        lead_i: int,
        math_certain: bool,
        uncontested: bool,
        proj_margin: float,
        prior: CallState | None,
        prior_i: int | None,
    ) -> tuple[RaceStatus, str | None, str, bool, str | None]:
        """The race-status state machine → ``(status, key, basis, retracted, retracted_key)``."""
        cfg = self.config.calling
        p_top = float(prob[top_i])
        top = keys[top_i]
        if prior is not None and prior.is_manual and not prior.locked:
            return prior.status, prior.key, "manual", False, None
        if prior is not None and prior.status in CALL_STATUSES and prior_i is not None:
            p_k = float(prob[prior_i])
            if not has_results:
                # only a poll-close call (or a cleared manual call) can precede any result: keep
                # it only when poll-close calls are allowed and the expectation still supports it
                if cfg.call_at_poll_close and p_k >= cfg.retraction_threshold:
                    return prior.status, prior.key, "poll_close", False, None
                return RaceStatus.POLLS_CLOSED, None, "retraction", True, prior.key
            certain_k = math_certain and prior_i == lead_i
            if p_k < cfg.retraction_threshold and not certain_k:
                return RaceStatus.TOO_CLOSE, None, "retraction", True, prior.key
            if prior.status == RaceStatus.PROJECTED:
                if p_k >= cfg.called_threshold and cfg.min_reporting_call <= rshare:
                    return RaceStatus.CALLED, prior.key, "model", False, None
                if certain_k:
                    return RaceStatus.CALLED, prior.key, "mathematical", False, None
            return prior.status, prior.key, "sticky", False, None
        if not has_results:
            if cfg.call_at_poll_close:
                if uncontested or p_top >= cfg.called_threshold:
                    return RaceStatus.CALLED, top, "poll_close", False, None
                if p_top >= cfg.projected_threshold:
                    return RaceStatus.PROJECTED, top, "poll_close", False, None
            return RaceStatus.POLLS_CLOSED, None, "no_results", False, None
        if math_certain:
            return (
                RaceStatus.CALLED,
                keys[lead_i],
                ("uncontested" if uncontested else "mathematical"),
                False,
                None,
            )
        if p_top >= cfg.called_threshold and cfg.min_reporting_call <= rshare:
            return RaceStatus.CALLED, top, "model", False, None
        if p_top >= cfg.projected_threshold and cfg.min_reporting_projection <= rshare:
            return RaceStatus.PROJECTED, top, "model", False, None
        if cfg.min_reporting_projection <= rshare and abs(proj_margin) < cfg.close_band_pct:
            return RaceStatus.TOO_CLOSE, None, "close_band", False, None
        lean_floor = cfg.lean_threshold
        if prior is not None and prior.status == RaceStatus.LEAN and prior.key == top:
            lean_floor -= cfg.lean_hysteresis
        if p_top >= lean_floor:
            return RaceStatus.LEAN, top, "model", False, None
        if cfg.too_close_min_reporting <= rshare or (
            prior is not None and prior.status == RaceStatus.TOO_CLOSE
        ):
            return RaceStatus.TOO_CLOSE, None, "model", False, None
        return RaceStatus.TOO_EARLY, None, "model", False, None

    # ------------------------------------------------------------------ final / recount
    def _final(
        self,
        progress: RaceProgress,
        keys: list[str],
        tot: np.ndarray,
        C: int,
        cb_tot: int,
        order: np.ndarray,
        common: dict,
        prior_ev: dict | None,
    ) -> CallDecision:
        L = len(keys)
        top = int(order[0])
        second = int(order[1]) if L > 1 else None
        tied_lines = [i for i in range(L) if tot[i] == tot[top]]
        tied = C == 0 or len(tied_lines) > 1
        margin_votes = int(tot[top] - (tot[second] if second is not None else 0))
        margin_pct = 100.0 * margin_votes / C if C > 0 else 0.0
        rin = RecountInput(
            race_key=progress.race_key,
            line_keys=keys,
            totals=tot.copy(),
            valid=C,
            ballots=cb_tot,
            winner_key=keys[top] if C > 0 else None,
            runner_up_key=keys[second] if second is not None else None,
            margin_votes=margin_votes,
            margin_pct=margin_pct,
            race_type=progress.race_type,
        )
        if tied:
            recount, reason = True, "tie"
        elif L == 1:
            recount, reason = False, "uncontested"
        elif self.recount_check is not None:
            res = self.recount_check(rin)
            if isinstance(res, tuple):
                recount, reason = bool(res[0]), f"recount_check: {res[1]}"
            else:
                recount, reason = bool(res), "recount_check"
        else:
            recount, reason = margin_pct <= self.config.recount.margin_pct, "fallback_margin"
        status = RaceStatus.RECOUNT if recount else RaceStatus.FINAL
        if tied and C > 0:
            win_prob = {k: (1.0 / len(tied_lines) if i in tied_lines else 0.0) for i, k in enumerate(keys)}
        else:
            win_prob = {k: (1.0 if (i == top and C > 0) else 0.0) for i, k in enumerate(keys)}
        shares = tot / C if C > 0 else np.zeros(L)
        key = keys[top] if C > 0 and not tied else None
        basis = "recount" if recount else "final"
        fallback = self.config.recount.margin_pct

        def _evidence() -> dict:
            return {
                "method": METHOD_VERSION,
                "data_category": "SIMULATED",
                "race_key": progress.race_key,
                "seq": common["seq"],
                "basis": basis,
                "status": status.value,
                "key": key,
                "prior": prior_ev,
                "reporting": {"pct_expected_ballots": 100.0, "units_total": common["units_total"]},
                "counted": {"votes": common["counted_votes"], "valid": C, "ballots": cb_tot},
                "final": {
                    "winner_key": key,
                    "leader_key": rin.winner_key,
                    "runner_up_key": rin.runner_up_key,
                    "margin_votes": margin_votes,
                    "margin_pct": _r(margin_pct, 5),
                    "tied": tied,
                    "tied_keys": [keys[i] for i in tied_lines] if tied else [],
                },
                "recount": {"required": recount, "reason": reason, "fallback_margin_pct": fallback},
                "win_probability": win_prob,
                "math_certain": not tied,
            }

        return CallDecision(
            status=status,
            winner_key=key,
            win_probability=win_prob,
            projected_share_mean=_kv(keys, shares),
            projected_share_p05=_kv(keys, shares),
            projected_share_p95=_kv(keys, shares),
            projected_votes=_kv(keys, tot, 1),
            outstanding_ballots_est=0.0,
            projected_margin_pct=margin_pct,
            comeback={},
            math_certain=not tied,
            basis=basis,
            retracted=False,
            n_draws=0,
            _evidence_fn=_evidence,
            **common,
        )

    # ------------------------------------------------------------------ projection model
    def _project(
        self,
        progress: RaceProgress,
        inv: _Invariants,
        keys: list[str],
        counted: np.ndarray,
        f: np.ndarray,
        cb: np.ndarray,
        tot: np.ndarray,
        C: int,
        seed: int,
        seq: int,
        targets: list[tuple[int | None, float]],
        lead_i: int,
        second_i: int | None,
    ) -> _Model:
        mc = self.config.calling.model
        L = len(keys)
        n = len(f)
        a = mc.pseudo_votes
        tot_f = tot.astype(np.float64)
        e, x, M = inv.e, inv.x, inv.M

        cv = counted.sum(axis=1).astype(np.float64)
        cb_tot = float(cb.sum())
        vr = float(C / cb_tot) if cb_tot > 0 and C > 0 else mc.valid_rate_prior
        vr = min(max(vr, 0.5), 1.0)

        # --- per-unit quantities, summed per cluster in a single pass (one reduceat / bincount)
        fx = f * x
        has = cv > 0
        unrep_w = np.where(has, 0.0, x * (1.0 - f))  # expected ballots not yet counted
        part = has & (f < 1.0)
        any_part = bool(part.any())
        c0 = 3 * L
        K = c0 + 4 + (L + 2 if any_part else 0)
        W = np.empty((n, K))
        W[:, :L] = counted
        np.multiply(cv[:, None], e, out=W[:, L : 2 * L])
        np.multiply(unrep_w[:, None], e, out=W[:, 2 * L : c0])
        W[:, c0] = fx
        W[:, c0 + 1] = cv * cv
        W[:, c0 + 2] = unrep_w
        W[:, c0 + 3] = unrep_w * unrep_w
        if any_part:
            rem_scale = np.where(part, (1.0 - f) / np.maximum(f, 1e-12), 0.0)
            rem_valid = cv * rem_scale
            np.multiply(counted, rem_scale[:, None], out=W[:, c0 + 4 : c0 + 4 + L])
            W[:, c0 + 4 + L] = rem_valid
            W[:, c0 + 5 + L] = rem_valid * rem_valid
        S = inv.csum(W)

        # --- turnout ratio (log, shrunk toward 1)
        sp2 = mc.turnout_prior_sd**2
        xr = float(fx.sum())
        if xr > 0 and cb_tot > 0:
            log_tau_raw = math.log(cb_tot / xr)
            w_cl = S[:, c0]
            H_t = float((w_cl @ w_cl) / max(w_cl.sum(), 1e-12) ** 2)
            v_tau = mc.turnout_cluster_sd**2 * H_t + 1.0 / max(cb_tot, 1.0)
            k_tau = sp2 / (sp2 + v_tau)
            log_tau = k_tau * log_tau_raw
            var_tau = k_tau * v_tau
        else:
            log_tau_raw, k_tau, log_tau, var_tau = 0.0, 0.0, 0.0, sp2
        tau = math.exp(log_tau)

        # --- log-linear swing fitted on reporting clusters, shrunk toward 0
        s2 = mc.swing_prior_sd**2
        c2 = mc.cluster_sd**2
        u2 = mc.unit_sd**2
        rho = np.zeros((M, L))
        var_rho = np.full((M, L), c2)
        n_clusters_rep = 0
        if C > 0:
            obs_m = S[:, :L]
            C_m = obs_m.sum(axis=1)
            Ew = S[:, L : 2 * L]
            rc = C_m > 0
            n_clusters_rep = int(rc.sum())
            Cm = C_m[rc]
            logE = np.log(np.maximum(Ew[rc] / Cm[:, None], 1e-12))
            delta = np.zeros(L)
            for _ in range(mc.fit_iterations):
                pred = Cm @ _softmax(logE + delta)
                step = np.log((tot_f + a) / (pred + a))
                delta += step - step.mean()
                if np.abs(step).max() < 1e-6:
                    break
            delta_raw = delta
            p_obs = np.maximum(tot_f / C, 1e-3)
            H_c = float((Cm @ Cm) / Cm.sum() ** 2)
            H_u = float((cv @ cv) / float(C) ** 2)
            v_data = c2 * H_c + u2 * H_u + 1.0 / (C * p_obs)
            k_sw = s2 / (s2 + v_data)
            delta_hat = k_sw * delta_raw
            v_post = k_sw * v_data
            # per-cluster residuals (shrunk) — they inform the cluster's uncounted remainder
            P_rep = _softmax(logE + delta_hat)
            rho_raw = np.log((obs_m[rc] + a) / (Cm[:, None] * P_rep + a))
            H_m = S[rc, c0 + 1] / (Cm * Cm)
            v_m = u2 * H_m[:, None] + 1.0 / (Cm[:, None] * np.maximum(P_rep, 1e-3) + 1.0)
            k_m = c2 / (c2 + v_m) if c2 > 0 else np.zeros_like(v_m)
            rho[rc] = k_m * rho_raw
            var_rho[rc] = c2 * (1.0 - k_m)
        else:
            delta_raw = np.zeros(L)
            k_sw = np.zeros(L)
            delta_hat = np.zeros(L)
            v_post = np.full(L, s2)

        # --- outstanding vote per cluster: unreported units and partial remainders
        XU = S[:, c0 + 2]
        out_cl = XU > 0
        XUo = XU[out_cl]
        eU = S[out_cl, 2 * L : c0] / XUo[:, None]
        H_out = S[out_cl, c0 + 3] / (XUo * XUo)
        V = tau * vr * XUo  # expected valid votes outstanding per cluster
        Pm = _softmax(np.log(np.maximum(eU, 1e-12)) + delta_hat + rho[out_cl])
        a_vec = V @ Pm
        mu = tot_f + a_vec
        Sig = np.zeros((L, L))
        if any_part:
            XP = S[:, c0 + 4 + L]
            pc = XP > 0
            comp_p = S[pc, c0 + 4 : c0 + 4 + L]
            XPp = XP[pc]
            oP = comp_p / XPp[:, None]
            mu = mu + comp_p.sum(axis=0)
            HP = S[pc, c0 + 5 + L] / (XPp * XPp)
            WP = XPp * XPp * mc.partial_sd**2 * HP
            QP = oP * oP * WP[:, None]
            rP = (oP * oP).sum(axis=1) * WP
            Sig += np.diag(QP.sum(axis=0)) - QP.T @ oP - oP.T @ QP + (oP * rP[:, None]).T @ oP
            partial_ballots = float(cb @ rem_scale)
            partial_valid = float(rem_valid.sum())
        else:
            partial_ballots = partial_valid = 0.0

        # --- linearised covariance: swing (common), turnout (common), cluster + unit noise
        G = np.diag(a_vec) - (Pm * V[:, None]).T @ Pm  # ∂ votes / ∂ swing
        sd_sw = np.sqrt(v_post + mc.differential_sd**2)
        s2m = var_rho[out_cl] + u2 * H_out[:, None]
        Wv = V * V
        Q = Pm * Pm * s2m
        QW = Q * Wv[:, None]
        r = Q.sum(axis=1) * Wv
        Sig += np.diag(QW.sum(axis=0)) - QW.T @ Pm - Pm.T @ QW + (Pm * r[:, None]).T @ Pm
        Sig += mc.turnout_cluster_sd**2 * (Pm * Wv[:, None]).T @ Pm
        Sig = 0.5 * (Sig + Sig.T)
        lam, vec = np.linalg.eigh(Sig)
        BcT = (vec * np.sqrt(np.clip(lam, 0.0, None))[None, :]).T
        sd_tau = math.sqrt(var_tau)
        GS = (G * sd_sw[None, :]).T  # z_sw @ GS  = G · (sd ⊙ z)
        tvec = a_vec * sd_tau

        # --- Monte-Carlo with adaptive draw count
        cfg = self.config.calling
        rng = make_rng(seed, "call", progress.race_key, seq)
        df = mc.swing_tail_df

        def _draw(D: int) -> np.ndarray:
            z_sw = rng.standard_normal((D, L))
            if df is not None:
                z_sw *= np.sqrt(df / rng.chisquare(df, size=D))[:, None]
            z_t = rng.standard_normal(D)
            z_c = rng.standard_normal((D, L))
            T = z_sw @ GS
            T += z_c @ BcT
            T += z_t[:, None] * tvec
            T += mu
            return np.maximum(T, tot_f, out=T)

        def _near(wins: np.ndarray, D: int) -> bool:
            """Some relevant threshold lies within the Monte-Carlo error of its estimate."""
            probs = wins / D
            for i, t in targets:
                p = float(probs.max()) if i is None else float(probs[i])
                if abs(p - t) < 3.0 * math.sqrt(t * (1.0 - t) / D) + 1.0 / D:
                    return True
            return False

        def _confirm(wins: np.ndarray, D: int) -> bool:
            """A high threshold (≥ projected) is currently met and must be confirmed."""
            probs = wins / D
            for i, t in targets:
                if t < cfg.projected_threshold:
                    continue
                p = float(probs.max()) if i is None else float(probs[i])
                if p >= t and p - t < 3.0 * math.sqrt(t * (1.0 - t) / D) + 1.0 / D:
                    return True
            return False

        def _wins(block: np.ndarray) -> np.ndarray:
            """Win counts; a non-finite draw (defensive: never expected) is won by nobody, so
            numerical trouble can only lower win probabilities, never produce a call."""
            ok = np.isfinite(block).all(axis=1)
            if not ok.all():
                log.warning("%s: %d non-finite projection draws ignored", progress.race_key, int((~ok).sum()))
                block = block[ok]
            return np.bincount(np.argmax(block, axis=1), minlength=L) if len(block) else np.zeros(L, np.int64)

        draws = _draw(cfg.n_draws_min)
        wins = _wins(draws)
        bounds: tuple[np.ndarray, np.ndarray] | None = None
        settled = False
        for target, need in ((cfg.n_draws, _near), (cfg.n_draws_max, _confirm)):
            D = draws.shape[0]
            if settled or target <= D or not need(wins, D):
                continue
            if bounds is None:
                # a threshold is within the Monte-Carlo error: the exact bounds of the same
                # model often decide it already, and then no further draw is needed
                bounds = _win_bounds(mu, GS, np.vstack([BcT, tvec[None, :]]), tot_f, df)
                if all(np.isfinite(b).all() for b in bounds):
                    settled = self._settled(targets, *bounds)
                else:  # defensive: never expected
                    bounds = None
                if settled:
                    continue
            extra = _draw(target - D)
            draws = np.vstack([draws, extra])
            wins = wins + _wins(extra)

        D = draws.shape[0]
        prob_mc = wins / D
        prob = prob_mc if bounds is None else _project_into(prob_mc, *bounds)
        k05, k95 = round(0.05 * (D - 1)), round(0.95 * (D - 1))
        shares = np.partition(draws / np.maximum(draws.sum(axis=1, keepdims=True), 1e-9), [k05, k95], axis=0)
        mo = np.argsort(-mu, kind="stable")
        margin_sd = float((draws[:, mo[0]] - draws[:, mo[1]]).std()) if L > 1 else 0.0
        if second_i is not None:
            gain = (draws[:, second_i] - draws[:, lead_i]) - float(tot_f[second_i] - tot_f[lead_i])
            kq = int(0.995 * (D - 1))
            gain_q995 = float(np.partition(gain, kq)[kq])
        else:
            gain_q995 = 0.0
        XU_tot = float(XU.sum())
        return _Model(
            mu=mu,
            prob=prob,
            prob_mc=prob_mc,
            bounds=bounds,
            n_draws=D,
            q05=shares[k05],
            q95=shares[k95],
            margin_sd=margin_sd,
            gain_q995=gain_q995,
            clusters_total=M,
            clusters_reporting=n_clusters_rep,
            clusters_outstanding=int(out_cl.sum()),
            exp_race=inv.exp_race,
            expected_ballots=inv.x_total,
            tau_raw=math.exp(log_tau_raw),
            tau=tau,
            k_tau=k_tau,
            sd_tau=sd_tau,
            valid_rate=vr,
            delta_raw=delta_raw,
            delta_hat=delta_hat,
            k_sw=k_sw,
            v_post=v_post,
            sd_sw=sd_sw,
            outstanding_ballots=tau * XU_tot + partial_ballots,
            unreported_expected_ballots=XU_tot,
            partial_remaining_valid=partial_valid,
        )

    @staticmethod
    def _settled(targets: list[tuple[int | None, float]], lo: np.ndarray, hi: np.ndarray) -> bool:
        """True when the bounds put every relevant threshold on a known side: the status the
        state machine reaches no longer depends on Monte-Carlo noise."""
        for i, t in targets:
            if i is None:
                if not (float(lo.max()) >= t or float(hi.max()) < t):
                    return False
            elif not (float(lo[i]) >= t or float(hi[i]) < t):
                return False
        return True

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _comeback(
        keys: list[str],
        tot: np.ndarray,
        gain_q995: float,
        lead_i: int,
        second_i: int | None,
        outstanding_valid: float,
        outstanding_upper: int,
        C: int,
    ) -> dict:
        """What the counted runner-up would need from the outstanding vote to overtake."""
        if second_i is None or C == 0:
            return {}
        deficit = int(tot[lead_i] - tot[second_i])
        if outstanding_valid > 0:
            required: float | None = 100.0 * deficit / outstanding_valid
            plausible = 100.0 * gain_q995 / outstanding_valid
        else:
            required = None if deficit > 0 else 0.0
            plausible = 0.0
        return {
            "leader_key": keys[lead_i],
            "trailer_key": keys[second_i],
            "deficit_votes": deficit,
            "outstanding_valid_est": _r(outstanding_valid, 1),
            "outstanding_ballots_upper": outstanding_upper,
            "required_net_margin_pct": None if required is None else _r(required, 4),
            "plausible_max_net_margin_pct": _r(plausible, 4),
            "plausible": bool(required is not None and plausible >= required),
            "mathematically_possible": bool(deficit < outstanding_upper),
        }

    def _thresholds(self) -> dict:
        c = self.config.calling
        return {
            "lean": c.lean_threshold,
            "lean_hysteresis": c.lean_hysteresis,
            "projected": c.projected_threshold,
            "called": c.called_threshold,
            "retraction": c.retraction_threshold,
            "min_reporting_projection": c.min_reporting_projection,
            "min_reporting_call": c.min_reporting_call,
            "close_band_pct": c.close_band_pct,
            "too_close_min_reporting": c.too_close_min_reporting,
            "call_at_poll_close": c.call_at_poll_close,
        }


def initial_state() -> CallState:
    """State of every race when the polls close (before any evaluation)."""
    return CallState(status=RaceStatus.POLLS_CLOSED, key=None, seq=0)
