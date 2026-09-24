"""Scenario document schema (FICTIONAL political assumptions).

A scenario is a human-readable YAML document (see ``config/scenarios/*.yaml``) that fully
specifies the political side of an election: parties, candidates, tickets, the national
environment, baselines/calibration targets, shocks, turnout, polling generation, campaigns
and the Electoral College allocation method.  Geography is never part of a scenario — it
always comes from the REAL CBS store.

All utility-scale numbers are *logit points* of the multinomial-logit vote model
(docs/SIMULATION.md): +0.10 ≈ +2.5 percentage points for a party near 25 %.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.constitution import ContingentElectionConfig, EVAllocationMethod


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- parties
class Ideology(_Model):
    economic: float = Field(0.0, ge=-1, le=1, description="−1 left … +1 right")
    social: float = Field(0.0, ge=-1, le=1, description="−1 progressive … +1 conservative")
    europe: float = Field(0.0, ge=-1, le=1, description="−1 eurosceptic … +1 pro-European")


class PartySpec(_Model):
    code: str = Field(..., pattern=r"^[A-Z][A-Z0-9]{1,11}$")
    name: str
    abbreviation: str
    color: str = Field(..., pattern=r"^#[0-9a-fA-F]{6}$")
    color_secondary: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    family: str | None = None
    description: str | None = None
    fictional: bool = True
    ideology: Ideology = Field(default_factory=Ideology)
    #: Baseline national vote share (0–1) when the party contests everywhere; the model calibrates
    #: party intercepts so the population-weighted national share matches this.
    base_share: float = Field(..., gt=0, lt=1)
    #: Logit shift of turnout among this party's supporters (+ = more reliable voters).
    turnout_propensity: float = 0.0
    #: Utility per population-SD of each demographic model variable (see DEMOGRAPHIC_VARIABLES).
    demographics: dict[str, float] = Field(default_factory=dict)
    #: Utility shift per CBS urbanity class "1".."5" (1 = very urban).
    urbanity: dict[str, float] = Field(default_factory=dict)
    #: Utility shift per province code.
    provinces: dict[str, float] = Field(default_factory=dict)
    #: Utility shift per named region (regions defined in config/regions.yaml).
    regions: dict[str, float] = Field(default_factory=dict)
    #: Utility shift per CBS municipality code.
    municipalities: dict[str, float] = Field(default_factory=dict)
    founded_year: int | None = None
    dissolved_year: int | None = None
    successor: str | None = None  # party code this party merged into

    @field_validator("urbanity")
    @classmethod
    def _urbanity_keys(cls, v: dict[str, float]) -> dict[str, float]:
        bad = [k for k in v if str(k) not in {"1", "2", "3", "4", "5"}]
        if bad:
            raise ValueError(f"urbanity keys must be '1'..'5', got {bad}")
        return {str(k): float(x) for k, x in v.items()}


class PartyEventSpec(_Model):
    """Lineage events applied when the scenario's election is created (history stays intact)."""

    year: int
    party: str
    event: Literal["founded", "renamed", "recolored", "merged_into", "split_from", "dissolved"]
    related_party: str | None = None
    new_name: str | None = None
    new_abbreviation: str | None = None
    new_color: str | None = None


# --------------------------------------------------------------------------- candidates
class CandidateSpec(_Model):
    key: str = Field(..., pattern=r"^[a-z0-9][a-z0-9\-]*$", description="stable slug, unique in scenario")
    first_name: str
    last_name: str
    gender: str | None = None
    birth_date: date | None = None
    party: str | None  # party code (None = independent)
    home_municipality: str | None = None  # CBS code 'GM0855'
    home_province: str | None = None  # derived from municipality when omitted
    quality: float = Field(0.0, ge=-3, le=3)  # candidate-quality z-score
    campaign_strength: float = Field(0.0, ge=-3, le=3)
    fundraising: float = Field(1.0, ge=0)
    favorability: float | None = None
    ideology: Ideology | None = None  # defaults to party ideology
    bio: str | None = None
    incumbent_office: str | None = None  # office code currently held, e.g. 'PRES', 'GOV-NB'


class TicketSpec(_Model):
    party: str | None
    president: str  # candidate key
    vice_president: str  # candidate key
    incumbent: bool = False
    withdrawn: bool = False


class PresidentialSpec(_Model):
    tickets: list[TicketSpec] = Field(default_factory=list)
    #: Home-province bonus (logit) for the presidential / vice-presidential candidate.
    home_province_bonus: float = 0.12
    vp_home_province_bonus: float = 0.05


