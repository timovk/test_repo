"""Chamber arithmetic: seat counts, control, net change and proportional seat allocation.

* :func:`house_composition` / :func:`chamber_composition` — seats per party from single-member
  winners; :func:`senate_composition` merges holdover senators with newly elected ones.
* :func:`chamber_control` — controlling party (absolute majority) or a hung chamber, with
  minimal-winning coalition hints (the fictional system is multiparty, so hung chambers are
  normal).
* :func:`dhondt`, :func:`sainte_lague` (highest averages) and :func:`largest_remainder` (Hare
  quota) — deterministic proportional allocation; exact ties are decided by seeded lot, as the
  real Dutch Kieswet does for equal averages.
* :func:`municipal_council_size` and :func:`provincial_legislature_size` — the REAL statutory
  size brackets of the Gemeentewet (art. 8) and Provinciewet (art. 8), applied here to the
  FICTIONAL municipal councils and provincial legislatures.

Independent winners (``party_code=None``, e.g. after a party disappeared) are counted under the
key :data:`INDEPENDENT`, which can never collide with a party code (party codes are upper-case).
"""

from __future__ import annotations

import itertools
from bisect import bisect_left
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

import numpy as np

from app.core.errors import ElectionError
from app.core.logging import get_logger
from app.core.rng import stable_choice_order

log = get_logger(__name__)

#: Composition key for members elected without a party (independents / dissolved parties).
INDEPENDENT = "independent"

# --------------------------------------------------------------------------- statutory sizes
#: Gemeentewet art. 8 (REAL): (maximum inhabitants, council seats); above the last bracket: 45.
#: Source: Gemeentewet, artikel 8 lid 1 (wetten.overheid.nl, BWBR0005416).  The statutory
#: population is the CBS count on 1 January of the year before the election (art. 8 lid 2).
MUNICIPAL_COUNCIL_BRACKETS: tuple[tuple[int, int], ...] = (
    (3_000, 9),
    (6_000, 11),
    (10_000, 13),
    (15_000, 15),
    (20_000, 17),
    (25_000, 19),
    (30_000, 21),
    (35_000, 23),
    (40_000, 25),
    (45_000, 27),
    (50_000, 29),
    (60_000, 31),
    (70_000, 33),
    (80_000, 35),
    (100_000, 37),
    (200_000, 39),
)
MUNICIPAL_COUNCIL_MAX = 45

#: Provinciewet art. 8 (REAL): (maximum inhabitants, Provinciale Staten seats); above: 55.
#: Source: Provinciewet, artikel 8 lid 1 (wetten.overheid.nl, BWBR0005645; nine brackets, verified
#: verbatim), cross-checked against the real Staten sizes elected in 2023 (Zeeland 39, Flevoland 41,
#: Groningen/Fryslân/Drenthe 43, Overijssel/Limburg 47, Utrecht 49, the four provinces above
#: 2 million 55; 572 seats in total).
#: Note: the brackets 500,000 / 750,000 / 1,250,000 / 1,750,000 are statutory; a simplified
#: 7-bracket table (≤600k: 41, ≤800k: 43, …, ≤2M: 49) is NOT the law and would give Groningen 41
#: and Utrecht 47 instead of their real 43 and 49 seats.
PROVINCIAL_LEGISLATURE_BRACKETS: tuple[tuple[int, int], ...] = (
    (400_000, 39),
    (500_000, 41),
    (750_000, 43),
    (1_000_000, 45),
    (1_250_000, 47),
    (1_500_000, 49),
    (1_750_000, 51),
    (2_000_000, 53),
)
PROVINCIAL_LEGISLATURE_MAX = 55


def _bracket_size(population: int, brackets: tuple[tuple[int, int], ...], above: int) -> int:
    if population < 0:
        raise ValueError("population must be non-negative")
    limits = [lim for lim, _ in brackets]
    i = bisect_left(limits, int(population))  # first bracket whose maximum is ≥ population
    return brackets[i][1] if i < len(brackets) else above


