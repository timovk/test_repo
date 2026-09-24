"""Deterministic generation of FICTIONAL candidates for single-member races.

Every generated person is fictional: a random combination of common Dutch given names and
surnames (see :mod:`app.simulation.names`), a home municipality drawn population-weighted from
the race's REAL jurisdiction, a birth date and a candidate-quality z-score.  Which parties field
candidates follows the scenario's :class:`~app.scenarios.schema.ContestRule` s, evaluated against
the structural model's expected shares in the jurisdiction.

All draws use keyed RNG streams ``(seed, "candidates", race_key, party)`` so adding a race or a
party elsewhere never changes the candidates of another race.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from app.core.constitution import RaceType
from app.core.errors import ScenarioError
from app.core.logging import get_logger
from app.core.rng import derive_seed, make_rng
from app.elections.types import BallotLine
from app.scenarios.schema import CandidateSpec, ContestRule, DownBallotSpec, ScenarioDocument
from app.simulation import names as name_pools
from app.simulation.structural import StructuralModel

log = get_logger(__name__)

_NORTH = {"GR", "FR", "DR"}
_SOUTH = {"NB", "LI"}
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"

PROFESSIONS: tuple[str, ...] = (
    "secondary-school teacher",
    "nurse",
    "dairy farmer",
    "small-business owner",
    "lawyer",
    "police officer",
    "municipal alderman",
    "civil servant",
    "civil engineer",
    "general practitioner",
    "trade-union organiser",
    "shopkeeper",
    "economist",
    "social worker",
    "army officer",
    "journalist",
    "software developer",
    "fisherman",
    "university lecturer",
    "housing-corporation director",
    "care-home manager",
    "logistics manager",
    "arable farmer",
    "primary-school head teacher",
    "accountant",
    "former professional cyclist",
    "harbour pilot",
    "pastor",
    "youth worker",
    "provincial councillor",
)

_OFFICE_LABEL = {
    RaceType.HOUSE: "the Tweede Kamer",
    RaceType.SENATE: "the Eerste Kamer",
    RaceType.GOVERNOR: "Governor",
    RaceType.MAYOR: "Mayor",
    RaceType.PROVINCIAL_LEGISLATURE: "the Provincial Legislature",
    RaceType.MUNICIPAL_COUNCIL: "the Municipal Council",
}


# --------------------------------------------------------------------------- names
def slugify(text: str) -> str:
    """ASCII slug: ``"Fleur van 't Hof"`` → ``"fleur-van-t-hof"``."""
    s = unicodedata.normalize("NFKD", text)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("ı", "i").lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def fictional_name(
    rng: np.random.Generator,
    gender: str | None = None,
    *,
    province: str | None = None,
    heritage_prob: float = 0.0,
) -> tuple[str, str]:
    """Draw a fictional ``(first_name, last_name)``.

    ``gender`` ``"F"``/``"M"`` (drawn when ``None``); ``province`` biases surnames towards regional
    pools (north: Frisian/Groningen, south: Brabant/Limburg); ``heritage_prob`` is the probability
    of a migrant-heritage name.  Blocked combinations (real public figures) are redrawn.
    """
    if gender is None:
        gender = "F" if rng.random() < 0.5 else "M"
    for _ in range(50):
        heritage = rng.random() < heritage_prob
        if gender == "F":
            pool = (
                name_pools.GIVEN_NAMES_FEMALE_HERITAGE
                if heritage and rng.random() < 0.8
                else name_pools.GIVEN_NAMES_FEMALE
            )
        else:
            pool = (
                name_pools.GIVEN_NAMES_MALE_HERITAGE
                if heritage and rng.random() < 0.8
                else name_pools.GIVEN_NAMES_MALE
            )
        first = str(pool[int(rng.integers(len(pool)))])
        if heritage:
            spool = name_pools.SURNAMES_HERITAGE
        elif province in _NORTH and rng.random() < 0.45:
            spool = name_pools.SURNAMES_NORTH
        elif province in _SOUTH and rng.random() < 0.45:
            spool = name_pools.SURNAMES_SOUTH
        else:
            spool = name_pools.SURNAMES_COMMON
        last = str(spool[int(rng.integers(len(spool)))])
        full = unicodedata.normalize("NFKD", f"{first} {last}".lower())
        full = "".join(ch for ch in full if not unicodedata.combining(ch)).replace("ı", "i")
        if full not in name_pools.BLOCKED_FULL_NAMES:
            return first, last
    raise RuntimeError("could not draw an unblocked name")  # pragma: no cover


def _key_suffix(seed: int, *keys: str) -> str:
    v = derive_seed(seed, "candidate-key", *keys)
    out = ""
    for _ in range(4):
        v, r = divmod(v, 36)
        out += _B36[r]
    return out


# --------------------------------------------------------------------------- results
@dataclass
class RaceCandidates:
    """Candidates (and ballot lines) of one race."""

    race_key: str
    race_type: RaceType
    candidates: list[CandidateSpec]
    lines: list[BallotLine]
    generated: list[str] = field(default_factory=list)  # keys of newly generated candidates
    incumbent_running: bool = False
    contested_parties: list[str] = field(default_factory=list)
    expected_shares: dict[str, float] = field(default_factory=dict)
    #: Party of the sitting office holder (also when they do not run: the open seat's party).
    incumbent_party: str | None = None


@dataclass(frozen=True)
class RaceSlot:
    """A single-member race awaiting candidates."""

    key: str
    race_type: RaceType
    unit_index: np.ndarray
    province_code: str | None = None
    office_label: str | None = None


def rules_for(doc: ScenarioDocument, race_type: RaceType | str) -> DownBallotSpec:
    """The scenario's candidate-generation rules for a race type."""
    rt = RaceType(race_type)
    if rt == RaceType.HOUSE:
        return doc.house
    if rt == RaceType.SENATE:
        return doc.senate
    if rt == RaceType.GOVERNOR:
        return doc.governors
    if rt in (RaceType.MAYOR, RaceType.MUNICIPAL_COUNCIL):
        return doc.municipal
    return DownBallotSpec()


