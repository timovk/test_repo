"""Contingent election: what happens when no ticket wins a majority of the electoral votes.

The fictional constitution (``ContingentElectionConfig``) offers four modes.  In every mode the
*finalists* are the top ``N`` recipients of electoral votes (``N = config.finalists``, 3 by
default, like the U.S. 12th Amendment); tickets with equal electoral votes are ordered by
national popular vote, then by seeded lot.  Only tickets with at least one electoral vote are
eligible; if fewer than two exist (e.g. electoral votes withheld after ties), the list is filled
up to two with the national popular-vote leaders.

``PROVINCE_DELEGATIONS`` (default, 12th-Amendment analogue)
    The newly elected Tweede Kamer votes by province delegation, one vote per province.  Each
    member votes for the finalist of their own party; members whose party has no finalist vote
    for the ideologically closest finalist (Euclidean distance of the party ideology vectors;
    members with unknown ideology rank finalists by national popular vote).  A delegation casts
    its vote for a finalist backed by a *strict majority* of its members; otherwise it is
    *divided* and casts no vote.  A finalist needs the votes of a majority of **all** provinces
    (``majority_of(12) = 7``).  If no one reaches it, the finalist with the fewest delegation
    votes is eliminated (ties: fewer member votes nationally, then fewer national popular votes,
    then seeded lot) and its supporters move to their next preference; elimination stops when
    two finalists remain (a two-way deadlock is possible, e.g. 6–6 or with divided
    delegations).  After ``max_ballots`` unsuccessful ballots the ``deadlock_fallback`` applies.
``HOUSE_MEMBERS``
    As above but every member votes individually; a finalist needs a majority of all members
    (76 of 150).
``NATIONAL_POPULAR_VOTE``
    The finalist with the most national popular votes wins (exact tie: seeded lot).
``NATIONAL_RUNOFF``
    A national runoff between the top two finalists, simulated by the caller-supplied
    ``runoff_fn(finalists) -> winner line key | {line key: votes}``.  Without a usable runoff
    result the top two are decided by the national popular vote of the general election.

Deadlock fallback (``config.deadlock_fallback``)
    ``'popular_vote'`` — the finalist with the most national popular votes is elected;
    ``'vice_president_acts'`` — no President is elected and the Vice-President-elect acts as
    President (U.S. 20th-Amendment analogue).

Vice-President
    With ``vice_president_by_senate`` the Senate chooses between the running mates of the top
    ``vice_president_finalists`` (2) tickets by electoral votes: each senator votes for their own
    party's ticket, otherwise the ideologically closest; a majority of the whole Senate
    (``majority_of(24) = 13``) is required.  With more than two VP finalists the same elimination
    rounds apply.  If the Senate does not reach a majority within ``max_ballots`` ballots, the
    running mate of the ticket with the most national popular votes among the VP finalists
    becomes Vice-President.  Without ``vice_president_by_senate`` the Vice-President is the
    running mate of the elected President (or, when the President is not elected, of the
    national popular-vote leader among the finalists).

Everything is deterministic given ``seed``.  Each ballot is recorded as a
:class:`ContingentRound` (full audit trail).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from app.core.constitution import ContingentElectionConfig, ContingentElectionMode, majority_of
from app.core.errors import ElectionError
from app.core.logging import get_logger
from app.core.rng import stable_choice_order
from app.elections.electoral_college import EVOutcome

log = get_logger(__name__)

OUTCOME_ELECTED = "elected"
OUTCOME_FALLBACK_POPULAR_VOTE = "fallback_popular_vote"
OUTCOME_VP_ACTS = "vp_acts"

RunoffFn = Callable[[list[str]], "str | Mapping[str, int] | None"]


@dataclass
class ContingentRound:
    """One ballot (or one popular-vote / runoff determination) of the contingent procedure."""

    number: int
    body: str  # house_delegations | house_members | national_popular_vote | national_runoff | senate
    candidates: list[str]  # finalists still in contention
    tallies: dict[str, int]  # finalist → delegation votes / member votes / popular votes
    required: int  # votes needed to win this ballot (0 for plurality determinations)
    member_votes: dict[str, int] = field(default_factory=dict)  # individual members' votes
    delegations: dict[str, str | None] = field(default_factory=dict)  # province → finalist (None = divided)
    delegation_breakdown: dict[str, dict[str, int]] = field(
        default_factory=dict
    )  # province → finalist → members
    divided: list[str] = field(default_factory=list)
    winner: str | None = None
    eliminated: str | None = None
    note: str = ""


@dataclass
class VicePresidentResult:
    """How the Vice-President was chosen."""

    method: str  # 'senate' | 'follows_president'
    finalists: list[str]  # ticket line keys whose running mates were eligible
    candidates: dict[str, Any]  # ticket line key → VP candidate
    winner_line: str | None
    vice_president: Any | None
    decided_by: str  # senate | follows_president | popular_vote_fallback | lot
    required: int = 0
    rounds: list[ContingentRound] = field(default_factory=list)


@dataclass
class ContingentResult:
    """Outcome and audit trail of a contingent election.

    ``outcome``: ``'elected'`` (the procedure elected a President), ``'fallback_popular_vote'``
    (deadlock → national popular vote) or ``'vp_acts'`` (deadlock → no President elected; the
    Vice-President-elect, ``acting_president``, acts).  ``decided_by`` names the deciding body
    or rule: ``house_delegations`` | ``house_members`` | ``national_popular_vote`` |
    ``national_runoff`` | ``popular_vote_fallback`` | ``vice_president_acts`` | ``lot``.
    """

    mode: ContingentElectionMode
    finalists: list[str]
    finalist_ev: dict[str, int]
    finalist_popular_votes: dict[str, int]
    rounds: list[ContingentRound]
    winner: str | None
    winner_party: str | None
    outcome: str
    decided_by: str
    ballots: int
    vice_president: VicePresidentResult
    acting_president: str | None = None  # ticket line key whose running mate acts as President
    config: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable audit record (``contingent_election.ballots_json``)."""
        d = asdict(self)
        d["mode"] = str(self.mode)
        return d


