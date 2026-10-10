"""Your own people (``config/people.yaml``): custom candidates — friends, family — who sometimes
run for office where they live.

Every person has a home municipality.  At every election each person decides (with their
``chance``) whether to run; a person who runs picks one race on that ballot whose area contains
their home — the school board or water board, a council seat or the mayoralty of their
municipality, the House district, the province's Senate seat or governorship — weighted towards
local offices.  A person with a party runs for that party (the party then fields a line in that
race); a person without one runs in the nonpartisan board races or as an independent.  A person
runs in at most one race per election, keeps the same identity across elections (a winner runs
again as the incumbent) and must meet the minimum age of the office.

Everything is a pure function of the file, the geography and the election seed: the same people
appear in the same races when an election is created again.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core.config import config_path
from app.core.constitution import RaceType
from app.core.errors import ScenarioError
from app.core.logging import get_logger
from app.core.rng import make_rng
from app.geography.frame import GeographyFrame
from app.scenarios.schema import CandidateSpec, Ideology

log = get_logger(__name__)

PEOPLE_FILE = "people.yaml"

#: ``chance`` words → probability of running at an election where the person has a race.
CHANCES: dict[str, float] = {"never": 0.0, "rarely": 0.15, "sometimes": 0.35, "often": 0.65, "always": 1.0}

#: Office kinds of ``offices:`` / ``settings`` → race types.
OFFICE_KINDS: dict[str, RaceType] = {
    "school_board": RaceType.SCHOOL_BOARD,
    "water_board": RaceType.WATER_BOARD,
    "council": RaceType.COUNCIL_SEAT,
    "mayor": RaceType.MAYOR,
    "house": RaceType.HOUSE,
    "senate": RaceType.SENATE,
    "governor": RaceType.GOVERNOR,
}
KIND_OF: dict[RaceType, str] = {v: k for k, v in OFFICE_KINDS.items()}
#: Races without party labels: anyone may run, the party only shapes the person's positions.
NONPARTISAN: frozenset[RaceType] = frozenset({RaceType.SCHOOL_BOARD, RaceType.WATER_BOARD})

DEFAULT_MIN_AGE: dict[str, int] = {
    "school_board": 18,
    "water_board": 18,
    "council": 18,
    "mayor": 21,
    "house": 25,
    "senate": 30,
    "governor": 30,
}
#: How likely a running person picks each kind of race (local offices are easier to get on).
DEFAULT_WEIGHTS: dict[str, float] = {
    "school_board": 3.0,
    "council": 2.0,
    "water_board": 1.0,
    "mayor": 1.5,
    "house": 1.5,
    "senate": 0.6,
    "governor": 0.5,
}


# --------------------------------------------------------------------------- file schema
def _check_kinds(v: Mapping[str, object] | Sequence[str] | None) -> None:
    bad = sorted(set(v or ()) - set(OFFICE_KINDS))
    if bad:
        raise ValueError(f"unknown office kind(s) {bad}; use {sorted(OFFICE_KINDS)}")


class PeopleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_age: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_MIN_AGE))
    weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    @field_validator("min_age", "weights")
    @classmethod
    def _kinds(cls, v: dict) -> dict:
        _check_kinds(v)
        return v


class PersonEntry(BaseModel):
    """One person of ``config/people.yaml``."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    key: str | None = Field(None, pattern=r"^[a-z0-9][a-z0-9\-]*$")
    gender: Literal["F", "M", "X"] | None = None
    born: int | date | None = None
    home: str
    party: str | None = None
    chance: float | Literal["never", "rarely", "sometimes", "often", "always"] = "sometimes"
    quality: float = Field(0.0, ge=-3, le=3)
    offices: list[str] | None = None
    ideology: Ideology | None = None
    bio: str | None = None

    @field_validator("offices")
    @classmethod
    def _offices(cls, v: list[str] | None) -> list[str] | None:
        _check_kinds(v)
        return v

    @field_validator("chance")
    @classmethod
    def _chance(cls, v: float | str) -> float | str:
        if isinstance(v, float | int) and not 0 <= float(v) <= 1:
            raise ValueError("chance must be a word (never … always) or a number from 0 to 1")
        return v

    @field_validator("born")
    @classmethod
    def _born(cls, v: int | date | None) -> int | date | None:
        if isinstance(v, int) and not 1900 <= v <= 2100:
            raise ValueError("born must be a year (e.g. 1990) or a date (1990-05-17)")
        return v

    @model_validator(mode="after")
    def _names(self) -> PersonEntry:
        if not self.name and not (self.first_name and self.last_name):
            raise ValueError("give a name (or first_name and last_name)")
        return self


class PeopleFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: PeopleSettings = Field(default_factory=PeopleSettings)
    people: list[PersonEntry] = Field(default_factory=list)


# --------------------------------------------------------------------------- resolved people
@dataclass(frozen=True)
class Person:
    key: str
    first_name: str
    last_name: str
    gender: str | None
    birth_date: date | None
    home: str  # CBS municipality code
    home_province: str
    home_name: str
    party: str | None
    chance: float
    quality: float
    offices: frozenset[RaceType] | None
    ideology: Ideology | None
    bio: str | None

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def age_on(self, on: date) -> int | None:
        b = self.birth_date
        if b is None:
            return None
        return on.year - b.year - ((on.month, on.day) < (b.month, b.day))

    def spec(self, party_ideology: Mapping[str, Sequence[float]]) -> CandidateSpec:
        """The person as a candidate: their own positions, else their party's with a small
        personal twist (stable per person), else the centre."""
        ideo = self.ideology
        if ideo is None:
            base = party_ideology.get(self.party or "", (0.0, 0.0, 0.0))
            rng = make_rng(0, "person-ideology", self.key)
            x = np.clip(np.asarray(base, dtype=float) + rng.normal(0.0, 0.15, size=3), -1.0, 1.0)
            ideo = Ideology(
                economic=round(float(x[0]), 3), social=round(float(x[1]), 3), europe=round(float(x[2]), 3)
            )
        return CandidateSpec(
            key=self.key,
            first_name=self.first_name,
            last_name=self.last_name,
            gender=None if self.gender == "X" else self.gender,
            birth_date=self.birth_date,
            party=self.party,
            home_municipality=self.home,
            home_province=self.home_province,
            quality=self.quality,
            ideology=ideo,
            bio=self.bio or f"From {self.home_name} (config/people.yaml).",
        )


@dataclass(frozen=True)
class PeopleConfig:
    people: tuple[Person, ...]
    min_age: dict[RaceType, int]
    weights: dict[RaceType, float]
    path: str = ""

    def __bool__(self) -> bool:
        return bool(self.people)

    def by_key(self) -> dict[str, Person]:
        return {p.key: p for p in self.people}


@dataclass(frozen=True)
class PeopleSlot:
    """A race people may run in: key, race type and the municipalities of its area."""

    key: str
    race_type: RaceType
    municipalities: frozenset[str]


@dataclass
class PeopleAssignment:
    """Who runs where at one election: race key → people (as candidate specs)."""

    races: dict[str, list[CandidateSpec]] = field(default_factory=dict)

    def get(self, race_key: str) -> list[CandidateSpec]:
        return self.races.get(race_key, [])

    @property
    def keys(self) -> set[str]:
        return {c.key for cs in self.races.values() for c in cs}

    def specs(self) -> list[CandidateSpec]:
        return [c for cs in self.races.values() for c in cs]

    def __len__(self) -> int:
        return sum(len(v) for v in self.races.values())


