"""Electoral College: allocation of the provinces' electoral votes and derived statistics.

Each province casts ``EV = House seats + senators`` electoral votes (174 in total, 88 to win,
see :mod:`app.core.constitution`).  Allocation methods (:class:`EVAllocationMethod`):

* ``WINNER_TAKE_ALL`` (default) — the province's popular-vote plurality winner receives all of
  its electoral votes (Noord-Brabant, 24 EV: A 35.81 % vs B 35.79 % → A 24, B 0).
* ``DISTRICT`` (Maine/Nebraska analogue) — ``senators_per_province`` (2) EV to the province-wide
  winner and 1 EV to the winner of each House district (presidential vote within the district).
* ``PROPORTIONAL`` — the province's EV split among tickets by largest remainder (Hare quota).

An *exact* tie follows ``constitution.province_tie_rule``: ``'lot'`` — a seeded drawing of lots
(the same lot as :func:`app.elections.tabulation.tabulate` if the tabulation already drew one) or
``'contingent'`` — the tied electoral votes are withheld (not cast), which can leave every ticket
short of the majority and send the election to the contingent procedure
(:mod:`app.elections.contingent`).

The majority is ``constitution.presidential_majority`` (88 of the canonical 174) whenever the
map is the whole Electoral College; withheld electoral votes still count in the denominator.  A
map whose total differs from the constitution (hand-built or partial maps) uses
``majority_of(sum of EV)`` unless ``majority`` is passed explicitly — pass ``majority=88`` when
allocating a *partial* map (e.g. the provinces decided so far on election night) so that nobody
"wins" a majority of a subset.

With ``PROPORTIONAL`` the province-wide tie rule never withholds votes: the electoral votes are
split by the vote counts, and a seeded lot only decides an exactly equal remainder for the last
electoral vote (``decided_by='lot'``).

Everything here is descriptive arithmetic over SIMULATED results of a FICTIONAL system.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

import numpy as np

from app.core.config import get_constitution
from app.core.constitution import ConstitutionConfig, EVAllocationMethod, majority_of
from app.core.errors import ElectionError
from app.core.logging import get_logger
from app.core.rng import stable_choice_order
from app.elections.seats import largest_remainder
from app.elections.types import TabulatedRace

log = get_logger(__name__)

DECIDED_POPULAR_VOTE = "popular_vote"
DECIDED_LOT = "lot"
DECIDED_WITHHELD = "withheld"


@dataclass(frozen=True)
class EVAward:
    """Electoral votes awarded to one ticket for one reason within a province."""

    province_code: str
    line_key: str
    electoral_votes: int
    basis: str  # 'province' (WTA) | 'statewide' (DISTRICT) | 'district:<code>' | 'proportional'
    decided_by: str  # 'popular_vote' | 'lot'


@dataclass
class ProvinceEV:
    """Electoral-vote outcome of one province."""

    province_code: str
    electoral_votes: int
    winner: str | None  # province-wide plurality winner (after the tie rule)
    decided_by: str  # how the province's EV were decided: 'popular_vote' | 'lot' | 'withheld'
    tied: bool  # exact province-wide first-place tie
    margin_pct: float  # province-wide winner − runner-up, pp of valid votes
    awards: dict[str, int] = field(default_factory=dict)  # line → EV
    withheld: int = 0


@dataclass
class EVOutcome:
    """Result of the Electoral College.

    ``allocations`` holds ``(province_code, line_key, ev)`` rows (one per province × recipient,
    merged over award bases) for persistence in ``electoral_vote_allocation``.
    ``winner`` is the ticket with at least ``majority`` electoral votes, else ``None`` and
    ``needs_contingent`` is true.  ``ties`` lists provinces (and, for the DISTRICT method,
    district codes) whose contest was an exact first-place tie; ``decided_by`` records per
    province how its electoral votes were decided (``popular_vote`` | ``lot`` | ``withheld``).
    """

    method: EVAllocationMethod
    line_keys: list[str]
    ev_by_line: dict[str, int]
    province_winners: dict[str, str | None]
    allocations: list[tuple[str, str, int]]
    awards: list[EVAward]
    provinces: dict[str, ProvinceEV]
    total_ev: int
    total_allocated: int
    withheld: dict[str, int]
    majority: int
    winner: str | None
    needs_contingent: bool
    ties: list[str]
    decided_by: dict[str, str]

    @property
    def ranking(self) -> list[str]:
        """Line keys by electoral votes (desc); equal EV keep first-appearance order."""
        order = sorted(range(len(self.line_keys)), key=lambda i: (-self.ev_by_line[self.line_keys[i]], i))
        return [self.line_keys[i] for i in order]

    @property
    def leader(self) -> str | None:
        """Unique EV leader (``None`` when the top two are tied, e.g. 87–87)."""
        r = self.ranking
        if not r or self.ev_by_line[r[0]] == 0:
            return None
        if len(r) > 1 and self.ev_by_line[r[1]] == self.ev_by_line[r[0]]:
            return None
        return r[0]

    @property
    def ev_margin(self) -> int:
        """Electoral votes of the first minus the second ticket."""
        return ev_margin(self)

    def to_dict(self) -> dict:
        """JSON-serialisable representation (for audit storage / API payloads)."""
        d = asdict(self)
        d["method"] = str(self.method)
        return d


def _line_union(tabs: Mapping[str, TabulatedRace], order: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for p in order:
        for k in tabs[p].line_keys:
            seen.setdefault(k, None)
    return list(seen)


def _decide(
    tab: TabulatedRace, tie_rule: str, tie_seed: int | None, stream_key: str
) -> tuple[str | None, str, bool]:
    """(winner line key | None, decided_by, tied) under the constitution's tie rule."""
    totals = np.asarray(tab.totals, dtype=np.int64)
    if len(totals) == 0:
        return None, DECIDED_WITHHELD, False
    if len(totals) == 1:
        return tab.line_keys[0], DECIDED_POPULAR_VOTE, False
    top = totals.max()
    tied_idx = [int(i) for i in np.flatnonzero(totals == top)]
    if len(tied_idx) == 1:
        return tab.line_keys[tied_idx[0]], DECIDED_POPULAR_VOTE, False
    if tie_rule == "contingent":
        log.debug("exact tie: electoral votes withheld", extra={"ctx": {"contest": stream_key}})
        return None, DECIDED_WITHHELD, True
    if tab.winner is not None and tab.winner in tied_idx:
        return tab.line_keys[tab.winner], DECIDED_LOT, True  # lot already drawn at tabulation
    keys = [tab.line_keys[i] for i in tied_idx]
    chosen = stable_choice_order(keys, 0 if tie_seed is None else int(tie_seed), "ev-tie-lot", stream_key)[0]
    log.debug("exact tie decided by lot", extra={"ctx": {"contest": stream_key, "winner": chosen}})
    return chosen, DECIDED_LOT, True


