"""Election-night configuration (``config/night.yaml``) — schema and loader.

The document drives three engines:

* :mod:`app.reporting.timeline` — when polls close, how fast municipalities count, how many
  batches they report in, how batches are composed (wijk clusters, partial precincts);
* :mod:`app.reporting.calling` — the probabilistic race-calling thresholds and the
  uncertainty model of the outstanding vote;
* :mod:`app.reporting.live` / :mod:`app.reporting.clock` — playback speeds and snapshot sizes.

Everything configured here is FICTIONAL (the reporting process is simulated); see
``docs/RACE_CALLING.md`` for the meaning of every parameter.
"""

from __future__ import annotations

import re
from datetime import date, time
from functools import lru_cache
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import load_config

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

#: Name of the configuration document in ``config/``.
NIGHT_CONFIG_FILE = "night.yaml"


def parse_hhmm(value: str) -> time:
    """Parse a local ``'HH:MM'`` clock string (24-hour) into :class:`datetime.time`."""
    m = _HHMM.match(value.strip())
    if not m:
        raise ValueError(f"expected 'HH:MM' (24h), got {value!r}")
    return time(int(m.group(1)), int(m.group(2)))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- polls closing
class PollsCloseConfig(_Model):
    """Local poll-closing times.  ``sim_time_s = 0`` is the *earliest* closing time."""

    default: str = "21:00"
    timezone: str = "Europe/Amsterdam"
    #: Province code → 'HH:MM' override.
    provinces: dict[str, str] = Field(default_factory=dict)
    #: CBS municipality code → 'HH:MM' override (takes precedence over the province).
    municipalities: dict[str, str] = Field(default_factory=dict)
    #: Election date used when the caller does not supply one (timestamps only).
    fallback_election_date: date = date(2028, 11, 7)

    @field_validator("default")
    @classmethod
    def _check_default(cls, v: str) -> str:
        parse_hhmm(v)
        return v

    @field_validator("provinces", "municipalities")
    @classmethod
    def _check_overrides(cls, v: dict[str, str]) -> dict[str, str]:
        for key, val in v.items():
            try:
                parse_hhmm(val)
            except ValueError as exc:
                raise ValueError(f"{key}: {exc}") from exc
        return v

    @field_validator("timezone")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(v)
        except Exception as exc:  # ZoneInfoNotFoundError is a KeyError, not a ValueError
            raise ValueError(f"unknown time zone {v!r}") from exc
        return v


# --------------------------------------------------------------------------- playback
class PlaybackConfig(_Model):
    #: Simulated seconds per real second at speed 1× (60 → one simulated minute per second).
    base_rate: float = Field(60.0, gt=0)
    speeds: list[float] = Field(default_factory=lambda: [1.0, 2.0, 5.0, 10.0, 25.0])
    default_speed: float = 1.0

    @model_validator(mode="after")
    def _check(self) -> PlaybackConfig:
        if not self.speeds or any(s <= 0 for s in self.speeds):
            raise ValueError("speeds must be a non-empty list of positive numbers")
        self.speeds = sorted(float(s) for s in self.speeds)
        if float(self.default_speed) not in self.speeds:
            raise ValueError("default_speed must be one of speeds")
        return self