# --------------------------------------------------------------------------- contest decisions
def _region_share(model: StructuralModel, units: np.ndarray, region: str) -> float:
    if region not in model.regions:
        return 0.0
    mask = model.regions.unit_mask(model.frame, region)[units]
    w = model.eligible[units]
    tot = w.sum()
    return float((w * mask).sum() / tot) if tot > 0 else 0.0


def party_contests(
    model: StructuralModel,
    rule: ContestRule,
    expected_share: float,
    units: np.ndarray,
    province_code: str | None,
) -> bool:
    """Whether a party with ``rule`` contests a jurisdiction (see :class:`ContestRule`)."""
    if rule.always:
        return True
    if expected_share < rule.min_expected_share:
        return False
    if rule.provinces is not None and province_code not in rule.provinces:
        return False
    if rule.regions is not None:
        thr = model.config.candidates.region_overlap_threshold
        if not any(_region_share(model, units, r) >= thr for r in rule.regions):
            return False
    return True


def _birth_date(rng: np.random.Generator, year: int, age_range: tuple[int, int]) -> date:
    lo, hi = age_range
    # triangular: most candidates in the middle of the range
    age = round(rng.triangular(lo, (lo + hi) / 2 + 2, hi))
    month = int(rng.integers(1, 13))
    day = int(rng.integers(1, 29))
    return date(year - age - 1 if month > 10 else year - age, month, day)


def _home_municipality(model: StructuralModel, rng: np.random.Generator, units: np.ndarray) -> str | None:
    w = model.eligible[units]
    if len(units) == 0:
        return None
    p = w / w.sum() if w.sum() > 0 else np.full(len(units), 1.0 / len(units))
    u = int(units[int(rng.choice(len(units), p=p))])
    return model.frame.muni_codes[int(model.frame.unit_muni[u])]


def _heritage_prob(model: StructuralModel, muni_code: str | None) -> float:
    f = model.frame
    if muni_code is None or "pct_origin_non_europe" not in f.demo_names:
        return 0.05
    m = f.muni_index_or_none(muni_code)
    if m is None:
        return 0.05
    units = f.units_in_muni(m)
    col = f.unit_demo[units, list(f.demo_names).index("pct_origin_non_europe")]
    w = f.unit_population[units].astype(float)
    ok = np.isfinite(col)
    if not ok.any() or w[ok].sum() <= 0:
        return 0.05
    pct = float((col[ok] * w[ok]).sum() / w[ok].sum())
    return float(np.clip(pct / 100.0 * 0.8, 0.02, 0.5))


