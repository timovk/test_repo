"""Senate (Eerste Kamer) seats and their staggered classes.

Each province elects ``senators_per_province`` senators (canonically 2 → 24 seats) for six-year
terms in ``senate_classes`` staggered classes (canonically 3 classes of 8 seats; one class is
elected every two years).  A province's seats are always in *different* classes, so no province
elects both senators in the same election (U.S. Art. I §3 analogue).

The canonical mapping is configured explicitly in ``config/senate.yaml``: the provinces are
partitioned into three groups of four whose seat pairs are classes {1, 2}, {2, 3} and {1, 3}.  When
no (matching) configuration is available, :func:`round_robin_classes` generates a deterministic
assignment for any constitution.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import get_constitution, load_config
from app.core.constitution import ConstitutionConfig
from app.core.errors import ConfigError, ValidationError
from app.core.logging import get_logger

log = get_logger(__name__)


class SenateGroup(BaseModel):
    """Provinces sharing the same pair of Senate classes."""

    model_config = ConfigDict(extra="forbid")

    classes: list[int] = Field(..., min_length=1, description="class of seat 1, seat 2, …")
    provinces: list[str] = Field(default_factory=list)
    description: str | None = None


class SenateConfig(BaseModel):
    """Schema of ``config/senate.yaml``."""

    model_config = ConfigDict(extra="forbid")

    #: Named groups of provinces → their seats' classes.  Seat numbers follow list order.
    groups: dict[str, SenateGroup] = Field(default_factory=dict)
    #: What to do when the groups do not cover exactly the given provinces:
    #: ``round_robin`` (deterministic generator) or ``error``.
    fallback: str = Field("round_robin", pattern="^(round_robin|error)$")

    @field_validator("groups")
    @classmethod
    def _unique_provinces(cls, v: dict[str, SenateGroup]) -> dict[str, SenateGroup]:
        seen: Counter[str] = Counter(p for g in v.values() for p in g.provinces)
        dup = sorted(p for p, n in seen.items() if n > 1)
        if dup:
            raise ValueError(f"provinces listed in more than one Senate group: {dup}")
        return v

    def mapping(self) -> dict[str, tuple[int, ...]]:
        """Province code → classes of its seats (seat 1, seat 2, …)."""
        return {p: tuple(int(c) for c in g.classes) for g in self.groups.values() for p in g.provinces}


def load_senate_config(name: str = "senate.yaml") -> SenateConfig:
    """Load ``config/senate.yaml`` (empty configuration → round-robin fallback)."""
    return load_config(name, SenateConfig, optional=True)


def senate_seat_code(province: str, n: int) -> str:
    """Seat / race code of a province's ``n``-th Senate seat: ``senate_seat_code('NB', 1) == 'SEN-NB-1'``."""
    if n < 1:
        raise ValueError("Senate seat numbers start at 1")
    return f"SEN-{province}-{n}"


def round_robin_classes(
    province_codes: Sequence[str], constitution: ConstitutionConfig | None = None
) -> dict[str, tuple[int, ...]]:
    """Deterministic fallback: deal the seats out over the classes in province order.

    Seat ``j`` of the ``i``-th province gets class ``(i × s + j) mod c + 1`` (``s`` senators per
    province, ``c`` classes); each province's classes are then sorted so seat 1 has the lowest
    class.  Because ``s ≤ c`` a province never has two seats in one class, and when ``c`` divides
    the number of seats every class has exactly ``seats / c`` members.
    """
    cons = constitution or get_constitution()
    s, c = cons.senators_per_province, cons.senate_classes
    out: dict[str, tuple[int, ...]] = {}
    for i, p in enumerate(province_codes):
        out[p] = tuple(sorted(((i * s + j) % c) + 1 for j in range(s)))
    return out


def validate_senate_classes(
    mapping: Mapping[str, Sequence[int]],
    province_codes: Sequence[str] | None = None,
    constitution: ConstitutionConfig | None = None,
) -> list[str]:
    """Constitutional checks of a class assignment; returns a list of problems (empty = valid)."""
    cons = constitution or get_constitution()
    problems: list[str] = []
    if province_codes is not None:
        missing = sorted(set(province_codes) - set(mapping))
        extra = sorted(set(mapping) - set(province_codes))
        if missing:
            problems.append(f"provinces without Senate classes: {missing}")
        if extra:
            problems.append(f"Senate classes for unknown provinces: {extra}")
    counts: Counter[int] = Counter()
    for p, classes in mapping.items():
        cl = [int(x) for x in classes]
        if len(cl) != cons.senators_per_province:
            problems.append(f"{p}: {len(cl)} seats, expected {cons.senators_per_province}")
        if len(set(cl)) != len(cl):
            problems.append(f"{p}: two seats in the same class {cl}")
        bad = [x for x in cl if not 1 <= x <= cons.senate_classes]
        if bad:
            problems.append(f"{p}: invalid class numbers {bad}")
        counts.update(cl)
    n_prov = len(province_codes) if province_codes is not None else len(mapping)
    total = n_prov * cons.senators_per_province
    if total % cons.senate_classes == 0:
        per_class = total // cons.senate_classes
        for k in range(1, cons.senate_classes + 1):
            if counts.get(k, 0) != per_class:
                problems.append(f"class {k} has {counts.get(k, 0)} seats, expected {per_class}")
    return problems


def assign_senate_classes(
    province_codes: Sequence[str],
    config: SenateConfig | None = None,
    *,
    constitution: ConstitutionConfig | None = None,
) -> dict[str, tuple[int, int]]:
    """Classes of every province's Senate seats: ``{province: (class_of_seat_1, class_of_seat_2)}``.

    Uses the explicit mapping of ``config`` (default: ``config/senate.yaml``) when it covers exactly
    ``province_codes`` and is constitutionally valid; otherwise the deterministic round-robin
    fallback (or a :class:`~app.core.errors.ConfigError` when ``fallback: error``).

    Raises:
        ValidationError: the resulting assignment violates the constitution.
    """
    cons = constitution or get_constitution()
    cfg = config if config is not None else load_senate_config()
    codes = [str(p) for p in province_codes]
    mapping = cfg.mapping()
    result: dict[str, tuple[int, ...]]
    if mapping and set(mapping) == set(codes):
        problems = validate_senate_classes(mapping, codes, cons)
        if problems:
            raise ConfigError("Invalid Senate class configuration:\n  - " + "\n  - ".join(problems))
        result = {p: mapping[p] for p in codes}
    else:
        if cfg.fallback == "error":
            raise ConfigError("Senate class configuration does not cover exactly the given provinces")
        if mapping:
            log.warning("Senate class configuration does not match the provinces; using round-robin fallback")
        result = round_robin_classes(codes, cons)
    problems = validate_senate_classes(result, codes, cons)
    if problems:
        raise ValidationError("Senate class assignment violates the constitution", problems)
    return result  # type: ignore[return-value]


def seats_by_class(mapping: Mapping[str, Sequence[int]]) -> dict[int, list[str]]:
    """Class → seat codes (``SEN-XX-n``), sorted, for election scheduling and display."""
    out: dict[int, list[str]] = {}
    for p, classes in mapping.items():
        for n, cl in enumerate(classes, start=1):
            out.setdefault(int(cl), []).append(senate_seat_code(p, n))
    return {k: sorted(v) for k, v in sorted(out.items())}
