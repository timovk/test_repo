from __future__ import annotations

import itertools
from datetime import date, timedelta

import pytest

from app.core.constitution import ConstitutionConfig, ElectionType, OfficeType
from app.core.errors import ConfigError, ElectionError
from app.elections.calendar import CalendarConfig, ElectionCalendar, ElectionDayRule, SenateCalendar


@pytest.fixture()
def cal() -> ElectionCalendar:
    return ElectionCalendar(CalendarConfig(), ConstitutionConfig())


def test_from_config_matches_defaults(cal: ElectionCalendar) -> None:
    loaded = ElectionCalendar.from_config()
    assert loaded.founding_year == 2024
    for y in range(2020, 2060):
        assert loaded.cycle(y) == cal.cycle(y)


def test_election_dates_are_wednesdays(cal: ElectionCalendar) -> None:
    assert cal.election_date(2028) == date(2028, 11, 8)
    assert cal.election_date(2024) == date(2024, 11, 6)
    assert cal.election_date(2026) == date(2026, 11, 4)
    assert cal.election_date(2032) == date(2032, 11, 3)  # 1 Nov 2032 is the first Monday
    for y in range(2024, 2100):
        d = cal.election_date(y)
        assert d.weekday() == 2 and d.month == 11 and 3 <= d.day <= 9
        first_monday = d - timedelta(days=2)
        assert first_monday.weekday() == 0 and first_monday.day <= 7


def test_polls_window_local_time(cal: ElectionCalendar) -> None:
    opens, closes = cal.polls_window(2028)
    assert (opens.hour, opens.minute, closes.hour) == (7, 30, 21)
    assert str(closes.tzinfo) == "Europe/Amsterdam"
    assert closes.utcoffset() == timedelta(hours=1)  # CET in November
    c = cal.cycle(2028)
    assert c.polls_close == closes and c.date == date(2028, 11, 8)


def test_founding_election(cal: ElectionCalendar) -> None:
    c = cal.cycle(2024)
    assert c.is_founding and c.election_type is ElectionType.GENERAL
    assert c.president and c.house and c.governors and c.provincial_legislatures
    assert c.senate_classes == (1, 2, 3)
    assert not c.municipal
    assert cal.senate_class_up(2024) is None  # no class's term expires at the founding election
    assert {OfficeType.PRESIDENT, OfficeType.SENATE, OfficeType.GOVERNOR} <= set(c.offices)
    assert cal.term_bounds(OfficeType.SENATE, 2024, 1) == (date(2025, 1, 15), date(2027, 1, 15))
    assert cal.term_bounds(OfficeType.SENATE, 2024, 2) == (date(2025, 1, 15), date(2029, 1, 15))
    assert cal.term_bounds(OfficeType.SENATE, 2024, 3) == (date(2025, 1, 15), date(2031, 1, 15))
    with pytest.raises(ElectionError):
        cal.term_bounds(OfficeType.SENATE, 2024)


def test_senate_rotation_over_decades(cal: ElectionCalendar) -> None:
    assert [cal.senate_class_up(y) for y in (2026, 2028, 2030, 2032, 2034, 2036)] == [1, 2, 3, 1, 2, 3]
    years_up: dict[int, list[int]] = {1: [], 2: [], 3: []}
    for y in range(2025, 2025 + 40):
        classes = cal.senate_classes_up(y)
        if y % 2 == 0:
            assert len(classes) == 1, y  # exactly one class at every regular election after founding
            assert cal.cycle(y).house
        else:
            assert classes == () and not cal.is_election_year(y)
        for c in classes:
            years_up[c].append(y)
    for c, years in years_up.items():
        gaps = {b - a for a, b in itertools.pairwise(years)}
        assert gaps == {6}, (c, years)  # every class up every 6 years
        assert years[0] == 2024 + 2 * c
    assert cal.next_senate_election(1, 2026) == 2032
    assert cal.next_senate_election(3, 2024) == 2030
    for y in (2026, 2032):
        assert cal.term_bounds("SENATE", y) == (date(y + 1, 1, 15), date(y + 7, 1, 15))


