"""Apportionment of House seats (and therefore electoral votes) among provinces.

The (FICTIONAL) constitution allocates the Tweede Kamer's single-member seats among the provinces
in proportion to their (REAL) population, with a guaranteed minimum per province, exactly like
Article I §2 of the U.S. Constitution.  Each province's electoral votes are its House seats plus
its senators (``ConstitutionConfig.senators_per_province``).

Supported methods (see docs/DISTRICTING.md):

* ``huntington_hill`` — equal proportions, the U.S. method since 1941: priority ``P / sqrt(n(n+1))``
* ``webster`` / ``sainte_lague`` — major fractions: divisor ``n + 0.5``
* ``jefferson`` / ``dhondt`` — greatest divisors: divisor ``n + 1``
* ``adams`` — smallest divisors: divisor ``n`` (requires a minimum of at least one seat)
* ``hamilton`` — largest remainders (Hare quota), honouring minimum seats

Every divisor method starts from the minimum seats and hands out the remaining seats one at a time
to the province with the highest priority value.  Priorities (and Hamilton's remainders) are
*compared* exactly in rational arithmetic — floating-point rounding never decides a seat — and exact
ties are broken deterministically by population (descending) and then province code (ascending).
The float priorities in :class:`PrioritySeat` are for reporting only.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

import pandas as pd

from app.core.config import get_constitution
from app.core.constitution import ConstitutionConfig
from app.core.errors import ApportionmentError
from app.core.logging import get_logger

log = get_logger(__name__)

#: Canonical method names and their accepted aliases.
METHOD_ALIASES: dict[str, str] = {
    "huntington_hill": "huntington_hill",
    "huntington-hill": "huntington_hill",
    "equal_proportions": "huntington_hill",
    "webster": "webster",
    "sainte_lague": "webster",
    "sainte-lague": "webster",
    "jefferson": "jefferson",
    "dhondt": "jefferson",
    "d'hondt": "jefferson",
    "adams": "adams",
    "hamilton": "hamilton",
    "largest_remainder": "hamilton",
    "hare": "hamilton",
}

#: Divisor ``d(n)`` for the step from ``n`` to ``n + 1`` seats (priority = population / d(n)).
DIVISORS: dict[str, Callable[[int], float]] = {
    "huntington_hill": lambda n: math.sqrt(n * (n + 1)),
    "webster": lambda n: n + 0.5,
    "jefferson": lambda n: n + 1.0,
    "adams": lambda n: float(n),
}

METHODS: tuple[str, ...] = ("huntington_hill", "webster", "jefferson", "adams", "hamilton")


def _exact_priority(method: str, population: int, n: int) -> tuple[int, Fraction]:
    """Exact sort key of the priority ``population / d(n)`` (smaller key = higher priority).

    ``(0, 0)`` stands for an infinite priority (``d(n) = 0``); otherwise ``(1, −q)`` where ``q`` is a
    rational number that is monotone in the priority (Huntington-Hill compares squared priorities,
    ``P² / (n(n+1))``, so no square root is ever taken).
    """
    p = int(population)
    if p <= 0:
        return (1, Fraction(0))
    if method == "huntington_hill":
        den = n * (n + 1)
        return (0, Fraction(0)) if den == 0 else (1, -Fraction(p * p, den))
    if method == "webster":
        return (1, -Fraction(2 * p, 2 * n + 1))
    if method == "jefferson":
        return (1, -Fraction(p, n + 1))
    if method == "adams":
        return (0, Fraction(0)) if n == 0 else (1, -Fraction(p, n))
    raise ApportionmentError(f"{method!r} is not a divisor method")


def canonical_method(method: str) -> str:
    """Normalise a method name or alias (case-insensitive) to its canonical name."""
    key = str(method).strip().lower()
    if key not in METHOD_ALIASES:
        raise ApportionmentError(
            f"Unknown apportionment method {method!r}; choose one of {sorted(METHOD_ALIASES)}"
        )
    return METHOD_ALIASES[key]


@dataclass(frozen=True)
class PrioritySeat:
    """One seat in the priority sequence (or one of the "first out" seats after the last one)."""

    rank: int  #: national seat number (1-based) in the order seats were awarded
    province: str
    province_seat: int  #: this is the province's n-th seat
    priority: float  #: priority value (divisor methods) or fractional remainder (Hamilton)


@dataclass
class ApportionmentResult:
    """Outcome of :func:`apportion`.  All mappings are keyed by province code (input order)."""

    method: str
    total_seats: int
    min_seats: int
    senators_per_province: int
    total_population: int
    province_codes: list[str]
    populations: dict[str, int]
    seats: dict[str, int]
    quotas: dict[str, float]  #: exact proportional share ``P_i × S / P``
    persons_per_seat: dict[str, float]
    electoral_votes: dict[str, int]
    #: Seats awarded beyond the minimum, in award order.
    priority_order: list[PrioritySeat] = field(default_factory=list)
    #: The next seats that *would* be awarded if the House had more seats ("first provinces out").
    first_out: list[PrioritySeat] = field(default_factory=list)

    @property
    def total_electoral_votes(self) -> int:
        return int(sum(self.electoral_votes.values()))

    @property
    def senate_seats(self) -> int:
        return self.senators_per_province * len(self.province_codes)

    @property
    def average_persons_per_seat(self) -> float:
        return self.total_population / self.total_seats if self.total_seats else 0.0

    def to_frame(self) -> pd.DataFrame:
        """One row per province: population, quota, seats, EV, persons per seat."""
        return pd.DataFrame(
            {
                "province_code": self.province_codes,
                "population": [self.populations[c] for c in self.province_codes],
                "quota": [self.quotas[c] for c in self.province_codes],
                "seats": [self.seats[c] for c in self.province_codes],
                "electoral_votes": [self.electoral_votes[c] for c in self.province_codes],
                "persons_per_seat": [self.persons_per_seat[c] for c in self.province_codes],
            }
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable representation (API / export)."""
        return {
            "method": self.method,
            "total_seats": self.total_seats,
            "min_seats": self.min_seats,
            "senators_per_province": self.senators_per_province,
            "total_population": self.total_population,
            "total_electoral_votes": self.total_electoral_votes,
            "provinces": self.to_frame().to_dict(orient="records"),
            "priority_order": [vars(s) for s in self.priority_order],
            "first_out": [vars(s) for s in self.first_out],
        }


