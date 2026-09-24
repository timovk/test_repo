"""Senate classes: 8/8/8, distinct classes per province, config + fallback, seat codes."""

from __future__ import annotations

from collections import Counter

import pytest

from app.core.constitution import SENATE_SEATS, ConstitutionConfig
from app.core.errors import ConfigError
from app.districts.senate import (
    SenateConfig,
    SenateGroup,
    assign_senate_classes,
    load_senate_config,
    round_robin_classes,
    seats_by_class,
    senate_seat_code,
    validate_senate_classes,
)
from app.geography.synthetic import PROVINCES

CODES = [p[0] for p in PROVINCES]


def _check_canonical(mapping: dict[str, tuple[int, ...]]) -> None:
    assert set(mapping) == set(CODES)
    counts = Counter(c for classes in mapping.values() for c in classes)
    assert counts == {1: 8, 2: 8, 3: 8}
    assert sum(counts.values()) == SENATE_SEATS
    for classes in mapping.values():
        assert len(classes) == 2 and len(set(classes)) == 2


def test_default_config_is_valid_and_mixes_sizes() -> None:
    cfg = load_senate_config()
    mapping = assign_senate_classes(CODES, cfg)
    _check_canonical(mapping)
    # the configured grouping: three groups of four with pairs {1,2}, {2,3}, {1,3}
    pairs = Counter(tuple(sorted(v)) for v in mapping.values())
    assert pairs == {(1, 2): 4, (2, 3): 4, (1, 3): 4}
    # the four largest provinces are spread over the three groups
    big = ["ZH", "NH", "NB", "GE"]
    assert len({mapping[p] for p in big}) == 3
    assert validate_senate_classes(mapping, CODES) == []


def test_round_robin_fallback() -> None:
    mapping = round_robin_classes(CODES)
    _check_canonical(mapping)
    # unknown provinces in the config → fallback
    cfg = SenateConfig(groups={"X": SenateGroup(classes=[1, 2], provinces=["AA", "BB"])})
    assert assign_senate_classes(CODES, cfg) == mapping
    # empty config → fallback, deterministic
    assert assign_senate_classes(CODES, SenateConfig()) == assign_senate_classes(CODES, SenateConfig())


def test_fallback_error_mode() -> None:
    with pytest.raises(ConfigError):
        assign_senate_classes(CODES, SenateConfig(fallback="error"))


def test_invalid_config_rejected() -> None:
    groups = {
        "A": SenateGroup(classes=[1, 1], provinces=CODES[:4]),  # both seats in one class
        "B": SenateGroup(classes=[2, 3], provinces=CODES[4:8]),
        "C": SenateGroup(classes=[1, 3], provinces=CODES[8:]),
    }
    with pytest.raises(ConfigError):
        assign_senate_classes(CODES, SenateConfig(groups=groups))
    unbalanced = {
        "A": SenateGroup(classes=[1, 2], provinces=CODES[:6]),
        "B": SenateGroup(classes=[2, 3], provinces=CODES[6:]),
    }
    with pytest.raises(ConfigError):
        assign_senate_classes(CODES, SenateConfig(groups=unbalanced))
    with pytest.raises(ValueError):
        SenateConfig(
            groups={
                "A": SenateGroup(classes=[1, 2], provinces=["NB"]),
                "B": SenateGroup(classes=[2, 3], provinces=["NB"]),
            }
        )


def test_alternative_constitution() -> None:
    cons = ConstitutionConfig(
        province_count=12, senators_per_province=3, senate_classes=3, senate_term_years=6
    )
    mapping = assign_senate_classes(CODES, SenateConfig(), constitution=cons)
    counts = Counter(c for v in mapping.values() for c in v)
    assert counts == {1: 12, 2: 12, 3: 12}
    assert all(len(set(v)) == 3 for v in mapping.values())


def test_seat_codes_and_class_lists() -> None:
    assert senate_seat_code("NB", 1) == "SEN-NB-1"
    with pytest.raises(ValueError):
        senate_seat_code("NB", 0)
    by_class = seats_by_class(assign_senate_classes(CODES))
    assert sorted(by_class) == [1, 2, 3]
    assert all(len(v) == 8 for v in by_class.values())
    assert len({s for v in by_class.values() for s in v}) == SENATE_SEATS