def municipal_council_size(population: int) -> int:
    """Council seats for a municipality of ``population`` inhabitants (Gemeentewet art. 8).

    ≤3,000: 9 · ≤6,000: 11 · ≤10,000: 13 · ≤15,000: 15 · ≤20,000: 17 · ≤25,000: 19 · ≤30,000: 21 ·
    ≤35,000: 23 · ≤40,000: 25 · ≤45,000: 27 · ≤50,000: 29 · ≤60,000: 31 · ≤70,000: 33 ·
    ≤80,000: 35 · ≤100,000: 37 · ≤200,000: 39 · >200,000: 45.
    """
    return _bracket_size(population, MUNICIPAL_COUNCIL_BRACKETS, MUNICIPAL_COUNCIL_MAX)


def provincial_legislature_size(population: int) -> int:
    """Provincial legislature seats for ``population`` inhabitants (Provinciewet art. 8 lid 1).

    ≤400,000: 39 · ≤500,000: 41 · ≤750,000: 43 · ≤1,000,000: 45 · ≤1,250,000: 47 ·
    ≤1,500,000: 49 · ≤1,750,000: 51 · ≤2,000,000: 53 · >2,000,000: 55.
    """
    return _bracket_size(population, PROVINCIAL_LEGISLATURE_BRACKETS, PROVINCIAL_LEGISLATURE_MAX)


#: Alias used in docs/ARCHITECTURE.md §6.
council_size = municipal_council_size


# --------------------------------------------------------------------------- composition
def _party_key(party: str | None) -> str:
    return INDEPENDENT if party is None else str(party)


def _sorted_counts(counts: Mapping[str, int]) -> dict[str, int]:
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def chamber_composition(seat_winner_party: Mapping[str, str | None] | Iterable[str | None]) -> dict[str, int]:
    """Seats per party from a mapping ``seat → party code`` (or an iterable of party codes).

    ``None`` counts as :data:`INDEPENDENT`.  The result is sorted by seats (desc), then key.
    """
    parties = seat_winner_party.values() if isinstance(seat_winner_party, Mapping) else seat_winner_party
    counts: dict[str, int] = {}
    for p in parties:
        k = _party_key(p)
        counts[k] = counts.get(k, 0) + 1
    return _sorted_counts(counts)


def house_composition(district_winner_party: Mapping[str, str | None]) -> dict[str, int]:
    """House seats per party from ``district code → winning party code`` (None = independent).

    ``None`` means an *elected* member without a party; leave undecided seats (e.g. a tie still
    being recounted) out of the mapping — :func:`chamber_control` then reports them as vacant.
    """
    return chamber_composition(district_winner_party)


def merge_senate_seats(
    holdovers: Mapping[str, str | None], results: Mapping[str, str | None]
) -> dict[str, str | None]:
    """Seat map of the new Senate: holdover senators plus the seats just elected.

    Raises :class:`ElectionError` if a seat appears in both (a seat is either up or held over).
    """
    overlap = sorted(set(holdovers) & set(results))
    if overlap:
        raise ElectionError(f"Senate seats both held over and elected: {overlap}")
    merged: dict[str, str | None] = dict(holdovers)
    merged.update(results)
    return dict(sorted(merged.items()))


def senate_composition(
    holdovers: Mapping[str, str | None], results: Mapping[str, str | None]
) -> dict[str, int]:
    """Senate seats per party after an election: holdovers (``seat → party``) + results."""
    return chamber_composition(merge_senate_seats(holdovers, results))


def net_change(current: Mapping[str, int], previous: Mapping[str, int]) -> dict[str, int]:
    """Seat change per party (``current − previous``) over the union of parties.

    Parties absent from one side count as 0 there.  Sorted by current seats (desc), then key.
    """
    keys = set(current) | set(previous)
    order = sorted(keys, key=lambda k: (-int(current.get(k, 0)), k))
    return {k: int(current.get(k, 0)) - int(previous.get(k, 0)) for k in order}