def test_office_cycles(cal: ElectionCalendar) -> None:
    pres = [y for y in range(2020, 2045) if cal.cycle(y).president]
    assert pres == [2024, 2028, 2032, 2036, 2040, 2044]
    house = [y for y in range(2020, 2034) if cal.cycle(y).house]
    assert house == [2024, 2026, 2028, 2030, 2032]
    gov = [y for y in range(2020, 2037) if cal.cycle(y).governors]
    assert gov == [2024, 2028, 2032, 2036]
    muni = [y for y in range(2020, 2040) if cal.cycle(y).municipal]
    assert muni == [2026, 2030, 2034, 2038]
    assert cal.cycle(2026).election_type is ElectionType.MIDTERM
    assert cal.cycle(2028).election_type is ElectionType.GENERAL
    assert cal.cycle(2027).election_type is None and not cal.cycle(2027).has_elections
    assert cal.cycle(2020).election_type is None  # before the founding
    assert cal.is_election_year(2030) and not cal.is_election_year(2031)


def test_next_and_upcoming(cal: ElectionCalendar) -> None:
    assert cal.next_election_year(2024) == 2026
    assert cal.next_election_year(2025) == 2026
    assert cal.next_election_year(2000) == 2024
    up = cal.upcoming(4, 2027)
    assert [c.year for c in up] == [2028, 2030, 2032, 2034]
    assert [c.senate_classes for c in up] == [(2,), (3,), (1,), (2,)]
    assert [c.year for c in cal.upcoming(2, 2026)] == [2026, 2028]


def test_term_bounds(cal: ElectionCalendar) -> None:
    assert cal.term_bounds(OfficeType.PRESIDENT, 2028) == (date(2029, 1, 15), date(2033, 1, 15))
    assert cal.term_bounds(OfficeType.VICE_PRESIDENT, 2028) == (date(2029, 1, 15), date(2033, 1, 15))
    assert cal.term_bounds(OfficeType.HOUSE, 2026) == (date(2027, 1, 15), date(2029, 1, 15))
    assert cal.term_bounds(OfficeType.GOVERNOR, 2028) == (date(2029, 1, 1), date(2033, 1, 1))
    assert cal.term_bounds(OfficeType.PROVINCIAL_LEGISLATOR, 2028) == (date(2029, 1, 1), date(2033, 1, 1))
    assert cal.term_bounds(OfficeType.MAYOR, 2026) == (date(2027, 1, 1), date(2031, 1, 1))
    assert cal.term_bounds(OfficeType.COUNCIL_MEMBER, 2030) == (date(2031, 1, 1), date(2035, 1, 1))


def test_special_election_date(cal: ElectionCalendar) -> None:
    # no regular election in 2027 → the 2028 election day
    assert cal.special_election_date(date(2027, 6, 1)) == date(2028, 11, 8)
    # 160 days before election day → that election day
    assert cal.special_election_date(date(2028, 6, 1)) == date(2028, 11, 8)
    # exactly 90 days before → still that election
    assert cal.special_election_date(date(2028, 11, 8) - timedelta(days=90)) == date(2028, 11, 8)
    # 89 days before → the one after
    assert cal.special_election_date(date(2028, 11, 8) - timedelta(days=89)) == date(2030, 11, 6)
    assert cal.special_election_date(date(2028, 12, 1)) == date(2030, 11, 6)
    assert cal.special_election_date(date(2028, 9, 1), min_days=30) == date(2028, 11, 8)


def test_configurable_rules() -> None:
    us = CalendarConfig(election_day=ElectionDayRule(weekday="tuesday"))
    cal = ElectionCalendar(us, ConstitutionConfig())
    assert cal.election_date(2028) == date(2028, 11, 7)
    anchor_only = CalendarConfig(
        election_day=ElectionDayRule(month=3, anchor_weekday="wednesday", weekday=None, anchor_occurrence=3)
    )
    assert ElectionCalendar(anchor_only, ConstitutionConfig()).election_date(2027) == date(2027, 3, 17)
    shifted = CalendarConfig(municipal={"first_year_offset": 0, "every_years": 4})
    assert ElectionCalendar(shifted, ConstitutionConfig()).cycle(2028).municipal


def test_invalid_calendar_rejected() -> None:
    bad = CalendarConfig(senate=SenateCalendar(initial_terms={1: 2, 2: 2, 3: 6}))
    with pytest.raises(ConfigError):
        ElectionCalendar(bad, ConstitutionConfig())
    odd = CalendarConfig(senate=SenateCalendar(initial_terms={1: 3, 2: 4, 3: 6}))
    with pytest.raises(ConfigError):
        ElectionCalendar(odd, ConstitutionConfig())
    missing = CalendarConfig(senate=SenateCalendar(initial_terms={1: 2, 2: 4}))
    with pytest.raises(ConfigError):
        ElectionCalendar(missing, ConstitutionConfig())
    with pytest.raises(ValueError):
        ElectionDayRule(weekday="funday")