# --------------------------------------------------------------------------- helpers
class _Prefs:
    """Preference orders of voters (by party) over a fixed candidate list."""

    def __init__(
        self,
        candidates: Sequence[str],
        line_party: Mapping[str, str | None],
        party_ideology: Mapping[str, Sequence[float]],
        popular_votes: Mapping[str, int],
        electoral_votes: Mapping[str, int],
    ) -> None:
        self.candidates = list(candidates)
        self.line_party = line_party
        self.party_ideology = party_ideology
        self.pv = popular_votes
        self.ev = electoral_votes
        self._cache: dict[str | None, list[str]] = {}

    def _line_ideology(self, line: str) -> np.ndarray | None:
        party = self.line_party.get(line)
        vec = self.party_ideology.get(party) if party is not None else None
        if vec is None:
            vec = self.party_ideology.get(line)  # independents: ideology keyed by line key
        return None if vec is None else np.asarray(vec, dtype=float)

    def order(self, voter_party: str | None) -> list[str]:
        if voter_party in self._cache:
            return self._cache[voter_party]
        idx = {c: i for i, c in enumerate(self.candidates)}
        own = [
            c for c in self.candidates if voter_party is not None and self.line_party.get(c) == voter_party
        ]
        own.sort(key=lambda c: (-int(self.pv.get(c, 0)), idx[c]))
        vi = self.party_ideology.get(voter_party) if voter_party is not None else None
        v = None if vi is None else np.asarray(vi, dtype=float)

        def dist(c: str) -> float:
            li = self._line_ideology(c)
            if v is None or li is None:
                return math.inf
            if li.shape != v.shape:
                raise ElectionError(f"ideology dimensions differ for {voter_party} and {c}")
            d = float(np.linalg.norm(v - li))
            # a NaN/inf coordinate means "unknown ideology": rank like an unknown profile, never
            # let NaN (which compares false both ways) scramble the preference order
            return d if math.isfinite(d) else math.inf

        rest = [c for c in self.candidates if c not in own]
        rest.sort(key=lambda c: (dist(c), -int(self.pv.get(c, 0)), -int(self.ev.get(c, 0)), idx[c]))
        out = own + rest
        self._cache[voter_party] = out
        return out

    def choice(self, voter_party: str | None, remaining: set[str]) -> str:
        for c in self.order(voter_party):
            if c in remaining:
                return c
        raise ElectionError("no remaining candidate")  # pragma: no cover - remaining is never empty


def _lot_order(keys: Sequence[str], seed: int, *stream: str) -> dict[str, int]:
    return {k: i for i, k in enumerate(stable_choice_order(list(keys), seed, "contingent", *stream))}


def _plurality(
    tallies: Mapping[str, int], candidates: Sequence[str], seed: int, stream: str
) -> tuple[str | None, bool]:
    """(winner, decided_by_lot) — plurality with seeded lot on exact ties."""
    if not candidates:
        return None, False
    top = max(int(tallies.get(c, 0)) for c in candidates)
    tied = [c for c in candidates if int(tallies.get(c, 0)) == top]
    if len(tied) == 1:
        return tied[0], False
    lot = _lot_order(tied, seed, stream)
    return min(tied, key=lambda c: lot[c]), True


