"""Calendar of the in-between local elections (FICTIONAL) — ``config/local.yaml``.

Between the big November elections every province holds ``days_per_province`` local election
days a year (e.g. the 2nd Wednesday of March, May, June and September in Gelderland).  Every
municipality and every water board has one fixed *slot* among its province's days, so each has
its own date every year.  A local election is one province on one date; its ballot holds:

* **school boards** (nonpartisan, vote for up to N, the top N win) — staggered 4-year terms: the
  first election fills every seat (half for 2 years, half for 4), afterwards half the seats every
  2 years, in the municipality's even or odd years;
* **water boards** — every 4 years in a board-specific year, all seats at once, in the province
  holding most of the board's voters (``GeographyFrame.water_board_province``);
* **ballot measures** — a Poisson number per municipality and day, drawn from the topic library;
* **special elections and recalls** — :meth:`LocalCalendar.office_events` draws mayor and council
  vacancies and mayor recall petitions; the services decide which of them reach a ballot (an
  office must be held, the regular midterm must not be close, one event per office at a time).

Everything is a pure function of the configuration, the geography and the seed (keyed RNG
streams, :func:`app.core.rng.make_rng`), so dates and ballots never depend on the order in
which elections are created.  Nothing here touches the database.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cached_property

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import load_config
from app.core.rng import make_rng
from app.elections.calendar import WEEKDAYS, ElectionCalendar
from app.geography.frame import GeographyFrame

MONTH_NAMES: tuple[str, ...] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
EVENT_MAYOR_VACANCY = "mayor_vacancy"
EVENT_COUNCIL_VACANCY = "council_vacancy"
EVENT_MAYOR_RECALL = "mayor_recall"


# ============================================================================ configuration
class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BoardConfig(_Cfg):
    """An at-large board (school board, water board)."""

    seats_by_population: list[tuple[int, int]] = Field(default_factory=lambda: [(0, 5)])
    term_years: int = Field(4, ge=2, le=8)
    candidates_per_seat: tuple[float, float] = (1.3, 2.4)
    incumbent_runs_again: float = Field(0.72, ge=0.0, le=1.0)

    @field_validator("seats_by_population")
    @classmethod
    def _brackets(cls, v: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not v or any(s < 1 for _, s in v):
            raise ValueError("seats_by_population needs at least one bracket with seats ≥ 1")
        return sorted((int(p), int(s)) for p, s in v)

    def seats(self, population: float) -> int:
        out = self.seats_by_population[0][1]
        for minimum, seats in self.seats_by_population:
            if population >= minimum:
                out = seats
        return out


class MeasuresConfig(_Cfg):
    rate: float = Field(1.0, ge=0.0)
    #: Logit shift added to every topic's appeal (calibrates the overall pass rate).
    base_appeal: float = 0.0
    population_elasticity: float = Field(0.3, ge=0.0, le=2.0)
    max_per_day: int = Field(5, ge=0, le=26)
    styles: list[str] = Field(default_factory=lambda: ["proposition_number"])

    @field_validator("styles")
    @classmethod
    def _styles(cls, v: list[str]) -> list[str]:
        allowed = {"proposition_number", "measure_letter", "proposition_letter"}
        bad = set(v) - allowed
        if bad or not v:
            raise ValueError(f"measure styles must be among {sorted(allowed)}")
        return v


class VacancyConfig(_Cfg):
    mayor_rate: float = Field(0.03, ge=0.0, le=1.0)
    council_rate: float = Field(0.06, ge=0.0, le=2.0)
    reasons: dict[str, float] = Field(default_factory=lambda: {"resigned": 1.0})
    min_days_before: int = Field(90, ge=0)


class RecallConfig(_Cfg):
    mayor_rate: float = Field(0.012, ge=0.0, le=1.0)
    min_days_before: int = Field(60, ge=0)
    min_days_in_office: int = Field(180, ge=0)
    base_appeal: float = -0.45
    scandal_probability: float = Field(0.35, ge=0.0, le=1.0)
    scandal_appeal: float = 0.65


class Lean(_Cfg):
    economic: float = Field(0.0, ge=-1.0, le=1.0)
    social: float = Field(0.0, ge=-1.0, le=1.0)
    europe: float = Field(0.0, ge=-1.0, le=1.0)

    def vector(self) -> tuple[float, float, float]:
        return (self.economic, self.social, self.europe)


class MeasureTopic(_Cfg):
    """A FICTIONAL ballot measure template (``config/local.yaml`` → ``topics``)."""

    key: str
    kind: str = Field(pattern="^(tax|bond|charter|zoning|policy)$")
    title: str
    summary: str
    lean: Lean = Field(default_factory=Lean)
    appeal: float = 0.0
    threshold: float = Field(0.5, gt=0.0, lt=1.0)
    weight: float = Field(1.0, gt=0.0)
    min_population: int = Field(0, ge=0)


class LocalConfig(_Cfg):
    """Schema of ``config/local.yaml``."""

    seed: int = 1848
    data_category: str = "FICTIONAL"
    days_per_province: int = Field(4, ge=1, le=12)
    months: list[int] = Field(default_factory=lambda: [2, 3, 4, 5, 6, 9])
    occurrences: list[int] = Field(default_factory=lambda: [1, 2, 3])
    weekday: str = "wednesday"
    polls_close: str = Field("21:00", pattern=r"^\d{2}:\d{2}$")
    turnout_logit_shift: float = -1.25
    term_start_days: int = Field(45, ge=0, le=365)
    school_boards: BoardConfig = Field(default_factory=BoardConfig)
    water_boards: BoardConfig = Field(default_factory=lambda: BoardConfig(seats_by_population=[(0, 7)]))
    measures: MeasuresConfig = Field(default_factory=MeasuresConfig)
    vacancies: VacancyConfig = Field(default_factory=VacancyConfig)
    recalls: RecallConfig = Field(default_factory=RecallConfig)
    regular_election_window_days: int = Field(200, ge=0)
    topics: list[MeasureTopic] = Field(default_factory=list)

    @field_validator("months")
    @classmethod
    def _months(cls, v: list[int]) -> list[int]:
        if not v or any(m < 1 or m > 12 for m in v) or len(set(v)) != len(v):
            raise ValueError("months must be distinct month numbers 1–12")
        return sorted(v)

    @field_validator("occurrences")
    @classmethod
    def _occ(cls, v: list[int]) -> list[int]:
        if not v or any(o < 1 or o > 4 for o in v):
            raise ValueError("occurrences must be in 1–4")
        return sorted(set(v))

    @field_validator("weekday")
    @classmethod
    def _weekday(cls, v: str) -> str:
        if v.lower() not in WEEKDAYS:
            raise ValueError(f"unknown weekday {v!r}")
        return v.lower()

    @model_validator(mode="after")
    def _enough_months(self) -> LocalConfig:
        if self.days_per_province > len(self.months):
            raise ValueError("days_per_province cannot exceed the number of allowed months")
        keys = [t.key for t in self.topics]
        if len(set(keys)) != len(keys):
            raise ValueError("measure topic keys must be unique")
        return self


def load_local_config() -> LocalConfig:
    """``config/local.yaml`` (defaults when the file is missing)."""
    return load_config("local.yaml", LocalConfig, optional=True)


# ============================================================================ plan objects
@dataclass(frozen=True)
class SlotRule:
    """One local election day of a province: the ``occurrence``-th weekday of ``month``."""

    month: int
    occurrence: int


@dataclass(frozen=True)
class LocalDay:
    """A province's local election day.  The province days of one date are held together as one
    local election (one combined election night)."""

    province_code: str
    slot: int
    date: date


@dataclass(frozen=True)
class BoardSeatClass:
    """Seats elected together: seat numbers (1-based) and the length of the term they win."""

    seats: tuple[int, ...]
    term_years: int


@dataclass(frozen=True)
class BoardCycle:
    """A due board election: cycle number (0 = the first ever), seats up and terms."""

    number: int
    total_seats: int
    classes: tuple[BoardSeatClass, ...]

    @property
    def seats_up(self) -> tuple[int, ...]:
        return tuple(s for c in self.classes for s in c.seats)

    @property
    def first(self) -> bool:
        return self.number == 0


@dataclass(frozen=True)
class MeasurePlan:
    """A ballot measure on a municipality's local day (FICTIONAL)."""

    municipality_code: str
    label: str  # 'Proposition 1' | 'Measure A' | 'Proposition A'
    suffix: str  # race-code suffix: '1' | 'A'
    topic: MeasureTopic