def apportion(
    populations: Mapping[str, int],
    seats: int,
    method: str = "huntington_hill",
    min_seats: int = 1,
    *,
    constitution: ConstitutionConfig | None = None,
    n_first_out: int = 5,
) -> ApportionmentResult:
    """Apportion ``seats`` House seats among provinces in proportion to ``populations``.

    Args:
        populations: province code → (non-negative integer) population.
        seats: total number of House seats (e.g. ``constitution.house_seats``).
        method: one of :data:`METHODS` or an alias (``sainte_lague``, ``dhondt`` …).
        min_seats: guaranteed seats per province.
        constitution: source of ``senators_per_province`` (default: the active constitution).
        n_first_out: how many "next seats" to report after the last awarded seat.

    Raises:
        ApportionmentError: unknown method, invalid populations, or an infeasible minimum.
    """
    cons = constitution or get_constitution()
    meth = canonical_method(method)
    codes = [str(c) for c in populations]
    if not codes:
        raise ApportionmentError("No provinces to apportion seats among")
    if len(set(codes)) != len(codes):
        raise ApportionmentError("Duplicate province codes")
    pops: dict[str, int] = {}
    for c in codes:
        v = populations[c]
        try:
            ok = v is not None and math.isfinite(v) and int(v) == v and v >= 0
        except (TypeError, ValueError, OverflowError):
            ok = False
        if not ok:
            raise ApportionmentError(f"Population of {c} must be a non-negative integer, got {v!r}")
        pops[c] = int(v)
    total_pop = sum(pops.values())
    if total_pop <= 0:
        raise ApportionmentError("Total population must be positive")
    seats = int(seats)
    min_seats = int(min_seats)
    if seats <= 0:
        raise ApportionmentError("Number of seats must be positive")
    if min_seats < 0:
        raise ApportionmentError("min_seats must be >= 0")
    if seats < min_seats * len(codes):
        raise ApportionmentError(
            f"{seats} seats cannot give {len(codes)} provinces at least {min_seats} seat(s) each"
        )
    if meth == "adams" and min_seats < 1:
        raise ApportionmentError("Adams' method requires a minimum of at least one seat per province")

    if meth == "hamilton":
        alloc, order, first_out = _hamilton(pops, codes, seats, min_seats, n_first_out)
    else:
        alloc, order, first_out = _divisor(pops, codes, seats, min_seats, meth, n_first_out)

    quotas = {c: pops[c] * seats / total_pop for c in codes}
    pps = {c: (pops[c] / alloc[c]) if alloc[c] else float("inf") for c in codes}
    senators = cons.senators_per_province
    ev = {c: alloc[c] + senators for c in codes}
    result = ApportionmentResult(
        method=meth,
        total_seats=seats,
        min_seats=min_seats,
        senators_per_province=senators,
        total_population=total_pop,
        province_codes=codes,
        populations=pops,
        seats=alloc,
        quotas=quotas,
        persons_per_seat=pps,
        electoral_votes=ev,
        priority_order=order,
        first_out=first_out,
    )
    _validate(result)
    log.debug("apportioned %d seats by %s", seats, meth)
    return result