@dataclass(frozen=True)
class ControlSummary:
    """Who controls a chamber.

    ``controlling_party`` holds an absolute majority (``None`` = hung chamber).  ``seats_short``
    is how many seats the largest party lacks for a majority (0 when it controls).
    ``coalition_hints`` lists minimal winning coalitions (every member needed), fewest parties
    first, then fewest seats — descriptive arithmetic only, not a prediction.
    """

    total: int
    majority: int
    filled: int
    composition: dict[str, int]
    controlling_party: str | None
    largest_party: str | None
    largest_seats: int
    largest_tied: bool
    seats_short: int
    coalition_hints: list[tuple[str, ...]] = field(default_factory=list)

    @property
    def vacant(self) -> int:
        return self.total - self.filled

    @property
    def hung(self) -> bool:
        return self.controlling_party is None

    @property
    def label(self) -> str:
        if self.controlling_party is not None:
            return f"{self.controlling_party} control ({self.largest_seats}/{self.total})"
        if self.largest_party is None:
            return "No members"
        return f"No majority — {self.largest_party} largest ({self.largest_seats}, {self.seats_short} short of {self.majority})"


def minimal_winning_coalitions(
    composition: Mapping[str, int], majority: int, max_parties: int = 12, limit: int | None = None
) -> list[tuple[str, ...]]:
    """Minimal winning coalitions of parties (independents excluded).

    A coalition is winning when its seats reach ``majority`` and minimal when dropping any member
    makes it lose.  Only the ``max_parties`` largest parties are considered (bounded search).
    Ordered by number of parties, then total seats (ascending), then party keys.
    """
    parties = sorted(
        ((k, int(v)) for k, v in composition.items() if k != INDEPENDENT and v > 0),
        key=lambda kv: (-kv[1], kv[0]),
    )[:max_parties]
    found: list[tuple[int, int, tuple[str, ...]]] = []
    for size in range(1, len(parties) + 1):
        for combo in itertools.combinations(parties, size):
            seats = sum(s for _, s in combo)
            if seats < majority:
                continue
            if all(seats - s < majority for _, s in combo):
                found.append((size, seats, tuple(k for k, _ in combo)))
    found.sort(key=lambda t: (t[0], t[1], t[2]))
    out = [t[2] for t in found]
    return out[:limit] if limit is not None else out


def chamber_control(
    composition: Mapping[str, int], total: int, majority: int, max_hints: int = 5
) -> ControlSummary:
    """Summarise control of a chamber with ``total`` seats needing ``majority`` for control.

    Use ``constitution.house_seats / house_majority`` (150 / 76) or ``senate_seats /
    senate_majority`` (24 / 13).  Independents never "control" a chamber.
    """
    comp = _sorted_counts({k: int(v) for k, v in composition.items() if int(v) > 0})
    filled = sum(comp.values())
    if filled > total:
        raise ElectionError(f"composition has {filled} seats, chamber has {total}")
    parties = [(k, v) for k, v in comp.items() if k != INDEPENDENT]
    largest, largest_seats, tied = None, 0, False
    if parties:
        largest, largest_seats = parties[0]
        tied = len(parties) > 1 and parties[1][1] == largest_seats
    controlling = largest if largest is not None and largest_seats >= majority else None
    hints = [(controlling,)] if controlling else minimal_winning_coalitions(comp, majority, limit=max_hints)
    return ControlSummary(
        total=int(total),
        majority=int(majority),
        filled=filled,
        composition=comp,
        controlling_party=controlling,
        largest_party=largest,
        largest_seats=int(largest_seats),
        largest_tied=tied,
        seats_short=max(0, int(majority) - int(largest_seats)),
        coalition_hints=hints,
    )


# --------------------------------------------------------------------------- proportional allocation
def _eligible_parties(votes: Mapping[str, int], threshold: float) -> tuple[list[str], np.ndarray, int]:
    keys = list(votes)
    v = np.array([int(votes[k]) for k in keys], dtype=np.int64)
    if (v < 0).any():
        raise ElectionError("negative votes in seat allocation")
    total = int(v.sum())
    if total <= 0:
        raise ElectionError("cannot allocate seats without votes")
    if not 0.0 <= threshold < 1.0:
        raise ValueError("threshold must be a share in [0, 1)")
    passes = (v > 0) & (v * 1.0 >= threshold * total)
    if not passes.any():
        raise ElectionError("no party reaches the threshold")
    return keys, np.where(passes, v, 0), total