def _ballot_rounds(
    body: str,
    candidates: list[str],
    groups: Mapping[str, Sequence[str | None]],
    by_group: bool,
    prefs: _Prefs,
    max_ballots: int,
    seed: int,
    stream: str,
) -> tuple[str | None, list[ContingentRound]]:
    """Repeated ballots with elimination of the weakest candidate while more than two remain.

    ``groups`` maps a group (province / 'chamber') to its members' party codes.  With
    ``by_group`` each group casts one vote (strict majority of its members, else divided) and a
    majority of all groups is needed; otherwise members vote individually and a majority of all
    members is needed.
    """
    remaining = list(candidates)
    members_total = sum(len(m) for m in groups.values())
    required = majority_of(len(groups)) if by_group else majority_of(max(members_total, 1))
    rounds: list[ContingentRound] = []
    for number in range(1, max_ballots + 1):
        rem = set(remaining)
        member_votes = dict.fromkeys(remaining, 0)
        delegations: dict[str, str | None] = {}
        breakdown: dict[str, dict[str, int]] = {}
        divided: list[str] = []
        for g, members in groups.items():
            counts = dict.fromkeys(remaining, 0)
            for party in members:
                counts[prefs.choice(party, rem)] += 1
            for c, n in counts.items():
                member_votes[c] += n
            if by_group:
                breakdown[g] = {c: n for c, n in counts.items() if n}
                backed = [c for c, n in counts.items() if 2 * n > len(members)]
                delegations[g] = backed[0] if backed else None
                if not backed:
                    divided.append(g)
        if by_group:
            tallies = dict.fromkeys(remaining, 0)
            for c in delegations.values():
                if c is not None:
                    tallies[c] += 1
        else:
            tallies = dict(member_votes)
        winners = [c for c in remaining if tallies[c] >= required]
        rnd = ContingentRound(
            number=number,
            body=body,
            candidates=list(remaining),
            tallies=tallies,
            required=required,
            member_votes=member_votes,
            delegations=delegations,
            delegation_breakdown=breakdown,
            divided=divided,
        )
        rounds.append(rnd)
        if winners:
            rnd.winner = winners[0]
            rnd.note = f"{winners[0]} elected with {tallies[winners[0]]} of {required} required"
            return winners[0], rounds
        if number == max_ballots:
            rnd.note = "no majority; ballot limit reached"
        elif len(remaining) > 2:
            lot = _lot_order(remaining, seed, stream, f"eliminate-{number}")
            weakest = min(
                remaining,
                key=lambda c: (tallies[c], member_votes[c], int(prefs.pv.get(c, 0)), lot[c]),
            )
            remaining.remove(weakest)
            rnd.eliminated = weakest
            rnd.note = f"no majority; {weakest} eliminated"
        else:
            rnd.note = "no majority; deadlocked between the final two"
    return None, rounds


def rank_finalists(
    line_keys: Sequence[str],
    electoral_votes: Mapping[str, int],
    national_popular_votes: Mapping[str, int],
    n: int,
    seed: int,
    stream: str = "finalists",
) -> list[str]:
    """Top ``n`` electoral-vote recipients among ``line_keys`` (EV desc, then national popular
    vote desc, then seeded lot).

    Tickets without electoral votes are ineligible, except that the list is filled up to two
    with the national popular-vote leaders when fewer than two tickets received electoral votes.
    """
    universe = list(dict.fromkeys(line_keys))
    lot = _lot_order(universe, seed, stream)
    ev = {c: int(electoral_votes.get(c, 0)) for c in universe}
    pv = {c: int(national_popular_votes.get(c, 0)) for c in universe}
    recipients = sorted((c for c in universe if ev[c] > 0), key=lambda c: (-ev[c], -pv[c], lot[c]))
    finalists = recipients[:n]
    for c in sorted(universe, key=lambda c: (-pv[c], lot[c])):
        if len(finalists) >= min(2, len(universe)):
            break
        if c not in finalists:
            finalists.append(c)
    return finalists


def select_finalists(
    ev_outcome: EVOutcome,
    national_popular_votes: Mapping[str, int],
    n: int,
    seed: int,
) -> list[str]:
    """Finalists of the contingent election: :func:`rank_finalists` over every ticket of the
    Electoral College outcome (and any ticket that only received popular votes)."""
    universe = list(dict.fromkeys([*ev_outcome.line_keys, *national_popular_votes]))
    return rank_finalists(universe, ev_outcome.ev_by_line, national_popular_votes, n, seed)