# --------------------------------------------------------------------------- reporting speed
class FirstReportConfig(_Model):
    """Minutes from a municipality's poll closing to its first batch.

    ``minutes = (base + per_log10 · log10(ballots / size_ref) + per_urbanity · u
    + province_effect) · exp(N(0, jitter_sd))``, clipped to ``[min_minutes, max_minutes]``, where
    ``u`` is the ballot-weighted urbanity step of the municipality (0 = rural … 4 = very urban).
    """

    base_minutes: float = 45.0
    minutes_per_log10_ballots: float = 30.0
    minutes_per_urbanity_step: float = 8.0
    jitter_sd: float = Field(0.22, ge=0)
    min_minutes: float = Field(24.0, ge=0)
    max_minutes: float = Field(330.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> FirstReportConfig:
        if self.max_minutes < self.min_minutes:
            raise ValueError("first_report.max_minutes must be ≥ min_minutes")
        return self


class DurationConfig(_Model):
    """Minutes from a municipality's first to its last batch.

    ``base · (ballots / ref_ballots)^elasticity · (1 + urbanity_factor · u)
    · exp(N(0, jitter_sd)) · province factor``; single-batch municipalities have zero duration.
    """

    ref_ballots: float = Field(20_000.0, gt=0)
    base_minutes: float = Field(80.0, ge=0)
    elasticity: float = Field(0.55, ge=0)
    urbanity_factor_per_step: float = Field(0.10, ge=0)
    jitter_sd: float = Field(0.25, ge=0)
    min_minutes: float = Field(5.0, ge=0)


class BatchTier(_Model):
    """Batch count range for municipalities with at most ``max_ballots`` ballots (null = no cap)."""

    max_ballots: float | None = None
    min: int = Field(1, ge=1)
    max: int = Field(1, ge=1)

    @model_validator(mode="after")
    def _check(self) -> BatchTier:
        if self.max < self.min:
            raise ValueError("batch tier max < min")
        return self


def _default_tiers() -> list[BatchTier]:
    return [
        BatchTier(max_ballots=3_000, min=1, max=2),
        BatchTier(max_ballots=10_000, min=2, max=4),
        BatchTier(max_ballots=25_000, min=3, max=7),
        BatchTier(max_ballots=60_000, min=6, max=12),
        BatchTier(max_ballots=150_000, min=10, max=22),
        BatchTier(max_ballots=300_000, min=18, max=36),
        BatchTier(max_ballots=None, min=30, max=60),
    ]


#: FICTIONAL province counting-speed effects (minutes added to the first-report delay).
DEFAULT_PROVINCE_EFFECT_MINUTES: dict[str, float] = {
    "GR": -3.0,
    "FR": -4.0,
    "DR": -6.0,
    "OV": -2.0,
    "FL": 0.0,
    "GE": 0.0,
    "UT": 3.0,
    "NH": 5.0,
    "ZH": 6.0,
    "ZE": -3.0,
    "NB": 2.0,
    "LI": 3.0,
}


class PartialPrecinctConfig(_Model):
    """Large buurten may be counted over several batches ("partial precincts")."""

    enabled: bool = True
    #: Units with at least this many ballots may be split across batches.
    min_ballots: int = Field(1_200, ge=1)
    #: Pieces smaller than this fraction of the unit are merged into the neighbouring batch.
    min_piece_fraction: float = Field(0.15, ge=0, lt=0.5)


class ReportingSpeedConfig(_Model):
    #: Reference municipality size (ballots) for the first-report model.
    size_ref_ballots: float = Field(2_000.0, gt=0)
    first_report: FirstReportConfig = Field(default_factory=FirstReportConfig)
    duration: DurationConfig = Field(default_factory=DurationConfig)
    #: Latest scheduled last batch, in minutes after the earliest poll closing (soft cap;
    #: 525 → 05:45 with polls closing at 21:00).
    latest_end_minutes: float = Field(525.0, gt=0)
    #: Spread of the scheduled ends that hit the cap (minutes before the cap).
    end_spread_minutes: float = Field(55.0, ge=0)
    #: Fixed province effects (minutes added to the first-report delay).
    province_effect_minutes: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_PROVINCE_EFFECT_MINUTES)
    )
    #: Seeded random province effect on the first-report delay (minutes, SD).
    province_random_sd_minutes: float = Field(4.0, ge=0)
    #: Seeded random province effect on the counting duration (log SD).
    province_duration_sd: float = Field(0.10, ge=0)
    batches_by_size: list[BatchTier] = Field(default_factory=_default_tiers)
    #: Dirichlet concentration of batch sizes within a municipality (higher = more equal).
    batch_size_concentration: float = Field(4.0, gt=0)
    #: A batch cut within this fraction of the target batch size of a wijk boundary snaps to it.
    wijk_snap_tolerance: float = Field(0.35, ge=0, le=1)
    #: Minimum gap (seconds) between two batches of the same municipality.
    min_batch_gap_s: float = Field(45.0, ge=0)
    partial_precinct: PartialPrecinctConfig = Field(default_factory=PartialPrecinctConfig)
    #: Keep a full per-unit fraction checkpoint every N events (fast random access replay).
    checkpoint_every: int = Field(500, ge=1)

    @model_validator(mode="after")
    def _check(self) -> ReportingSpeedConfig:
        tiers = self.batches_by_size
        if not tiers or tiers[-1].max_ballots is not None:
            raise ValueError("batches_by_size must end with an open tier (max_ballots: null)")
        caps = [t.max_ballots for t in tiers[:-1]]
        if any(c is None for c in caps) or any(b <= a for a, b in pairwise(caps)):  # type: ignore[operator]
            raise ValueError("batches_by_size tiers must have strictly increasing max_ballots")
        return self


# --------------------------------------------------------------------------- calling
class CallModelConfig(_Model):
    """Uncertainty model of the outstanding vote (log-share / log-ratio units)."""

    #: Prior SD of the race-level swing vs the pre-election expectation (log-share points).
    swing_prior_sd: float = Field(0.12, gt=0)
    #: Student-t degrees of freedom of the race-level swing draws (null = Gaussian).
    swing_tail_df: float | None = Field(5.0, gt=2)
    #: Systematic difference between the swing in reported and in outstanding areas.
    differential_sd: float = Field(0.03, ge=0)
    #: Between-municipality deviation from the race swing.
    cluster_sd: float = Field(0.06, ge=0)
    #: Between-unit (buurt) deviation, averaged within clusters (Herfindahl weighting).
    unit_sd: float = Field(0.10, ge=0)
    #: Composition uncertainty of the uncounted remainder of partially counted units.
    partial_sd: float = Field(0.02, ge=0)
    #: Prior SD of the race turnout ratio (log) vs expectation.
    turnout_prior_sd: float = Field(0.06, gt=0)
    #: Between-municipality turnout deviation (log).
    turnout_cluster_sd: float = Field(0.05, ge=0)
    #: Pseudo-votes added when forming log ratios (stabilises tiny lines).
    pseudo_votes: float = Field(0.5, gt=0)
    #: Valid / ballots ratio assumed before any ballots are counted.
    valid_rate_prior: float = Field(0.993, gt=0, le=1)
    #: Iterations of the log-linear swing fit.
    fit_iterations: int = Field(12, ge=1)