def _lot_rank(keys: list[str], tie_seed: int | None, lot_key: str) -> np.ndarray:
    order = stable_choice_order(keys, 0 if tie_seed is None else int(tie_seed), "seat-lot", lot_key)
    rank = {k: i for i, k in enumerate(order)}
    return np.array([rank[k] for k in keys], dtype=np.int64)


def highest_averages(
    votes: Mapping[str, int],
    seats: int,
    divisor: Callable[[np.ndarray], np.ndarray],
    threshold: float = 0.0,
    tie_seed: int | None = None,
    lot_key: str = "",
) -> dict[str, int]:
    """Generic highest-averages allocation with divisors ``divisor(k)`` for k = 0, 1, 2, ….

    Equivalent to awarding seats one at a time to the largest average ``votes / divisor(seats
    won)``; exact equal averages competing for the last seat(s) are decided by seeded lot.
    Parties with a vote share below ``threshold`` (0–1 of all votes) receive no seats.
    """
    if seats < 0:
        raise ValueError("seats must be non-negative")
    keys, v, _ = _eligible_parties(votes, threshold)
    if seats == 0:
        return dict.fromkeys(keys, 0)
    d = np.asarray(divisor(np.arange(seats, dtype=float)), dtype=float)
    quot = v[:, None] / d[None, :]  # (P, S) — correctly rounded, so equal rationals compare equal
    quot[v == 0, :] = -1.0
    party = np.repeat(np.arange(len(keys)), seats)
    rnd = np.tile(np.arange(seats), len(keys))
    lot = _lot_rank(keys, tie_seed, lot_key)[party]
    flat = quot.reshape(-1)
    order = np.lexsort((rnd, lot, -flat))
    winners = order[:seats]
    if flat[winners].min() < 0:
        raise ElectionError("more seats than can be allocated to eligible parties")
    alloc = np.bincount(party[winners], minlength=len(keys))
    if seats < len(flat):
        last, nxt = flat[order[seats - 1]], flat[order[seats]]
        if last == nxt:
            log.info("equal averages for the last seat decided by lot", extra={"ctx": {"lot_key": lot_key}})
    return {k: int(a) for k, a in zip(keys, alloc, strict=True)}


def dhondt(
    votes: Mapping[str, int],
    seats: int,
    threshold: float = 0.0,
    tie_seed: int | None = None,
    lot_key: str = "",
) -> dict[str, int]:
    """D'Hondt (Jefferson) allocation — divisors 1, 2, 3, … (used for Dutch councils/Staten)."""
    return highest_averages(votes, seats, lambda k: k + 1.0, threshold, tie_seed, lot_key)


def sainte_lague(
    votes: Mapping[str, int],
    seats: int,
    threshold: float = 0.0,
    tie_seed: int | None = None,
    lot_key: str = "",
    first_divisor: float = 1.0,
) -> dict[str, int]:
    """Sainte-Laguë (Webster) allocation — divisors 1, 3, 5, … (``first_divisor`` 1.4 = modified)."""

    def div(k: np.ndarray) -> np.ndarray:
        out = 2.0 * k + 1.0
        out[k == 0] = first_divisor
        return out

    return highest_averages(votes, seats, div, threshold, tie_seed, lot_key)


def largest_remainder(
    votes: Mapping[str, int],
    seats: int,
    threshold: float = 0.0,
    tie_seed: int | None = None,
    lot_key: str = "",
) -> dict[str, int]:
    """Largest-remainder (Hamilton, Hare quota) allocation with exact integer arithmetic.

    Each party first gets ``floor(votes × seats / total)``; the remaining seats go to the
    largest remainders (equal remainders: more votes first, then seeded lot).
    """
    if seats < 0:
        raise ValueError("seats must be non-negative")
    keys, v, _ = _eligible_parties(votes, threshold)
    total = int(v.sum())
    if seats == 0:
        return dict.fromkeys(keys, 0)
    num = v * int(seats)
    base = num // total
    rem = num % total
    left = int(seats - base.sum())
    lot = _lot_rank(keys, tie_seed, lot_key)
    eligible = v > 0
    order = [i for i in np.lexsort((lot, -v, -rem)) if eligible[i]]
    for i in order[:left]:
        base[i] += 1
    return {k: int(a) for k, a in zip(keys, base, strict=True)}