# --------------------------------------------------------------------------- loading
def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def resolve_municipality(frame: GeographyFrame, text: str) -> str:
    """CBS code of a municipality given by code (``GM0855``) or name (``Tilburg``, case and
    punctuation ignored; ``Bergen (NH.)`` also matches ``bergen nh``)."""
    t = text.strip()
    if re.fullmatch(r"(?i)GM\d{4}", t):
        code = t.upper()
        if frame.muni_index_or_none(code) is None:
            raise ScenarioError(f"people.yaml: no municipality with code {code}")
        return code
    want = _norm(t)
    names = {_norm(n): frame.muni_codes[i] for i, n in enumerate(frame.muni_names)}
    if want in names:
        return names[want]
    starts = [code for n, code in names.items() if n.split(" (")[0] == want or n.startswith(want + " ")]
    if len(starts) == 1:
        return starts[0]
    close = difflib.get_close_matches(want, list(names), n=3, cutoff=0.6)
    hint = (
        f" (did you mean {', '.join(repr(frame.muni_names[frame.muni_index(names[c])]) for c in close)}?)"
        if close
        else ""
    )
    raise ScenarioError(f"people.yaml: unknown municipality {text!r}{hint}")


def _split_name(e: PersonEntry) -> tuple[str, str]:
    if e.first_name and e.last_name:
        return e.first_name.strip(), e.last_name.strip()
    parts = str(e.name).strip().split()
    if len(parts) < 2:
        raise ScenarioError(f"people.yaml: {e.name!r} needs a first and a last name")
    return parts[0], " ".join(parts[1:])


def _person(e: PersonEntry, frame: GeographyFrame) -> Person:
    from app.simulation.candidates import slugify

    first, last = _split_name(e)
    home = resolve_municipality(frame, e.home)
    m = frame.muni_index(home)
    born = e.born
    birth = date(born, 7, 1) if isinstance(born, int) else born
    chance = CHANCES[e.chance] if isinstance(e.chance, str) else float(e.chance)
    return Person(
        key=e.key or f"person-{slugify(f'{first} {last}')}",
        first_name=first,
        last_name=last,
        gender=e.gender,
        birth_date=birth,
        home=home,
        home_province=frame.province_codes[int(frame.muni_province[m])],
        home_name=frame.muni_names[m],
        party=e.party.upper() if e.party else None,
        chance=chance,
        quality=float(e.quality),
        offices=frozenset(OFFICE_KINDS[k] for k in e.offices) if e.offices else None,
        ideology=e.ideology,
        bio=e.bio,
    )


def parse_people(text: str, frame: GeographyFrame, *, path: str = "") -> PeopleConfig:
    """Validate a people file's text against the geography."""
    import yaml

    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ScenarioError(f"people.yaml is not valid YAML: {exc}") from exc
    try:
        doc = PeopleFile.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in exc.errors()[:8]
        )
        raise ScenarioError(f"people.yaml: {problems}") from exc
    people = [_person(e, frame) for e in doc.people]
    seen: dict[str, str] = {}
    for p in people:
        if p.key in seen:
            raise ScenarioError(
                f"people.yaml: {p.full_name!r} and {seen[p.key]!r} share the key {p.key!r}; give one of them a `key:`"
            )
        seen[p.key] = p.full_name
    min_age = {OFFICE_KINDS[k]: v for k, v in {**DEFAULT_MIN_AGE, **doc.settings.min_age}.items()}
    weights = {OFFICE_KINDS[k]: v for k, v in {**DEFAULT_WEIGHTS, **doc.settings.weights}.items()}
    return PeopleConfig(tuple(people), min_age, weights, path)


_cache: dict[tuple[str, float, int], PeopleConfig] = {}


def people_path() -> Path:
    return config_path(PEOPLE_FILE)