def _remainder_lot_used(totals: Mapping[str, int], ev: int) -> bool:
    """Whether a largest-remainder split of ``ev`` needs the lot: the last EV falls between
    tickets with exactly equal remainders *and* equal votes (see :func:`largest_remainder`)."""
    v = np.array([int(x) for x in totals.values()], dtype=np.int64)
    total = int(v.sum())
    if total <= 0 or ev <= 0:
        return False
    num = v * int(ev)
    left = int(ev - (num // total).sum())
    keyed = sorted(((int(r), int(x)) for r, x in zip(num % total, v, strict=True) if x > 0), reverse=True)
    return 0 < left < len(keyed) and keyed[left - 1] == keyed[left]


def _margin_pct(tab: TabulatedRace) -> float:
    totals = np.sort(np.asarray(tab.totals, dtype=np.int64))[::-1]
    valid = int(totals.sum())
    if valid <= 0 or len(totals) == 0:
        return 0.0
    second = totals[1] if len(totals) > 1 else 0
    return float(100.0 * (totals[0] - second) / valid)


def allocate(
    province_tabs: Mapping[str, TabulatedRace],
    ev_by_province: Mapping[str, int],
    method: EVAllocationMethod | str = EVAllocationMethod.WINNER_TAKE_ALL,
    district_tabs: Mapping[str, TabulatedRace] | None = None,
    district_province: Mapping[str, str] | None = None,
    constitution: ConstitutionConfig | None = None,
    tie_seed: int | None = None,
    *,
    majority: int | None = None,
) -> EVOutcome:
    """Allocate every province's electoral votes.

    Parameters
    ----------
    province_tabs:
        Province code → tabulated ``PRESIDENT_PROVINCE`` race.  Line keys must identify tickets
        consistently across provinces (a ticket missing from a province's ballot gets 0 there).
    ev_by_province:
        Province code → electoral votes (the order defines the output order).
    method:
        :class:`EVAllocationMethod`; default winner-take-all.
    district_tabs, district_province:
        DISTRICT method only: district code → tabulated presidential vote within the district,
        and district code → province code.  Each province needs exactly
        ``ev − senators_per_province`` districts.
    constitution:
        Supplies ``province_tie_rule`` and ``senators_per_province`` (default: active config).
    tie_seed:
        Root seed for drawing lots on exact ties (default 0 — still deterministic).
    majority:
        Electoral votes needed to win.  Default: ``constitution.presidential_majority`` when the
        map total equals ``constitution.electoral_votes``, else ``majority_of(map total)`` (with a
        warning).  Pass the constitutional majority explicitly for partial maps.  Must exceed
        half the map total so that at most one ticket can reach it.
    """
    cfg = constitution or get_constitution()
    method = EVAllocationMethod(method)
    tie_rule = cfg.province_tie_rule
    provinces = list(ev_by_province)
    missing = [p for p in provinces if p not in province_tabs]
    if missing:
        raise ElectionError(f"no presidential result for provinces {missing}")
    total_ev = int(sum(int(v) for v in ev_by_province.values()))
    if total_ev <= 0:
        raise ElectionError("electoral-vote map is empty")
    if majority is not None:
        majority = int(majority)
        if majority < 1 or 2 * majority <= total_ev:
            raise ElectionError(f"majority {majority} must exceed half of the {total_ev} electoral votes")
    elif total_ev == cfg.electoral_votes:
        majority = cfg.presidential_majority
    else:
        majority = majority_of(total_ev)
        log.warning(
            "EV map total differs from the constitution; using the majority of the map",
            extra={"ctx": {"total": total_ev, "constitution": cfg.electoral_votes, "majority": majority}},
        )

    by_district: dict[str, list[str]] = {}
    if method is EVAllocationMethod.DISTRICT:
        if district_tabs is None or district_province is None:
            raise ElectionError("DISTRICT allocation needs district_tabs and district_province")
        for d in sorted(district_province):
            by_district.setdefault(district_province[d], []).append(d)
        absent = [d for d in district_province if d not in district_tabs]
        if absent:
            raise ElectionError(f"no presidential district result for {absent[:5]}")

    line_keys = _line_union(province_tabs, provinces)
    extra_lines: dict[str, None] = {}
    if method is EVAllocationMethod.DISTRICT and district_tabs is not None:
        for d in sorted(district_tabs):
            for k in district_tabs[d].line_keys:
                if k not in line_keys:
                    extra_lines.setdefault(k, None)
    line_keys += list(extra_lines)

    awards: list[EVAward] = []
    prov_out: dict[str, ProvinceEV] = {}
    ties: list[str] = []
    withheld: dict[str, int] = {}
    for p in provinces:
        ev = int(ev_by_province[p])
        tab = province_tabs[p]
        winner, how, tied = _decide(tab, tie_rule, tie_seed, p)
        if tied:
            ties.append(p)
        pev = ProvinceEV(p, ev, winner, how, tied, _margin_pct(tab))
        if method is EVAllocationMethod.WINNER_TAKE_ALL:
            if winner is not None:
                awards.append(EVAward(p, winner, ev, "province", how))
            else:
                pev.withheld = ev
        elif method is EVAllocationMethod.DISTRICT:
            districts = by_district.get(p, [])
            statewide = cfg.senators_per_province
            if len(districts) + statewide != ev:
                raise ElectionError(
                    f"{p}: {len(districts)} districts + {statewide} statewide EV != {ev} electoral votes"
                )
            if winner is not None:
                awards.append(EVAward(p, winner, statewide, "statewide", how))
            else:
                pev.withheld += statewide
            for d in districts:
                dw, dhow, dtied = _decide(district_tabs[d], tie_rule, tie_seed, d)  # type: ignore[index]
                if dtied:
                    ties.append(d)
                if dw is not None:
                    awards.append(EVAward(p, dw, 1, f"district:{d}", dhow))
                else:
                    pev.withheld += 1
        else:  # PROPORTIONAL
            totals = {k: int(v) for k, v in zip(tab.line_keys, np.asarray(tab.totals), strict=True)}
            if sum(totals.values()) > 0:
                split = largest_remainder(totals, ev, tie_seed=tie_seed, lot_key=f"ev-proportional-{p}")
                # the vote split decides the EV; a province-wide first-place tie is irrelevant here
                pev.decided_by = DECIDED_LOT if _remainder_lot_used(totals, ev) else DECIDED_POPULAR_VOTE
                for k, n in split.items():
                    if n > 0:
                        awards.append(EVAward(p, k, n, "proportional", pev.decided_by))
            elif winner is not None:  # no votes at all: the tie rule decides the whole province
                awards.append(EVAward(p, winner, ev, "proportional", how))
            else:
                pev.withheld = ev
        for a in awards:
            if a.province_code == p:
                pev.awards[a.line_key] = pev.awards.get(a.line_key, 0) + a.electoral_votes
        if pev.withheld:
            withheld[p] = pev.withheld
        prov_out[p] = pev

    ev_by_line = dict.fromkeys(line_keys, 0)
    for a in awards:
        ev_by_line[a.line_key] += a.electoral_votes
    allocations = [(p, k, n) for p in provinces for k, n in prov_out[p].awards.items()]
    total_allocated = sum(ev_by_line.values())
    if total_allocated + sum(withheld.values()) != total_ev:
        raise ElectionError("electoral votes do not reconcile (internal error)")
    reaching = [k for k, n in ev_by_line.items() if n >= majority]
    winner_key = reaching[0] if reaching else None
    outcome = EVOutcome(
        method=method,
        line_keys=line_keys,
        ev_by_line=ev_by_line,
        province_winners={p: prov_out[p].winner for p in provinces},
        allocations=allocations,
        awards=awards,
        provinces=prov_out,
        total_ev=total_ev,
        total_allocated=total_allocated,
        withheld=withheld,
        majority=majority,
        winner=winner_key,
        needs_contingent=winner_key is None,
        ties=ties,
        decided_by={p: prov_out[p].decided_by for p in provinces},
    )
    log.debug(
        "electoral college allocated",
        extra={
            "ctx": {"method": str(method), "winner": winner_key, "ev": dict(ev_by_line), "majority": majority}
        },
    )
    return outcome


# --------------------------------------------------------------------------- statistics
def _winner_margin_pp(tab: TabulatedRace, line_key: str) -> float:
    """``line_key``'s share minus its strongest opponent's share, in percentage points."""
    totals = np.asarray(tab.totals, dtype=np.int64)
    valid = int(totals.sum())
    if valid <= 0:
        return 0.0
    keys = list(tab.line_keys)
    own = int(totals[keys.index(line_key)]) if line_key in keys else 0
    others = [int(t) for k, t in zip(keys, totals, strict=True) if k != line_key]
    best_other = max(others) if others else 0
    return float(100.0 * (own - best_other) / valid)


def province_margins(province_tabs: Mapping[str, TabulatedRace], line_key: str) -> dict[str, float]:
    """Per province: ``line_key``'s margin over its strongest opponent (pp; negative = trailing)."""
    return {p: _winner_margin_pp(tab, line_key) for p, tab in province_tabs.items()}


def tipping_point(
    province_tabs: Mapping[str, TabulatedRace],
    ev_by_province: Mapping[str, int],
    winner_key: str,
    majority: int | None = None,
) -> tuple[str, float]:
    """The tipping-point province for ``winner_key``.

    Provinces are sorted by the winner's margin over the strongest opponent (pp, descending;
    equal margins keep ``ev_by_province`` order) and their electoral votes accumulated (the
    winner-take-all count); the province whose electoral votes first bring the running total to
    the majority (default ``majority_of(sum EV)``, 88 for the canonical map) is returned with
    the winner's margin there.
    """
    provinces = [p for p in ev_by_province if p in province_tabs]
    if len(provinces) != len(ev_by_province):
        raise ElectionError("tipping_point: missing province results")
    total = int(sum(int(v) for v in ev_by_province.values()))
    need = majority if majority is not None else majority_of(total)
    margins = province_margins({p: province_tabs[p] for p in provinces}, winner_key)
    order = sorted(range(len(provinces)), key=lambda i: (-margins[provinces[i]], i))
    running = 0
    for i in order:
        p = provinces[i]
        running += int(ev_by_province[p])
        if running >= need:
            return p, margins[p]
    raise ElectionError("tipping_point: majority exceeds total electoral votes")


def national_popular_votes(province_tabs: Mapping[str, TabulatedRace]) -> dict[str, int]:
    """National popular vote per ticket (sum of the province contests, exact integers)."""
    out: dict[str, int] = {}
    for tab in province_tabs.values():
        for k, v in zip(tab.line_keys, np.asarray(tab.totals, dtype=np.int64), strict=True):
            out[k] = out.get(k, 0) + int(v)
    return out


def _pv_totals(source: Mapping[str, TabulatedRace] | Mapping[str, int]) -> dict[str, int]:
    if not source:
        return {}
    first = next(iter(source.values()))
    if isinstance(first, TabulatedRace):
        return national_popular_votes(source)  # type: ignore[arg-type]
    return {k: int(v) for k, v in source.items()}  # type: ignore[arg-type]


def popular_vote_winner(source: Mapping[str, TabulatedRace] | Mapping[str, int]) -> str | None:
    """National popular-vote plurality winner (``None`` on an exact tie or with no votes).

    ``source`` is either province code → tabulated province race, or ticket → national votes.
    """
    pv = _pv_totals(source)
    if not pv:
        return None
    ranked = sorted(pv.items(), key=lambda kv: -kv[1])
    if ranked[0][1] <= 0 or (len(ranked) > 1 and ranked[1][1] == ranked[0][1]):
        return None
    return ranked[0][0]


def pv_margin(source: Mapping[str, TabulatedRace] | Mapping[str, int]) -> tuple[int, float]:
    """National popular-vote margin of the leader over the runner-up: (votes, pp of valid votes)."""
    pv = _pv_totals(source)
    vals = sorted(pv.values(), reverse=True)
    if not vals:
        return 0, 0.0
    total = sum(vals)
    diff = vals[0] - (vals[1] if len(vals) > 1 else 0)
    return int(diff), float(100.0 * diff / total) if total > 0 else 0.0


def ev_margin(outcome: EVOutcome) -> int:
    """Electoral votes of the first minus the second ticket (0 on an EV tie)."""
    vals = sorted(outcome.ev_by_line.values(), reverse=True)
    if not vals:
        return 0
    return int(vals[0] - (vals[1] if len(vals) > 1 else 0))


@dataclass(frozen=True)
class PVDivergence:
    """Whether the national popular-vote plurality winner differs from the EV winner/leader.

    Purely descriptive: the fictional constitution elects by electoral votes; a divergence
    (as in the U.S. in 1876, 1888, 2000 and 2016) is reported, not judged.
    """

    diverged: bool
    popular_vote_winner: str | None
    ev_winner: str | None
    ev_leader: str | None
    pv_margin_votes: int
    pv_margin_pct: float

    def __bool__(self) -> bool:
        return self.diverged


def ev_pv_divergence(
    outcome: EVOutcome, source: Mapping[str, TabulatedRace] | Mapping[str, int]
) -> PVDivergence:
    """Compare the national popular-vote winner with the EV winner (or the EV leader when no
    ticket reached the majority)."""
    pv_w = popular_vote_winner(source)
    ref = outcome.winner if outcome.winner is not None else outcome.leader
    votes, pct = pv_margin(source)
    return PVDivergence(
        diverged=pv_w is not None and ref is not None and pv_w != ref,
        popular_vote_winner=pv_w,
        ev_winner=outcome.winner,
        ev_leader=outcome.leader,
        pv_margin_votes=votes,
        pv_margin_pct=pct,
    )


def closest_province(province_tabs: Mapping[str, TabulatedRace]) -> tuple[str, float]:
    """Province with the smallest winner − runner-up margin (pp); ties keep mapping order."""
    if not province_tabs:
        raise ElectionError("no provinces")
    items = [(p, _margin_pct(t)) for p, t in province_tabs.items()]
    return min(items, key=lambda it: it[1])


def largest_victory(province_tabs: Mapping[str, TabulatedRace]) -> tuple[str, float]:
    """Province with the largest winner − runner-up margin (pp); ties keep mapping order."""
    if not province_tabs:
        raise ElectionError("no provinces")
    items = [(p, _margin_pct(t)) for p, t in province_tabs.items()]
    return max(items, key=lambda it: it[1])