def _vice_president(
    ev_outcome: EVOutcome,
    national_popular_votes: Mapping[str, int],
    president_line: str | None,
    finalists: list[str],
    senate_members: Sequence[str | None],
    line_party: Mapping[str, str | None],
    party_ideology: Mapping[str, Sequence[float]],
    vp_candidates: Mapping[str, Any],
    config: ContingentElectionConfig,
    seed: int,
) -> VicePresidentResult:
    pv = national_popular_votes
    if not config.vice_president_by_senate:
        line = president_line
        how = "follows_president"
        if line is None:
            line, by_lot = _plurality(pv, finalists, seed, "vp-follow")
            how = "lot" if by_lot else "popular_vote_fallback"
        return VicePresidentResult(
            method="follows_president",
            finalists=[line] if line else [],
            candidates={line: vp_candidates.get(line)} if line else {},
            winner_line=line,
            vice_president=vp_candidates.get(line) if line else None,
            decided_by=how,
        )
    eligible = [c for c in ev_outcome.line_keys if c in vp_candidates]
    ranked = rank_finalists(
        eligible, ev_outcome.ev_by_line, pv, config.vice_president_finalists, seed, "vp-finalists"
    )
    cands = {c: vp_candidates[c] for c in ranked}
    if not ranked:
        return VicePresidentResult("senate", [], {}, None, None, "senate")
    prefs = _Prefs(ranked, line_party, party_ideology, pv, ev_outcome.ev_by_line)
    winner, rounds = _ballot_rounds(
        "senate", ranked, {"senate": list(senate_members)}, False, prefs, config.max_ballots, seed, "vp"
    )
    required = rounds[0].required if rounds else majority_of(max(len(senate_members), 1))
    how = "senate"
    if winner is None:
        winner, by_lot = _plurality(pv, ranked, seed, "vp-fallback")
        how = "lot" if by_lot else "popular_vote_fallback"
        if rounds:
            rounds[-1].note += f"; Vice-President by national popular vote: {winner}"
    return VicePresidentResult(
        method="senate",
        finalists=ranked,
        candidates=cands,
        winner_line=winner,
        vice_president=vp_candidates.get(winner) if winner else None,
        decided_by=how,
        required=required,
        rounds=rounds,
    )