# --------------------------------------------------------------------------- down-ballot
class ContestRule(_Model):
    """Where a party fields candidates in single-member races.

    A party contests a race when its *expected* baseline share in that jurisdiction is at least
    ``min_expected_share`` and (if given) the jurisdiction lies in ``provinces``/``regions``.
    ``always`` forces contesting everywhere.
    """

    always: bool = False
    min_expected_share: float = 0.06
    provinces: list[str] | None = None
    regions: list[str] | None = None


class DownBallotSpec(_Model):
    """Candidate generation for House / Senate / Governor / Mayor races."""

    contest_rules: dict[str, ContestRule] = Field(default_factory=dict)  # party code → rule
    default_rule: ContestRule = Field(default_factory=ContestRule)
    max_candidates: int = Field(6, ge=2)
    candidate_quality_sd: float = 0.35
    incumbent_runs_again_prob: float = Field(0.88, ge=0, le=1)
    explicit_candidates: dict[str, list[str]] = Field(
        default_factory=dict, description="race code → list of candidate keys (overrides generation)"
    )
    independents_prob: float = Field(0.03, ge=0, le=1)


class SenateSpec(DownBallotSpec):
    classes_up: list[int] | None = None  # default: from the election calendar
    special_elections: list[str] = Field(default_factory=list)  # seat codes with a special election


class GovernorSpec(DownBallotSpec):
    enabled: bool = True
    lieutenant_governors: bool = True


class MunicipalSpec(DownBallotSpec):
    enabled: bool = False
    councils: bool = True
    council_system: Literal["proportional_dhondt", "wards_fptp"] = "proportional_dhondt"


# --------------------------------------------------------------------------- environment
class ShockSpec(_Model):
    """Standard deviations (logit points) of the election-specific random components."""

    national_sd: float = 0.05
    province_sd: float = 0.035
    municipality_sd: float = 0.03
    unit_sd: float = 0.06
    spatial_sd: float = 0.03  # spatially correlated field (Gaussian process over centroids)
    spatial_length_km: float = 40.0
    turnout_national_sd: float = 0.08
    turnout_local_sd: float = 0.06
    tail_df: float = Field(6.0, gt=2, description="Student-t degrees of freedom for national shocks")


class IncumbencySpec(_Model):
    president: float = 0.04
    house: float = 0.07
    senate: float = 0.05
    governor: float = 0.07
    mayor: float = 0.08
    #: Utility penalty for the president's party in midterm House/Senate elections.
    midterm_penalty: float = 0.04


class EnvironmentSpec(_Model):
    """The national political environment of the scenario's election."""

    #: Party → logit shift relative to the baseline (e.g. momentum, scandal, economy).
    national: dict[str, float] = Field(default_factory=dict)
    #: Province code → party → logit shift.
    provinces: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: Named events (documented shocks), each shifting parties nationally.
    events: list[dict] = Field(default_factory=list)
    turnout_base: float = Field(0.78, gt=0, lt=1, description="national turnout target (share of eligible)")
    blank_rate: float = Field(0.004, ge=0, le=0.05)
    invalid_rate: float = Field(0.003, ge=0, le=0.05)
    shocks: ShockSpec = Field(default_factory=ShockSpec)
    incumbency: IncumbencySpec = Field(default_factory=IncumbencySpec)
    #: 0 = sincere voting; 1 = supporters of non-viable candidates fully defect to their closest viable one.
    strategic_voting: float = Field(0.35, ge=0, le=1)
    #: How strongly local elasticity scales national swings (0 = uniform swing).
    elasticity_strength: float = Field(0.5, ge=0, le=2)


class CalibrationSpec(_Model):
    """Optional baseline targets (vote shares 0–1) the structural model is calibrated to.

    ``national`` overrides party ``base_share``; ``provinces``/``municipalities`` pin local
    baselines ("manually defined party support").  Shares are normalised over listed parties.
    """

    national: dict[str, float] = Field(default_factory=dict)
    provinces: dict[str, dict[str, float]] = Field(default_factory=dict)
    municipalities: dict[str, dict[str, float]] = Field(default_factory=dict)
    #: Path (relative to config/) of an imported historical baseline (see app.simulation.baselines).
    imported_baseline: str | None = None
    imported_baseline_weight: float = Field(0.5, ge=0, le=1)
    max_iterations: int = 60
    tolerance: float = 1e-4