# --------------------------------------------------------------------------- divisor methods
def _divisor(
    pops: dict[str, int],
    codes: list[str],
    seats: int,
    min_seats: int,
    method: str,
    n_first_out: int,
) -> tuple[dict[str, int], list[PrioritySeat], list[PrioritySeat]]:
    divisor: Callable[[int], float] = DIVISORS[method]
    alloc = {c: min_seats for c in codes}

    def priority(c: str) -> float:
        """Reported (float) priority of the next seat of ``c``."""
        p = pops[c]
        if p <= 0:
            return 0.0
        d = divisor(alloc[c])
        return math.inf if d <= 0 else p / d

    def key(c: str) -> tuple[tuple[int, Fraction], int, str]:
        # exact priority, then population (descending), then code: never decided by float rounding
        return (_exact_priority(method, pops[c], alloc[c]), -pops[c], c)

    heap = [key(c) for c in codes]
    heapq.heapify(heap)
    order: list[PrioritySeat] = []
    first_out: list[PrioritySeat] = []
    awarded = min_seats * len(codes)
    while awarded < seats + n_first_out:
        _, _, c = heapq.heappop(heap)
        awarded += 1
        seat = PrioritySeat(rank=awarded, province=c, province_seat=alloc[c] + 1, priority=priority(c))
        alloc[c] += 1
        if awarded <= seats:
            order.append(seat)
        else:
            first_out.append(seat)
        heapq.heappush(heap, key(c))
    # undo the hypothetical "first out" seats
    for s in first_out:
        alloc[s.province] -= 1
    return alloc, order, first_out


# --------------------------------------------------------------------------- Hamilton
def _hamilton(
    pops: dict[str, int], codes: list[str], seats: int, min_seats: int, n_first_out: int
) -> tuple[dict[str, int], list[PrioritySeat], list[PrioritySeat]]:
    """Largest remainders (Hare quota) with a guaranteed minimum.

    Provinces whose Hamilton allocation falls below the minimum are fixed at the minimum and the
    remaining seats are re-apportioned among the other provinces until nobody is below it.  Quotas
    and remainders are exact fractions (ties: population descending, then code).
    """
    fixed: set[str] = set()
    while True:
        free = [c for c in codes if c not in fixed]
        seats_free = seats - min_seats * len(fixed)
        pop_free = sum(pops[c] for c in free)
        if pop_free > 0:
            quotas = {c: Fraction(pops[c] * seats_free, pop_free) for c in free}
        else:
            quotas = {c: Fraction(seats_free, len(free)) for c in free}
        base = {c: math.floor(quotas[c]) for c in free}
        rem = {c: quotas[c] - base[c] for c in free}
        leftovers = seats_free - sum(base.values())
        ranked = sorted(free, key=lambda c: (-rem[c], -pops[c], c))
        alloc = {c: (min_seats if c in fixed else base[c]) for c in codes}
        for c in ranked[:leftovers]:
            alloc[c] += 1
        below = [c for c in free if alloc[c] < min_seats]
        if not below:
            break
        fixed.update(below)
    first_rank = seats - leftovers
    order = [
        PrioritySeat(rank=first_rank + i + 1, province=c, province_seat=alloc[c], priority=float(rem[c]))
        for i, c in enumerate(ranked[:leftovers])
    ]
    first_out = [
        PrioritySeat(rank=seats + i + 1, province=c, province_seat=alloc[c] + 1, priority=float(rem[c]))
        for i, c in enumerate(ranked[leftovers : leftovers + n_first_out])
    ]
    return alloc, order, first_out


# --------------------------------------------------------------------------- validation
def _validate(result: ApportionmentResult) -> None:
    problems: list[str] = []
    total = sum(result.seats.values())
    if total != result.total_seats:
        problems.append(f"seats sum to {total}, expected {result.total_seats}")
    below = [c for c, s in result.seats.items() if s < result.min_seats]
    if below:
        problems.append(f"provinces below the minimum of {result.min_seats}: {below}")
    expected_ev = result.total_seats + result.senate_seats
    if result.total_electoral_votes != expected_ev:
        problems.append(f"electoral votes sum to {result.total_electoral_votes}, expected {expected_ev}")
    if problems:
        raise ApportionmentError("Invalid apportionment: " + "; ".join(problems))


def compare_methods(
    populations: Mapping[str, int],
    seats: int,
    min_seats: int = 1,
    methods: tuple[str, ...] = METHODS,
    *,
    constitution: ConstitutionConfig | None = None,
) -> pd.DataFrame:
    """Seats per province under several methods side by side (rows = provinces)."""
    cols: dict[str, list[int]] = {}
    codes = list(populations)
    for m in methods:
        r = apportion(populations, seats, m, min_seats, constitution=constitution)
        cols[m] = [r.seats[c] for c in codes]
    df = pd.DataFrame(cols, index=pd.Index(codes, name="province_code"))
    df.insert(0, "population", [int(populations[c]) for c in codes])
    return df