def load_people(frame: GeographyFrame, path: str | Path | None = None) -> PeopleConfig:
    """The people of ``config/people.yaml`` (cached until the file changes); empty when the file
    does not exist."""
    p = Path(path) if path is not None else people_path()
    if not p.exists():
        return PeopleConfig((), {}, {}, str(p))
    key = (str(p.resolve()), p.stat().st_mtime, id(frame))
    hit = _cache.get(key)
    if hit is None:
        hit = parse_people(p.read_text(encoding="utf-8"), frame, path=str(p))
        _cache.clear()
        _cache[key] = hit
        if hit.people:
            log.info("loaded %d custom people from %s", len(hit.people), p)
    return hit


# --------------------------------------------------------------------------- assignment
def assign_people(
    config: PeopleConfig,
    slots: Sequence[PeopleSlot],
    *,
    seed: int,
    on: date,
    parties: Mapping[str, Sequence[float]],
    exclude: set[str] | frozenset[str] = frozenset(),
    held_lines: set[tuple[str, str | None]] | frozenset[tuple[str, str | None]] = frozenset(),
) -> PeopleAssignment:
    """Decide who of ``config`` runs where at one election.

    ``slots``: the races of the ballot people may run in; ``parties``: the parties of the election
    (code → ideology) — a person whose party is not among them only runs in board races;
    ``exclude``: candidate keys that cannot be assigned (incumbents running in this election and
    people already on the ballot); ``held_lines``: ``(race, party)`` lines held by the race's
    incumbent (a party fields no challenger against its own incumbent; ``None`` = independent).
    """
    out = PeopleAssignment()
    if not config.people or not slots:
        return out
    taken: set[tuple[str, str | None]] = set(held_lines)  # (race, party): one person per party line
    for person in sorted(config.people, key=lambda p: p.key):
        if person.key in exclude or person.chance <= 0:
            continue
        rng = make_rng(seed, "people", person.key)
        if rng.random() >= person.chance:
            continue
        age = person.age_on(on)
        has_party = person.party is not None and person.party in parties
        eligible: list[PeopleSlot] = []
        for s in slots:
            rt = s.race_type
            if rt not in config.weights or person.home not in s.municipalities:
                continue
            if person.offices is not None and rt not in person.offices:
                continue
            if age is not None and age < config.min_age.get(rt, 18):
                continue
            if rt not in NONPARTISAN and person.party is not None and not has_party:
                continue  # their party is not in this election: board races only
            line = None if rt in NONPARTISAN else (person.party if has_party else None)
            if rt not in NONPARTISAN and (s.key, line) in taken:
                continue
            eligible.append(s)
        if not eligible:
            continue
        w = np.array([config.weights[s.race_type] for s in eligible], dtype=float)
        if w.sum() <= 0:
            continue
        pick = eligible[int(rng.choice(len(eligible), p=w / w.sum()))]
        spec = person.spec(parties)
        if pick.race_type not in NONPARTISAN and not has_party:
            spec = spec.model_copy(update={"party": None})  # runs as an independent
        taken.add((pick.key, None if pick.race_type in NONPARTISAN else spec.party))
        out.races.setdefault(pick.key, []).append(spec)
    if len(out):
        log.info("%d custom people run in this election", len(out))
    return out


def slots_from_units(
    frame: GeographyFrame, items: Sequence[tuple[str, RaceType | str, np.ndarray | Sequence[int]]]
) -> list[PeopleSlot]:
    """People slots of races given as ``(key, race type, unit indices)``."""
    out = []
    for key, rt, units in items:
        u = np.asarray(units, dtype=np.int64)
        munis = (
            frozenset(frame.muni_codes[int(m)] for m in np.unique(frame.unit_muni[u]))
            if len(u)
            else frozenset()
        )
        out.append(PeopleSlot(str(key), RaceType(rt), munis))
    return out


__all__ = [
    "CHANCES",
    "OFFICE_KINDS",
    "PeopleAssignment",
    "PeopleConfig",
    "PeopleSlot",
    "Person",
    "assign_people",
    "load_people",
    "parse_people",
    "people_path",
    "resolve_municipality",
    "slots_from_units",
]