# --------------------------------------------------------------------------- polling / campaign
class PollsterSpec(_Model):
    name: str
    rating: float = Field(1.0, gt=0, description="aggregation weight multiplier (subjective, editable)")
    rating_label: str | None = None
    method: str = "online"
    house_effects: dict[str, float] = Field(default_factory=dict)  # party → percentage points
    typical_sample: int = 1500


class PollingSpec(_Model):
    generate: bool = True
    start_date: date | None = None  # default: 120 days before election
    national_polls: int = 45
    province_polls: int = 60
    district_polls: int = 40
    senate_polls: int = 24
    governor_polls: int = 24
    generic_ballot_polls: int = 20
    true_polling_error_sd: float = 0.02  # correlated industry-wide error (share points)
    pollsters: list[PollsterSpec] = Field(default_factory=list)


class CampaignSpec(_Model):
    enabled: bool = True
    weeks: int = 10
    #: Party → total budget (abstract units, 100 = typical major-party presidential campaign).
    budgets: dict[str, float] = Field(default_factory=dict)
    #: Party → strategy: balanced | battleground | base | expansion
    strategies: dict[str, str] = Field(default_factory=dict)
    #: Max absolute logit effect any single target can obtain from spending (diminishing returns).
    effect_cap: float = 0.06
    effect_uncertainty: float = 0.5  # relative SD of the realised effect


class ElectoralCollegeSpec(_Model):
    allocation: EVAllocationMethod = EVAllocationMethod.WINNER_TAKE_ALL
    contingent: ContingentElectionConfig = Field(default_factory=ContingentElectionConfig)


# --------------------------------------------------------------------------- root
class ScenarioMeta(_Model):
    slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9\-_]*$")
    name: str
    description: str | None = None
    year: int = Field(..., ge=1900, le=2500)
    election_type: Literal["general", "midterm", "special", "municipal", "provincial"] = "general"
    election_date: date | None = None  # default from the calendar rule
    seed: int = Field(..., ge=0, lt=2**62)
    #: Seed of the persistent, structural local-lean field (kept constant across elections so
    #: that municipalities keep a stable political character over time).
    political_geography_seed: int = Field(1848, ge=0, lt=2**62)
    fictional: Literal[True] = True


class ScenarioDocument(_Model):
    scenario: ScenarioMeta
    parties: list[PartySpec]
    party_events: list[PartyEventSpec] = Field(default_factory=list)
    candidates: list[CandidateSpec] = Field(default_factory=list)
    president: PresidentialSpec = Field(default_factory=PresidentialSpec)
    house: DownBallotSpec = Field(default_factory=DownBallotSpec)
    senate: SenateSpec = Field(default_factory=SenateSpec)
    governors: GovernorSpec = Field(default_factory=GovernorSpec)
    municipal: MunicipalSpec = Field(default_factory=MunicipalSpec)
    environment: EnvironmentSpec = Field(default_factory=EnvironmentSpec)
    calibration: CalibrationSpec = Field(default_factory=CalibrationSpec)
    polling: PollingSpec = Field(default_factory=PollingSpec)
    campaigns: CampaignSpec = Field(default_factory=CampaignSpec)
    electoral_college: ElectoralCollegeSpec = Field(default_factory=ElectoralCollegeSpec)

    @model_validator(mode="after")
    def _cross_refs(self) -> ScenarioDocument:
        party_codes = {p.code for p in self.parties}
        if len(party_codes) != len(self.parties):
            raise ValueError("duplicate party codes")
        cand_keys = [c.key for c in self.candidates]
        if len(set(cand_keys)) != len(cand_keys):
            raise ValueError("duplicate candidate keys")
        ckeys = set(cand_keys)
        problems: list[str] = []
        for c in self.candidates:
            if c.party is not None and c.party not in party_codes:
                problems.append(f"candidate {c.key}: unknown party {c.party}")
        for t in self.president.tickets:
            if t.party is not None and t.party not in party_codes:
                problems.append(f"ticket: unknown party {t.party}")
            for role in (t.president, t.vice_president):
                if role not in ckeys:
                    problems.append(f"ticket: unknown candidate {role}")
        for block in (self.environment.national, self.calibration.national):
            for code in block:
                if code not in party_codes:
                    problems.append(f"unknown party in environment/calibration: {code}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def party(self, code: str) -> PartySpec:
        for p in self.parties:
            if p.code == code:
                return p
        raise KeyError(code)

    def candidate(self, key: str) -> CandidateSpec:
        for c in self.candidates:
            if c.key == key:
                return c
        raise KeyError(key)