# --------------------------------------------------------------------------- main entry point
def run_contingent_election(
    ev_outcome: EVOutcome,
    national_popular_votes: Mapping[str, int],
    finalists_n: int | None,
    house_delegations: Mapping[str, Sequence[str | None]],
    senate_members: Sequence[str | None],
    line_party: Mapping[str, str | None],
    party_ideology: Mapping[str, Sequence[float]],
    vp_candidates: Mapping[str, Any],
    config: ContingentElectionConfig,
    seed: int,
    runoff_fn: RunoffFn | None = None,
) -> ContingentResult:
    """Run the contingent election configured by ``config`` (see the module docstring).

    Parameters
    ----------
    ev_outcome:
        Electoral-College outcome without a majority winner (``needs_contingent``).
    national_popular_votes:
        Ticket line key → national popular votes of the general election.
    finalists_n:
        Number of finalists (default ``config.finalists``).
    house_delegations:
        Province code → party codes of *all* members of the newly elected House from that
        province (``None`` = independent).  Its length defines the number of delegations.
    senate_members:
        Party codes of all senators (for the Vice-President choice).
    line_party:
        Ticket line key → party code (``None`` for independents / dissolved parties).
    party_ideology:
        Party code → ideology vector (e.g. economic, social, europe).  Independent tickets may
        be keyed by line key.
    vp_candidates:
        Ticket line key → running mate (any identifier; returned as-is).
    config, seed:
        Procedure configuration and root seed of all lots.
    runoff_fn:
        NATIONAL_RUNOFF only: simulates the runoff among the given finalists and returns the
        winning line key or a mapping line key → runoff votes.
    """
    if ev_outcome.winner is not None:
        raise ElectionError(f"{ev_outcome.winner} won the Electoral College; no contingent election is held")
    mode = ContingentElectionMode(config.mode)
    n = finalists_n if finalists_n is not None else config.finalists
    if n < 2:
        raise ElectionError("a contingent election needs at least two finalists")
    pv = {k: int(v) for k, v in national_popular_votes.items()}
    finalists = select_finalists(ev_outcome, pv, n, seed)
    if len(finalists) < 2:
        raise ElectionError("fewer than two tickets available for the contingent election")
    prefs = _Prefs(finalists, line_party, party_ideology, pv, ev_outcome.ev_by_line)
    rounds: list[ContingentRound] = []
    winner: str | None = None
    tallies: dict[str, int] = {}
    decided = ""

    if mode in (ContingentElectionMode.PROVINCE_DELEGATIONS, ContingentElectionMode.HOUSE_MEMBERS):
        by_group = mode is ContingentElectionMode.PROVINCE_DELEGATIONS
        body = "house_delegations" if by_group else "house_members"
        groups = (
            dict(house_delegations)
            if by_group
            else {"house": [m for ms in house_delegations.values() for m in ms]}
        )
        if not groups or not any(groups.values()):
            raise ElectionError("house_delegations is empty")
        winner, rounds = _ballot_rounds(
            body, finalists, groups, by_group, prefs, config.max_ballots, seed, "president"
        )
        decided = body
    elif mode is ContingentElectionMode.NATIONAL_POPULAR_VOTE:
        tallies = {c: pv.get(c, 0) for c in finalists}
        winner, by_lot = _plurality(tallies, finalists, seed, "npv")
        rounds.append(
            ContingentRound(
                1,
                "national_popular_vote",
                list(finalists),
                tallies,
                0,
                winner=winner,
                note="plurality of the national popular vote among the finalists"
                + (" (exact tie decided by lot)" if by_lot else ""),
            )
        )
        decided = "lot" if by_lot else "national_popular_vote"
    else:  # NATIONAL_RUNOFF
        top_two = finalists[:2]
        result = runoff_fn(list(top_two)) if runoff_fn is not None else None
        tallies = {}
        note = ""
        if isinstance(result, Mapping):
            tallies = {c: int(result.get(c, 0)) for c in top_two}
            if sum(tallies.values()) > 0:
                winner, by_lot = _plurality(tallies, top_two, seed, "runoff")
                note = "national runoff" + (" (exact tie decided by lot)" if by_lot else "")
                decided = "lot" if by_lot else "national_runoff"
        elif isinstance(result, str) and result in top_two:
            winner, note, decided = (
                result,
                "national runoff (winner reported by runoff_fn)",
                "national_runoff",
            )
        if winner is None:
            if result is not None:
                log.warning(
                    "runoff_fn returned no usable result; falling back to the popular vote",
                    extra={"ctx": {"result": repr(result)[:200], "finalists": list(top_two)}},
                )
            tallies = {c: pv.get(c, 0) for c in top_two}
            winner, by_lot = _plurality(tallies, top_two, seed, "runoff-fallback")
            note = "no runoff result available; top two decided by the general-election popular vote"
            if result is not None:
                note = f"runoff result unusable ({repr(result)[:80]}); top two decided by the general-election popular vote"
            decided = "lot" if by_lot else "national_popular_vote"
        rounds.append(
            ContingentRound(1, "national_runoff", list(top_two), tallies, 0, winner=winner, note=note)
        )

    outcome = OUTCOME_ELECTED
    acting: str | None = None
    if winner is None:  # deadlock of the House procedure
        if config.deadlock_fallback == "popular_vote":
            winner, by_lot = _plurality({c: pv.get(c, 0) for c in finalists}, finalists, seed, "deadlock")
            outcome = OUTCOME_FALLBACK_POPULAR_VOTE
            decided = "lot" if by_lot else "popular_vote_fallback"
        else:
            outcome = OUTCOME_VP_ACTS
            decided = "vice_president_acts"

    vp = _vice_president(
        ev_outcome,
        pv,
        winner,
        finalists,
        senate_members,
        line_party,
        party_ideology,
        vp_candidates,
        config,
        seed,
    )
    if outcome == OUTCOME_VP_ACTS:
        acting = vp.winner_line
    result_obj = ContingentResult(
        mode=mode,
        finalists=finalists,
        finalist_ev={c: int(ev_outcome.ev_by_line.get(c, 0)) for c in finalists},
        finalist_popular_votes={c: pv.get(c, 0) for c in finalists},
        rounds=rounds,
        winner=winner,
        winner_party=line_party.get(winner) if winner else None,
        outcome=outcome,
        decided_by=decided,
        ballots=len(rounds),
        vice_president=vp,
        acting_president=acting,
        config=config.model_dump(mode="json"),
    )
    log.info(
        "contingent election decided",
        extra={
            "ctx": {
                "mode": str(mode),
                "winner": winner,
                "outcome": outcome,
                "ballots": len(rounds),
                "vp": vp.winner_line,
            }
        },
    )
    return result_obj