@dataclass(frozen=True)
class OfficeEvent:
    """A drawn vacancy or recall petition of a municipal office (before the holder checks)."""

    kind: str  # EVENT_MAYOR_VACANCY | EVENT_COUNCIL_VACANCY | EVENT_MAYOR_RECALL
    municipality_code: str
    event_date: date
    election_date: date  # the municipality's local day that would hold the special / recall
    reason: str  # resigned | died | appointed_elsewhere | removed | petition | scandal
    seq: int = 0  # running number of the event within (municipality, kind, year)
    details: dict = field(default_factory=dict, compare=False)


# ============================================================================ the calendar
class LocalCalendar:
    """Local election days, slots and ballots for one geography (see the module docstring)."""

    def __init__(
        self,
        frame: GeographyFrame,
        config: LocalConfig | None = None,
        calendar: ElectionCalendar | None = None,
    ) -> None:
        self.frame = frame
        self.config = config or load_local_config()
        self.calendar = calendar or ElectionCalendar.from_config()
        self.seed = int(self.config.seed)
        self.founding_date = self.calendar.election_date(self.calendar.founding_year)
        self._weekday = WEEKDAYS.index(self.config.weekday)

    # ------------------------------------------------------------------ slots
    @cached_property
    def province_slots(self) -> dict[str, list[SlotRule]]:
        """Province code → its ``days_per_province`` day rules, in calendar order."""
        out: dict[str, list[SlotRule]] = {}
        cfg = self.config
        for pv in self.frame.province_codes:
            rng = make_rng(self.seed, "local-days", pv)
            months = sorted(rng.choice(cfg.months, size=cfg.days_per_province, replace=False).tolist())
            out[pv] = [SlotRule(int(m), int(rng.choice(cfg.occurrences))) for m in months]
        return out

    @cached_property
    def muni_slot(self) -> dict[str, int]:
        """Municipality code → slot index (balanced within each province: a seeded permutation
        dealt round-robin over the province's days)."""
        out: dict[str, int] = {}
        f = self.frame
        n = self.config.days_per_province
        for p, pv in enumerate(f.province_codes):
            munis = [f.muni_codes[m] for m in f.munis_in_province(p)]
            order = make_rng(self.seed, "local-muni-slots", pv).permutation(len(munis))
            for rank, i in enumerate(order.tolist()):
                out[munis[i]] = rank % n
        return out

    @cached_property
    def school_board_parity(self) -> dict[str, int]:
        """Municipality → 0 (school board elections in even years) or 1 (odd years)."""
        return {
            gm: int(make_rng(self.seed, "school-board-parity", gm).integers(0, 2))
            for gm in self.frame.muni_codes
        }

    @cached_property
    def water_board_slot(self) -> dict[str, int]:
        n = self.config.days_per_province
        return {
            ws: int(make_rng(self.seed, "water-board-slot", ws).integers(0, n))
            for ws in self.frame.water_board_codes
        }

    @cached_property
    def water_board_offset(self) -> dict[str, int]:
        """Water board → years after the founding year of its first election (1 … term)."""
        term = self.config.water_boards.term_years
        return {
            ws: 1 + int(make_rng(self.seed, "water-board-year", ws).integers(0, term))
            for ws in self.frame.water_board_codes
        }

    # ------------------------------------------------------------------ dates
    def day(self, province_code: str, slot: int, year: int) -> date:
        """Date of a province's local day ``slot`` in ``year``."""
        rule = self.province_slots[province_code][slot]
        first = date(year, rule.month, 1)
        d = first + timedelta(days=(self._weekday - first.weekday()) % 7 + 7 * (rule.occurrence - 1))
        if d.month != rule.month:  # pragma: no cover - occurrences ≤ 4 always fit
            d -= timedelta(days=7)
        return d

    def muni_day(self, municipality_code: str, year: int) -> date:
        """The municipality's own local day in ``year``."""
        p = self.frame.province_codes[int(self.frame.muni_province[self.frame.muni_index(municipality_code)])]
        return self.day(p, self.muni_slot[municipality_code], year)

    def next_muni_day(self, municipality_code: str, on_or_after: date) -> date:
        """The municipality's first local day on or after a date."""
        for year in range(on_or_after.year, on_or_after.year + 3):
            d = self.muni_day(municipality_code, year)
            if d >= on_or_after:
                return d
        raise ValueError("no local day found")  # pragma: no cover

    def days(self, start: date, end: date) -> list[LocalDay]:
        """Every province's local days with ``start < date ≤ end`` (and after the founding
        election), ordered by date then province (canonical province order)."""
        out: list[LocalDay] = []
        lo = max(start, self.founding_date)
        for year in range(lo.year, end.year + 1):
            for pv in self.frame.province_codes:
                for slot in range(len(self.province_slots[pv])):
                    d = self.day(pv, slot, year)
                    if lo < d <= end:
                        out.append(LocalDay(pv, slot, d))
        order = {pv: i for i, pv in enumerate(self.frame.province_codes)}
        return sorted(out, key=lambda x: (x.date, order[x.province_code]))

    def dates(self, start: date, end: date) -> dict[date, list[LocalDay]]:
        """Local election dates with ``start < date ≤ end`` → the province days held on each
        (oldest first).  All provinces voting on a date form one local election."""
        out: dict[date, list[LocalDay]] = {}
        for d in self.days(start, end):
            out.setdefault(d.date, []).append(d)
        return out

    def municipalities_on(self, day: LocalDay) -> list[str]:
        """Municipalities whose own local day is ``day`` (canonical order)."""
        f = self.frame
        p = f.province_index(day.province_code)
        return [
            f.muni_codes[m] for m in f.munis_in_province(p) if self.muni_slot[f.muni_codes[m]] == day.slot
        ]

    def water_boards_on(self, day: LocalDay) -> list[str]:
        """Water boards electing their board on ``day`` (held by this province, due this year)."""
        f = self.frame
        p = f.province_index(day.province_code)
        out = []
        for w, ws in enumerate(f.water_board_codes):
            if int(f.water_board_province[w]) != p or self.water_board_slot[ws] != day.slot:
                continue
            if self.water_board_cycle(ws, day.date.year) is not None:
                out.append(ws)
        return out

    def term_start(self, election_day: date) -> date:
        """First day of a term won at a local election."""
        return election_day + timedelta(days=self.config.term_start_days)

    # ------------------------------------------------------------------ boards
    def school_board_seats(self, municipality_code: str) -> int:
        pop = float(self.frame.muni_population[self.frame.muni_index(municipality_code)])
        return self.config.school_boards.seats(pop)

    def school_board_cycle(self, municipality_code: str, year: int) -> BoardCycle | None:
        """The school board election due in ``year`` (None when the municipality has none)."""
        first_year = self.founding_date.year + 1
        parity = self.school_board_parity[municipality_code]
        y0 = first_year if first_year % 2 == parity else first_year + 1
        if year < y0 or (year - y0) % 2:
            return None
        k = (year - y0) // 2
        n = self.school_board_seats(municipality_code)
        term = self.config.school_boards.term_years
        a = tuple(range(1, math.ceil(n / 2) + 1))
        b = tuple(range(math.ceil(n / 2) + 1, n + 1))
        if k == 0:
            classes = (BoardSeatClass(a, term), BoardSeatClass(b, max(term // 2, 1)))
        elif k % 2:
            classes = (BoardSeatClass(b, term),)
        else:
            classes = (BoardSeatClass(a, term),)
        return BoardCycle(k, n, tuple(c for c in classes if c.seats))

    def water_board_seats(self, water_board_code: str) -> int:
        f = self.frame
        w = f.water_board_index(water_board_code)
        if f.unit_water_board is None:
            return self.config.water_boards.seats(0)
        pop = float(f.unit_population[f.unit_water_board == w].sum())
        return self.config.water_boards.seats(pop)

    def water_board_cycle(self, water_board_code: str, year: int) -> BoardCycle | None:
        term = self.config.water_boards.term_years
        y0 = self.founding_date.year + self.water_board_offset[water_board_code]
        if year < y0 or (year - y0) % term:
            return None
        n = self.water_board_seats(water_board_code)
        return BoardCycle((year - y0) // term, n, (BoardSeatClass(tuple(range(1, n + 1)), term),))

    # ------------------------------------------------------------------ measures
    def measure_style(self, municipality_code: str) -> str:
        styles = self.config.measures.styles
        return styles[int(make_rng(self.seed, "measure-style", municipality_code).integers(0, len(styles)))]

    def measures(self, municipality_code: str, on: date) -> list[MeasurePlan]:
        """Ballot measures of a municipality on its local day ``on``."""
        cfg = self.config.measures
        topics = self.config.topics
        if not topics or cfg.rate <= 0 or cfg.max_per_day <= 0:
            return []
        pop = float(self.frame.muni_population[self.frame.muni_index(municipality_code)])
        rng = make_rng(self.seed, "measures", municipality_code, on.isoformat())
        lam = cfg.rate * (max(pop, 1.0) / 50_000.0) ** cfg.population_elasticity
        n = min(int(rng.poisson(lam)), cfg.max_per_day)
        eligible = [t for t in topics if pop >= t.min_population]
        n = min(n, len(eligible))
        if n == 0:
            return []
        w = np.array([t.weight for t in eligible], dtype=float)
        picks = rng.choice(len(eligible), size=n, replace=False, p=w / w.sum())
        style = self.measure_style(municipality_code)
        out = []
        for i, j in enumerate(sorted(int(x) for x in picks)):
            letter = chr(ord("A") + i)
            if style == "proposition_number":
                label, suffix = f"Proposition {i + 1}", str(i + 1)
            elif style == "measure_letter":
                label, suffix = f"Measure {letter}", letter
            else:
                label, suffix = f"Proposition {letter}", letter
            out.append(MeasurePlan(municipality_code, label, suffix, eligible[j]))
        return out

    # ------------------------------------------------------------------ vacancies and recalls
    def office_events(self, until: date) -> list[OfficeEvent]:
        """See :meth:`_office_events` (cached per year: the draws never change)."""
        cache = self.__dict__.setdefault("_event_cache", {})
        year_events = cache.get(until.year)
        if year_events is None:
            year_events = cache[until.year] = self._office_events(date(until.year, 12, 31))
        return [e for e in year_events if e.event_date <= until]

    def _office_events(self, until: date) -> list[OfficeEvent]:
        """Every drawn mayor / council vacancy and mayor recall petition from the founding
        election up to ``until`` (event date), with the local day that would hold the special
        election or recall.  The services filter them (office held, regular election not close,
        one pending event per office), so the draw itself never depends on the database."""
        vac, rec = self.config.vacancies, self.config.recalls
        reasons = list(vac.reasons)
        rw = np.array([vac.reasons[r] for r in reasons], dtype=float)
        rw = rw / rw.sum() if rw.sum() > 0 else np.full(len(reasons), 1.0 / max(len(reasons), 1))
        out: list[OfficeEvent] = []
        start_year = self.founding_date.year
        for gm in self.frame.muni_codes:
            for year in range(start_year, until.year + 1):
                for kind, rate, lead in (
                    (EVENT_MAYOR_VACANCY, vac.mayor_rate, vac.min_days_before),
                    (EVENT_COUNCIL_VACANCY, vac.council_rate, vac.min_days_before),
                    (EVENT_MAYOR_RECALL, rec.mayor_rate, rec.min_days_before),
                ):
                    if rate <= 0:
                        continue
                    rng = make_rng(self.seed, "office-events", kind, gm, year)
                    n = int(rng.poisson(rate))
                    days_in_year = (date(year + 1, 1, 1) - date(year, 1, 1)).days
                    for seq in range(n):
                        d = date(year, 1, 1) + timedelta(days=int(rng.integers(0, days_in_year)))
                        if d <= self.founding_date or d > until:
                            continue
                        if kind == EVENT_MAYOR_RECALL:
                            scandal = bool(rng.random() < rec.scandal_probability)
                            reason = "scandal" if scandal else "petition"
                        else:
                            reason = reasons[int(rng.choice(len(reasons), p=rw))] if reasons else "resigned"
                        when = self.next_muni_day(gm, d + timedelta(days=lead))
                        out.append(OfficeEvent(kind, gm, d, when, reason, seq))
        return sorted(out, key=lambda e: (e.event_date, e.municipality_code, e.kind, e.seq))

    # ------------------------------------------------------------------ names
    @staticmethod
    def format_date(d: date) -> str:
        return f"{d.day} {MONTH_NAMES[d.month - 1]} {d.year}"

    def election_name(self, on: date) -> str:
        """E.g. ``"Local Elections · 17 March 2027"`` (every province voting that day)."""
        return f"Local Elections · {self.format_date(on)}"
