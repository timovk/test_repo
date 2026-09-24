"""Election calendar of the (FICTIONAL) federal republic.

Configured by ``config/calendar.yaml`` (:class:`CalendarConfig`); unspecified cycle lengths and
terms default to the constitution (``config/constitution.yaml``).

Canonical calendar:

* **Founding general election** in ``founding_year`` (2024) elects the President, the entire
  House, *all* senators and all governors (and the provincial legislatures).  Senate classes
  receive initial terms of 2, 4 and 6 years (class ``c`` → ``c × house term``), like the first
  U.S. Senate, so that afterwards exactly one class is up at every regular election.
* **Election day**: the Wednesday after the first Monday of November (Dutch elections are held
  on Wednesdays); polls open 07:30 and close 21:00 Europe/Amsterdam.
* **President** every 4 years (2024, 2028, …), **House** every 2 years, **Senate** class ``c``
  whenever its term ends (2026 → class 1, 2028 → class 2, 2030 → class 3, 2032 → class 1, …),
  **governors** and **provincial legislatures** in presidential years (4-year terms),
  **municipal** (mayors and councils) every 4 years offset by 2 (2026, 2030, …).
* **Terms** begin on 15 January after the election for federal offices and on 1 January for
  provincial and municipal offices.
* **Special elections** for vacancies are held on the next regular election day that is at
  least 90 days after the vacancy occurs, otherwise on the one after.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import get_constitution, load_config
from app.core.constitution import ConstitutionConfig, ElectionType, OfficeType
from app.core.errors import ConfigError, ElectionError
from app.core.logging import get_logger

log = get_logger(__name__)

WEEKDAYS: tuple[str, ...] = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class _Cfg(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ElectionDayRule(_Cfg):
    """Election day = the first ``weekday`` strictly after the ``anchor_occurrence``-th
    ``anchor_weekday`` of ``month`` (``weekday: null`` = the anchor day itself)."""

    month: int = Field(11, ge=1, le=12)
    anchor_weekday: str = "monday"
    anchor_occurrence: int = Field(1, ge=1, le=4)
    weekday: str | None = "wednesday"

    @field_validator("anchor_weekday", "weekday")
    @classmethod
    def _weekday(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.lower()
        if v not in WEEKDAYS:
            raise ValueError(f"unknown weekday {v!r}")
        return v


class OfficeCycle(_Cfg):
    """Regular elections in ``founding_year + first_year_offset + k × every_years`` (k ≥ 0).

    ``every_years`` / ``term_years`` default to the constitution's term for the office.
    """

    first_year_offset: int = Field(0, ge=0)
    every_years: int | None = Field(None, ge=1)
    term_years: int | None = Field(None, ge=1)


class SenateCalendar(_Cfg):
    """Senate staggering.  ``initial_terms`` maps class → length (years) of the term won at the
    founding election; default ``c × house term``.  ``term_years`` defaults to the constitution."""

    term_years: int | None = Field(None, ge=1)
    initial_terms: dict[int, int] | None = None


class TermStart(_Cfg):
    """Terms begin on ``month``/``day`` of ``election_year + years_after_election``."""

    years_after_election: int = Field(1, ge=0)
    month: int = Field(1, ge=1, le=12)
    day: int = Field(1, ge=1, le=31)


class SpecialElectionRule(_Cfg):
    min_days_before: int = Field(90, ge=0)


def _default_term_start() -> dict[str, TermStart]:
    return {
        "federal": TermStart(years_after_election=1, month=1, day=15),
        "provincial": TermStart(years_after_election=1, month=1, day=1),
        "municipal": TermStart(years_after_election=1, month=1, day=1),
    }


class CalendarConfig(_Cfg):
    """Schema of ``config/calendar.yaml``."""

    founding_year: int = Field(2024, ge=1800, le=2500)
    timezone: str = "Europe/Amsterdam"
    election_day: ElectionDayRule = Field(default_factory=ElectionDayRule)
    polls_open: str = Field("07:30", pattern=r"^\d{2}:\d{2}$")
    polls_close: str = Field("21:00", pattern=r"^\d{2}:\d{2}$")
    president: OfficeCycle = Field(default_factory=OfficeCycle)
    house: OfficeCycle = Field(default_factory=OfficeCycle)
    senate: SenateCalendar = Field(default_factory=SenateCalendar)
    governors: OfficeCycle = Field(default_factory=OfficeCycle)
    provincial_legislatures: OfficeCycle = Field(default_factory=OfficeCycle)
    municipal: OfficeCycle = Field(default_factory=lambda: OfficeCycle(first_year_offset=2))
    term_start: dict[str, TermStart] = Field(default_factory=_default_term_start)
    special_elections: SpecialElectionRule = Field(default_factory=SpecialElectionRule)

    @field_validator("term_start")
    @classmethod
    def _levels(cls, v: dict[str, TermStart]) -> dict[str, TermStart]:
        merged = _default_term_start()
        unknown = set(v) - set(merged)
        if unknown:
            raise ValueError(f"unknown term_start levels {sorted(unknown)} (federal|provincial|municipal)")
        merged.update(v)
        return merged


@dataclass(frozen=True)
class CycleContents:
    """What is on the ballot in one year's regular election."""

    year: int
    date: date
    election_type: ElectionType | None  # None: no regular election this year
    president: bool
    house: bool
    senate_classes: tuple[int, ...]
    governors: bool
    municipal: bool
    provincial_legislatures: bool
    is_founding: bool
    polls_open: datetime
    polls_close: datetime

    @property
    def has_elections(self) -> bool:
        return self.election_type is not None

    @property
    def offices(self) -> list[OfficeType]:
        """Office types regularly elected this year."""
        out: list[OfficeType] = []
        if self.president:
            out += [OfficeType.PRESIDENT, OfficeType.VICE_PRESIDENT]
        if self.house:
            out.append(OfficeType.HOUSE)
        if self.senate_classes:
            out.append(OfficeType.SENATE)
        if self.governors:
            out += [OfficeType.GOVERNOR, OfficeType.LIEUTENANT_GOVERNOR]
        if self.provincial_legislatures:
            out.append(OfficeType.PROVINCIAL_LEGISLATOR)
        if self.municipal:
            out += [OfficeType.MAYOR, OfficeType.COUNCIL_MEMBER]
        return out