def _make_candidate(
    model: StructuralModel,
    rng: np.random.Generator,
    *,
    seed: int,
    race_key: str,
    race_type: RaceType,
    party: str | None,
    units: np.ndarray,
    quality_sd: float,
    quality_mean: float,
    office_label: str,
    existing: set[str],
) -> CandidateSpec:
    cc = model.config.candidates
    gender = "F" if rng.random() < cc.female_share else "M"
    home = _home_municipality(model, rng, units)
    f = model.frame
    home_idx = f.muni_index(home) if home else None
    prov = f.province_codes[int(f.muni_province[home_idx])] if home_idx is not None else None
    first, last = fictional_name(rng, gender, province=prov, heritage_prob=_heritage_prob(model, home))
    base = slugify(f"{first} {last}")
    key = f"{base}-{_key_suffix(seed, race_key, party or 'IND')}"
    n = 2
    while key in existing:
        key = f"{base}-{_key_suffix(seed, race_key, party or 'IND', str(n))}"
        n += 1
    existing.add(key)
    year = model.scenario.scenario.year
    age_range = cc.age_range.get(race_type.value, (30, 68))
    quality = float(np.clip(rng.normal(quality_mean, quality_sd), -3.0, 3.0))
    profession = PROFESSIONS[int(rng.integers(len(PROFESSIONS)))]
    muni_name = f.muni_names[home_idx] if home_idx is not None else "the district"
    if party is not None:
        pname = model.scenario.party(party).name
        bio = f"Fictional {pname} candidate for {office_label}; {profession} from {muni_name}."
    else:
        bio = f"Fictional independent candidate for {office_label}; {profession} from {muni_name}."
    return CandidateSpec(
        key=key,
        first_name=first,
        last_name=last,
        gender=gender,
        birth_date=_birth_date(rng, year, age_range),
        party=party,
        home_municipality=home,
        home_province=prov,
        quality=quality,
        campaign_strength=float(np.clip(rng.normal(0.0, 0.5), -3.0, 3.0)),
        fundraising=float(np.round(rng.lognormal(0.0, 0.4), 3)),
        bio=bio,
    )


def _line(c: CandidateSpec, *, incumbent: bool = False) -> BallotLine:
    return BallotLine(
        key=c.key,
        party_code=c.party,
        candidate_key=c.key,
        label=f"{c.first_name} {c.last_name}",
        quality=c.quality,
        incumbent=incumbent,
        home_province=c.home_province,
        home_municipality=c.home_municipality,
    )