class CallingConfig(_Model):
    lean_threshold: float = Field(0.80, gt=0.5, lt=1)
    #: A LEAN is kept while its line stays within this distance below ``lean_threshold``.
    lean_hysteresis: float = Field(0.03, ge=0, lt=0.3)
    projected_threshold: float = Field(0.995, gt=0.5, lt=1)
    called_threshold: float = Field(0.9995, gt=0.5, le=1)
    retraction_threshold: float = Field(0.90, gt=0, lt=1)
    #: Minimum reporting (share of expected ballots, 0–1) before a race may be PROJECTED.
    min_reporting_projection: float = Field(0.05, ge=0, le=1)
    #: Minimum reporting (share of expected ballots, 0–1) before a race may be CALLED
    #: (mathematical certainty is exempt).
    min_reporting_call: float = Field(0.15, ge=0, le=1)
    #: A touched race is re-evaluated only once its reporting (percentage points of expected
    #: ballots) grew by at least this much since its last evaluation — plus always on its first
    #: results and at 100 %.  Counted votes and leaders update on every event regardless.
    min_eval_increment_pct: float = Field(0.5, ge=0)
    #: The same increment for races already CALLED (they are only checked for retraction).
    called_eval_increment_pct: float = Field(5.0, ge=0)
    #: Expected final margin (percentage points, top two) inside which a race with at least
    #: ``min_reporting_projection`` reporting is shown TOO_CLOSE instead of LEAN.
    close_band_pct: float = Field(1.0, ge=0)
    #: Reporting share (0–1) from which an undecided race is TOO_CLOSE rather than TOO_EARLY.
    too_close_min_reporting: float = Field(0.5, ge=0, le=1)
    #: Allow calls from expectations alone when polls close (0 % reporting).
    call_at_poll_close: bool = False
    #: Monte-Carlo draws: first pass / standard / maximum (adaptive refinement near thresholds).
    n_draws_min: int = Field(500, ge=50)
    n_draws: int = Field(4_000, ge=100)
    n_draws_max: int = Field(16_000, ge=100)
    model: CallModelConfig = Field(default_factory=CallModelConfig)

    @model_validator(mode="after")
    def _check(self) -> CallingConfig:
        if not (self.lean_threshold < self.projected_threshold <= self.called_threshold):
            raise ValueError("thresholds must satisfy lean < projected ≤ called")
        if self.retraction_threshold >= self.projected_threshold:
            raise ValueError("retraction_threshold must be below projected_threshold")
        if not (self.n_draws_min <= self.n_draws <= self.n_draws_max):
            raise ValueError("n_draws_min ≤ n_draws ≤ n_draws_max required")
        return self


class RecountConfig(_Model):
    #: Fallback automatic-recount margin (percentage points of valid votes, winner − runner-up)
    #: used when no ``recount_check`` callable is injected.  Exact ties always go to recount.
    margin_pct: float = Field(0.25, ge=0)


class SnapshotConfig(_Model):
    recent_calls: int = Field(12, ge=0)
    recent_lead_changes: int = Field(12, ge=0)


class NightConfig(_Model):
    """Root of ``config/night.yaml``."""

    data_category: str = "SIMULATED"
    polls_close: PollsCloseConfig = Field(default_factory=PollsCloseConfig)
    playback: PlaybackConfig = Field(default_factory=PlaybackConfig)
    reporting: ReportingSpeedConfig = Field(default_factory=ReportingSpeedConfig)
    calling: CallingConfig = Field(default_factory=CallingConfig)
    recount: RecountConfig = Field(default_factory=RecountConfig)
    snapshot: SnapshotConfig = Field(default_factory=SnapshotConfig)

    def fingerprint(self) -> str:
        """Stable short hash of the configuration (stored with every generated night)."""
        from app.core.rng import config_hash

        return config_hash(self.model_dump_json())


@lru_cache(maxsize=1)
def _default() -> NightConfig:
    return NightConfig()


def load_night_config(name: str = NIGHT_CONFIG_FILE) -> NightConfig:
    """Load ``config/night.yaml`` (schema defaults when the file is absent)."""
    return load_config(name, NightConfig, optional=True)


def default_night_config() -> NightConfig:
    """Schema defaults (independent of the YAML file) — handy for tests and tools."""
    return _default().model_copy(deep=True)