_FEDERAL = {OfficeType.PRESIDENT, OfficeType.VICE_PRESIDENT, OfficeType.HOUSE, OfficeType.SENATE}
_PROVINCIAL = {OfficeType.GOVERNOR, OfficeType.LIEUTENANT_GOVERNOR, OfficeType.PROVINCIAL_LEGISLATOR}
_MUNICIPAL = {OfficeType.MAYOR, OfficeType.COUNCIL_MEMBER}


@dataclass(frozen=True)
class _Cycle:
    first_year: int
    every: int
    term: int

    def up(self, year: int) -> bool:
        return year >= self.first_year and (year - self.first_year) % self.every == 0


class ElectionCalendar:
    """Resolved election calendar (config + constitution).  All methods are pure and cheap."""

    def __init__(
        self, config: CalendarConfig | None = None, constitution: ConstitutionConfig | None = None
    ) -> None:
        self.config = config or CalendarConfig()
        self.constitution = constitution or get_constitution()
        c, k = self.config, self.constitution
        self.founding_year = c.founding_year
        self.tz = ZoneInfo(c.timezone)

        def cyc(rule: OfficeCycle, default_every: int, default_term: int) -> _Cycle:
            return _Cycle(
                first_year=c.founding_year + rule.first_year_offset,
                every=rule.every_years or default_every,
                term=rule.term_years or rule.every_years or default_term,
            )

        self.president = cyc(c.president, k.presidential_term_years, k.presidential_term_years)
        self.house = cyc(c.house, k.house_term_years, k.house_term_years)
        self.governors = cyc(c.governors, k.governor_term_years, k.governor_term_years)
        self.provincial_legislatures = cyc(
            c.provincial_legislatures, k.governor_term_years, k.governor_term_years
        )
        self.municipal = cyc(c.municipal, k.mayor_term_years, k.mayor_term_years)
        self.senate_term = c.senate.term_years or k.senate_term_years
        self.senate_classes = tuple(range(1, k.senate_classes + 1))
        initial = c.senate.initial_terms or {cls: cls * self.house.every for cls in self.senate_classes}
        if sorted(initial) != list(self.senate_classes):
            raise ConfigError(f"senate.initial_terms must define classes {list(self.senate_classes)}")
        self.senate_initial_terms: dict[int, int] = {int(a): int(b) for a, b in sorted(initial.items())}
        self._open = time.fromisoformat(c.polls_open)
        self._close = time.fromisoformat(c.polls_close)
        problems = self.validate()
        if problems:
            raise ConfigError("Invalid election calendar:\n  - " + "\n  - ".join(problems))

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_config(
        cls, name: str | Path = "calendar.yaml", constitution: ConstitutionConfig | None = None
    ) -> ElectionCalendar:
        """Load ``config/calendar.yaml`` (defaults if absent) against the active constitution."""
        return cls(load_config(name, CalendarConfig, optional=True), constitution)

    def validate(self, horizon_years: int | None = None) -> list[str]:
        """Consistency checks of the calendar (empty list = OK).

        * Terms fixed by the constitution (President, House, Senate, governors, mayors) must equal
          ``config/constitution.yaml`` — the calendar may only choose *when* offices are elected.
        * A single-seat office must be elected exactly as often as its term lasts (no gaps or
          overlapping terms).
        * Every Senate class must come up only in years with a regular House election, and after
          the founding election exactly one class must be up at every regular House election
          (checked over ``horizon_years``, default three full Senate terms).
        """
        problems: list[str] = []
        k = self.constitution
        for name, cyc, const_term in (
            ("president", self.president, k.presidential_term_years),
            ("house", self.house, k.house_term_years),
            ("governors", self.governors, k.governor_term_years),
            ("municipal", self.municipal, k.mayor_term_years),
        ):
            if cyc.term != const_term:
                problems.append(
                    f"{name}: term of {cyc.term} years differs from the constitution ({const_term})"
                )
        for name, cyc in (
            ("president", self.president),
            ("house", self.house),
            ("governors", self.governors),
            ("provincial_legislatures", self.provincial_legislatures),
            ("municipal", self.municipal),
        ):
            if cyc.every != cyc.term:
                problems.append(f"{name}: elected every {cyc.every} years but terms last {cyc.term} years")
        if self.senate_term != k.senate_term_years:
            problems.append(
                f"senate: term of {self.senate_term} years differs from the constitution ({k.senate_term_years})"
            )
        for cls_, term in self.senate_initial_terms.items():
            if term <= 0:
                problems.append(f"Senate class {cls_}: initial term must be positive")
            elif term % self.house.every:
                problems.append(
                    f"Senate class {cls_}: initial term {term} is not a multiple of the House cycle"
                )
        if self.senate_term % self.house.every:
            problems.append("Senate term is not a multiple of the House cycle")
        if self.house.first_year != self.founding_year:
            problems.append("the House must be elected at the founding election")
        if problems:
            return problems
        horizon = horizon_years or 3 * self.senate_term
        for year in range(self.founding_year + 1, self.founding_year + horizon + 1):
            classes = self.senate_classes_up(year)
            if classes and not self.house.up(year):
                problems.append(f"{year}: Senate class up without a regular House election")
            if self.house.up(year) and len(classes) != 1:
                problems.append(f"{year}: {len(classes)} Senate classes up (expected exactly one)")
        try:
            self.election_date(self.founding_year)
        except ValueError as exc:
            problems.append(f"election-day rule: {exc}")
        return problems

    # ------------------------------------------------------------------ dates
    def election_date(self, year: int) -> date:
        """Election day of ``year`` under the configured rule (e.g. 2028 → Wed 8 Nov 2028)."""
        rule = self.config.election_day
        first = date(year, rule.month, 1)
        anchor_wd = WEEKDAYS.index(rule.anchor_weekday)
        anchor = first + timedelta(days=(anchor_wd - first.weekday()) % 7 + 7 * (rule.anchor_occurrence - 1))
        if anchor.month != rule.month:
            raise ValueError(
                f"no occurrence {rule.anchor_occurrence} of {rule.anchor_weekday} in month {rule.month}"
            )
        if rule.weekday is None:
            return anchor
        target = WEEKDAYS.index(rule.weekday)
        return anchor + timedelta(days=(target - anchor.weekday() - 1) % 7 + 1)

    def polls_window(self, year: int | date) -> tuple[datetime, datetime]:
        """Timezone-aware (polls open, polls close) on election day (local time, Europe/Amsterdam)."""
        day = year if isinstance(year, date) else self.election_date(year)
        return datetime.combine(day, self._open, self.tz), datetime.combine(day, self._close, self.tz)

    # ------------------------------------------------------------------ what is up
    def senate_classes_up(self, year: int) -> tuple[int, ...]:
        """Senate classes elected in ``year``: all classes at the founding election, afterwards the
        classes whose term ends (founding + initial term + k × Senate term)."""
        if year < self.founding_year:
            return ()
        if year == self.founding_year:
            return self.senate_classes
        up = []
        for cls_, initial in self.senate_initial_terms.items():
            delta = year - self.founding_year - initial
            if delta >= 0 and delta % self.senate_term == 0:
                up.append(cls_)
        return tuple(up)

    def senate_class_up(self, year: int) -> int | None:
        """The class whose regular term expires in ``year`` (``None`` if none — including the
        founding year, when all classes are elected for the first time; see
        :meth:`senate_classes_up`)."""
        if year <= self.founding_year:
            return None
        classes = self.senate_classes_up(year)
        if len(classes) > 1:
            raise ElectionError(f"{year}: several Senate classes up ({classes})")
        return classes[0] if classes else None

    def next_senate_election(self, senate_class: int, after: int) -> int:
        """First year after ``after`` in which ``senate_class`` is elected."""
        if senate_class not in self.senate_classes:
            raise ElectionError(f"unknown Senate class {senate_class}")
        year = max(after + 1, self.founding_year)
        while senate_class not in self.senate_classes_up(year):
            year += 1
        return year

    def cycle(self, year: int) -> CycleContents:
        """Contents of the regular election in ``year`` (``election_type=None`` if nothing is up)."""
        founded = year >= self.founding_year
        president = founded and self.president.up(year)
        house = founded and self.house.up(year)
        senate = self.senate_classes_up(year)
        governors = founded and self.governors.up(year)
        provleg = founded and self.provincial_legislatures.up(year)
        municipal = founded and self.municipal.up(year)
        if president:
            etype: ElectionType | None = ElectionType.GENERAL
        elif house or senate:
            etype = ElectionType.MIDTERM
        elif governors or provleg:
            etype = ElectionType.PROVINCIAL
        elif municipal:
            etype = ElectionType.MUNICIPAL
        else:
            etype = None
        day = self.election_date(year)
        opens, closes = self.polls_window(day)
        return CycleContents(
            year=year,
            date=day,
            election_type=etype,
            president=president,
            house=house,
            senate_classes=senate,
            governors=governors,
            municipal=municipal,
            provincial_legislatures=provleg,
            is_founding=year == self.founding_year,
            polls_open=opens,
            polls_close=closes,
        )

    def is_election_year(self, year: int) -> bool:
        """Whether any regular election takes place in ``year``."""
        return self.cycle(year).has_elections

    def next_election_year(self, after: int) -> int:
        """First year strictly after ``after`` with a regular election."""
        year = max(after + 1, self.founding_year)
        for _ in range(1000):
            if self.is_election_year(year):
                return year
            year += 1
        raise ElectionError("no election within 1000 years")  # pragma: no cover - validated config

    def upcoming(self, n: int, from_year: int) -> list[CycleContents]:
        """The next ``n`` regular elections in years ≥ ``from_year``."""
        out: list[CycleContents] = []
        year = from_year - 1
        while len(out) < n:
            year = self.next_election_year(year)
            out.append(self.cycle(year))
        return out

    # ------------------------------------------------------------------ terms
    def _term_start(self, office: OfficeType) -> TermStart:
        level = "federal" if office in _FEDERAL else "provincial" if office in _PROVINCIAL else "municipal"
        return self.config.term_start[level]

    def term_years(
        self, office_type: OfficeType | str, election_year: int, senate_class: int | None = None
    ) -> int:
        """Length of the term won at the regular ``election_year`` election."""
        office = OfficeType(office_type)
        if office in (OfficeType.PRESIDENT, OfficeType.VICE_PRESIDENT):
            return self.president.term
        if office is OfficeType.HOUSE:
            return self.house.term
        if office is OfficeType.SENATE:
            if senate_class is not None:
                if int(senate_class) not in self.senate_classes:
                    raise ElectionError(f"unknown Senate class {senate_class}")
                if int(senate_class) not in self.senate_classes_up(election_year):
                    raise ElectionError(
                        f"Senate class {senate_class} is not up in {election_year}; a special election "
                        "fills only the remainder of the current term"
                    )
            if election_year == self.founding_year:
                if senate_class is None:
                    raise ElectionError("the founding Senate term depends on the class; pass senate_class")
                return self.senate_initial_terms[int(senate_class)]
            return self.senate_term
        if office in (OfficeType.GOVERNOR, OfficeType.LIEUTENANT_GOVERNOR):
            return self.governors.term
        if office is OfficeType.PROVINCIAL_LEGISLATOR:
            return self.provincial_legislatures.term
        return self.municipal.term

    def term_bounds(
        self, office_type: OfficeType | str, election_year: int, senate_class: int | None = None
    ) -> tuple[date, date]:
        """(start, end) of the term won at the regular election of ``election_year``.

        Federal terms start 15 January after the election, provincial and municipal terms on
        1 January (configurable); a term ends on the day its successor's term starts.  For the
        Senate, ``senate_class`` is required at the founding election and, when given, must be a
        class that is up in ``election_year`` (special elections serve only the remainder).
        """
        office = OfficeType(office_type)
        ts = self._term_start(office)
        years = self.term_years(office, election_year, senate_class)
        start_year = election_year + ts.years_after_election
        return date(start_year, ts.month, ts.day), date(start_year + years, ts.month, ts.day)

    # ------------------------------------------------------------------ vacancies
    def special_election_date(self, vacancy_date: date, min_days: int | None = None) -> date:
        """Date of the special election filling a vacancy that occurred on ``vacancy_date``.

        The next regular election day at least ``min_days`` (default 90) days after the vacancy;
        if the next one is closer, the one after.  The winner serves the rest of the term.
        """
        need = self.config.special_elections.min_days_before if min_days is None else min_days
        year = max(vacancy_date.year, self.founding_year)
        for _ in range(1000):
            if self.is_election_year(year):
                day = self.election_date(year)
                if (day - vacancy_date).days >= need:
                    return day
            year += 1
        raise ElectionError("no regular election found for the special election")  # pragma: no cover


def get_calendar() -> ElectionCalendar:
    """The active calendar (``config/calendar.yaml`` + ``config/constitution.yaml``)."""
    return ElectionCalendar.from_config()