def generate_race_candidates(
    model: StructuralModel,
    race_key: str,
    race_type: RaceType | str,
    unit_index: np.ndarray | Sequence[int],
    rules: DownBallotSpec,
    seed: int,
    *,
    province_code: str | None = None,
    incumbent: CandidateSpec | None = None,
    scenario_candidates: Mapping[str, CandidateSpec] | None = None,
    existing_keys: set[str] | None = None,
    office_label: str | None = None,
) -> RaceCandidates:
    """Field candidates for one single-member race.

    * ``rules.explicit_candidates[race_key]`` (keys into ``scenario_candidates``) overrides
      generation entirely;
    * otherwise parties contest per their :class:`ContestRule` (expected share threshold,
      provinces, regions, ``always``), at most ``rules.max_candidates`` lines, at least two;
      ``always`` parties and a re-running incumbent are guaranteed a line (they may exceed the
      maximum only when they alone outnumber it);
    * the ``incumbent`` runs again with probability ``rules.incumbent_runs_again_prob`` — under
      their party's label, so an incumbent whose party is not in the scenario (dissolved, merged)
      does not run; an independent incumbent occupies the independent slot;
    * with probability ``rules.independents_prob`` an independent joins the ballot;
    * new candidates get quality ``N(0, rules.candidate_quality_sd)`` and a home municipality
      drawn by eligible voters within the jurisdiction.
    """
    rt = RaceType(race_type)
    units = np.asarray(unit_index, dtype=np.int64)
    existing = existing_keys if existing_keys is not None else set()
    scenario_candidates = scenario_candidates or {}
    office = office_label or _OFFICE_LABEL.get(rt, rt.value.title())
    codes = model.party_codes
    js = model.jurisdiction_shares(units, codes)
    expected = {c: float(v) for c, v in zip(codes, js, strict=True)}

    explicit = rules.explicit_candidates.get(race_key)
    if explicit:
        cands = []
        for k in explicit:
            if k not in scenario_candidates:
                raise ScenarioError(f"{race_key}: explicit candidate {k!r} is not defined in the scenario")
            cands.append(scenario_candidates[k])
        inc_key = incumbent.key if incumbent is not None else None
        lines = [_line(c, incumbent=c.key == inc_key) for c in cands]
        return RaceCandidates(
            race_key=race_key,
            race_type=rt,
            candidates=cands,
            lines=lines,
            incumbent_running=inc_key in {c.key for c in cands},
            contested_parties=sorted({c.party for c in cands if c.party}),
            expected_shares=expected,
            incumbent_party=incumbent.party if incumbent is not None else None,
        )

    # --- which parties contest --------------------------------------------------------------
    order = sorted(codes, key=lambda c: (-expected[c], c))
    contesting = [
        c
        for c in order
        if party_contests(
            model, rules.contest_rules.get(c, rules.default_rule), expected[c], units, province_code
        )
    ]
    always = {c for c in contesting if rules.contest_rules.get(c, rules.default_rule).always}
    rng_inc = make_rng(seed, "candidates", race_key, "incumbent")
    inc_runs = incumbent is not None and bool(rng_inc.random() < rules.incumbent_runs_again_prob)
    if inc_runs and incumbent is not None and incumbent.party is not None and incumbent.party not in expected:
        log.warning(
            "%s: incumbent %s's party %s is not in the scenario; the seat is open",
            race_key,
            incumbent.key,
            incumbent.party,
        )
        inc_runs = False
    rng_ind = make_rng(seed, "candidates", race_key, "independent")
    add_independent = bool(rng_ind.random() < rules.independents_prob)
    independent_incumbent = inc_runs and incumbent is not None and incumbent.party is None
    slots = rules.max_candidates - (1 if (add_independent or independent_incumbent) else 0)
    must = set(always)
    if inc_runs and incumbent is not None and incumbent.party is not None:
        must.add(incumbent.party)
        if incumbent.party not in contesting:
            contesting.append(incumbent.party)
    chosen = [c for c in contesting if c in must]
    for c in contesting:
        if len(chosen) >= max(slots, len(must)):
            break
        if c not in chosen:
            chosen.append(c)
    # at least two lines on the ballot (an independent counts as one)
    min_party_lines = 1 if (add_independent or independent_incumbent) else 2
    for c in order:
        if len(chosen) >= min_party_lines:
            break
        if c not in chosen:
            chosen.append(c)
    chosen.sort(key=lambda c: (-expected.get(c, 0.0), c))

    candidates: list[CandidateSpec] = []
    lines: list[BallotLine] = []
    generated: list[str] = []
    sd = rules.candidate_quality_sd
    for party in chosen:
        if inc_runs and incumbent is not None and incumbent.party == party:
            candidates.append(incumbent)
            lines.append(_line(incumbent, incumbent=True))
            continue
        rng = make_rng(seed, "candidates", race_key, party)
        c = _make_candidate(
            model,
            rng,
            seed=seed,
            race_key=race_key,
            race_type=rt,
            party=party,
            units=units,
            quality_sd=sd,
            quality_mean=0.0,
            office_label=office,
            existing=existing,
        )
        candidates.append(c)
        lines.append(_line(c))
        generated.append(c.key)
    if inc_runs and incumbent is not None and incumbent.party is None:
        candidates.append(incumbent)
        lines.append(_line(incumbent, incumbent=True))
    elif add_independent:
        rng = make_rng(seed, "candidates", race_key, "IND")
        c = _make_candidate(
            model,
            rng,
            seed=seed,
            race_key=race_key,
            race_type=rt,
            party=None,
            units=units,
            quality_sd=sd,
            quality_mean=0.2,
            office_label=office,
            existing=existing,
        )
        candidates.append(c)
        lines.append(_line(c))
        generated.append(c.key)
    return RaceCandidates(
        race_key=race_key,
        race_type=rt,
        candidates=candidates,
        lines=lines,
        generated=generated,
        incumbent_running=inc_runs,
        contested_parties=[c for c in chosen],
        expected_shares=expected,
        incumbent_party=incumbent.party if incumbent is not None else None,
    )


def generate_down_ballot(
    model: StructuralModel,
    slots: Sequence[RaceSlot],
    seed: int,
    *,
    rules: Mapping[RaceType, DownBallotSpec] | None = None,
    incumbents: Mapping[str, CandidateSpec] | None = None,
    existing_keys: set[str] | None = None,
) -> dict[str, RaceCandidates]:
    """Candidates for many races (rules default to the model scenario's sections per race type).

    ``incumbents`` maps race key → sitting office holder; candidate keys are unique across the
    whole batch (and against ``existing_keys``).
    """
    doc = model.scenario
    incumbents = incumbents or {}
    existing = existing_keys if existing_keys is not None else {c.key for c in doc.candidates}
    scenario_candidates = {c.key: c for c in doc.candidates}
    out: dict[str, RaceCandidates] = {}
    for slot in slots:
        rt = RaceType(slot.race_type)
        r = (rules or {}).get(rt) or rules_for(doc, rt)
        out[slot.key] = generate_race_candidates(
            model,
            slot.key,
            rt,
            slot.unit_index,
            r,
            seed,
            province_code=slot.province_code,
            incumbent=incumbents.get(slot.key),
            scenario_candidates=scenario_candidates,
            existing_keys=existing,
            office_label=slot.office_label,
        )
    log.info(
        "generated candidates for %d races (%d new people)",
        len(out),
        sum(len(r.generated) for r in out.values()),
    )
    return out
