from __future__ import annotations

import pytest

from app.core.constitution import ContingentElectionConfig, ContingentElectionMode
from app.core.errors import ElectionError
from app.elections.contingent import (
    OUTCOME_ELECTED,
    OUTCOME_FALLBACK_POPULAR_VOTE,
    OUTCOME_VP_ACTS,
    run_contingent_election,
    select_finalists,
)
from app.elections.electoral_college import EVOutcome, allocate
from app.elections.tabulation import tabulate_totals

PROVINCES = ["GR", "FR", "DR", "OV", "FL", "GE", "UT", "NH", "ZH", "ZE", "NB", "LI"]
HOUSE_SEATS = {
    "GR": 5,
    "FR": 6,
    "DR": 4,
    "OV": 10,
    "FL": 4,
    "GE": 18,
    "UT": 12,
    "NH": 25,
    "ZH": 32,
    "ZE": 3,
    "NB": 22,
    "LI": 9,
}
EV = {p: s + 2 for p, s in HOUSE_SEATS.items()}

LINE_PARTY = {"A": "PA", "B": "PB", "C": "PC", "D": "PD"}
IDEOLOGY = {
    "PA": (-0.6, -0.4, 0.5),
    "PB": (0.5, 0.4, 0.0),
    "PC": (0.1, 0.0, 0.6),  # closer to PA (0.812) than to PB (0.825)
    "PD": (-0.8, -0.6, 0.3),
    "PE": (0.9, 0.9, -0.9),  # far right: closest to PB
}
VP = {"A": "vp-anna", "B": "vp-bram", "C": "vp-cor", "D": "vp-dirk"}
PV = {"A": 3_000_000, "B": 2_900_000, "C": 2_000_000, "D": 500_000}


def _tab(key: str, winner: str) -> object:
    votes = {"A": 300, "B": 300, "C": 300}
    votes[winner] = 400
    return tabulate_totals(key, list(votes), list(votes.values()))


def three_way_outcome() -> EVOutcome:
    """A 75, B 57, C 42 electoral votes (no majority)."""
    w = {
        p: "A" if p in {"ZH", "UT", "GE", "GR"} else "B" if p in {"NH", "NB", "FL"} else "C"
        for p in PROVINCES
    }
    out = allocate({p: _tab(p, w[p]) for p in PROVINCES}, EV)
    assert out.ev_by_line == {"A": 75, "B": 57, "C": 42} and out.needs_contingent
    return out


def two_way_tie_outcome() -> EVOutcome:
    a = {"ZH", "NH", "UT", "OV"}
    out = allocate({p: _tab(p, "A" if p in a else "B") for p in PROVINCES}, EV)
    assert out.ev_by_line["A"] == out.ev_by_line["B"] == 87
    return out


def deleg(spec: dict[str, list[tuple[str | None, int]]]) -> dict[str, list[str | None]]:
    out = {}
    for p in PROVINCES:
        members = [party for party, n in spec[p] for _ in range(n)]
        assert len(members) == HOUSE_SEATS[p], p
        out[p] = members
    return out


ELIMINATION_HOUSE = deleg(
    {
        "GR": [("PA", 3), ("PB", 2)],
        "FR": [("PA", 4), ("PB", 2)],
        "DR": [("PA", 3), ("PC", 1)],
        "OV": [("PA", 6), ("PB", 4)],
        "FL": [("PA", 3), ("PB", 1)],
        "GE": [("PB", 10), ("PA", 8)],
        "UT": [("PB", 7), ("PA", 5)],
        "NH": [("PB", 13), ("PA", 12)],
        "ZE": [("PB", 2), ("PA", 1)],
        "LI": [("PB", 5), ("PA", 4)],
        "ZH": [("PC", 17), ("PB", 15)],
        "NB": [("PC", 12), ("PA", 10)],
    }
)
SENATE = ["PA"] * 9 + ["PB"] * 10 + ["PC"] * 5


def run(outcome, house, config, senate=SENATE, pv=PV, seed=42, runoff_fn=None, finalists_n=None):  # type: ignore[no-untyped-def]
    return run_contingent_election(
        outcome, pv, finalists_n, house, senate, LINE_PARTY, IDEOLOGY, VP, config, seed, runoff_fn
    )


def test_province_delegations_with_elimination() -> None:
    res = run(three_way_outcome(), ELIMINATION_HOUSE, ContingentElectionConfig())
    assert res.finalists == ["A", "B", "C"]
    assert res.finalist_ev == {"A": 75, "B": 57, "C": 42}
    r1, r2 = res.rounds
    assert r1.required == 7 and r1.body == "house_delegations"
    assert r1.tallies == {"A": 5, "B": 5, "C": 2}
    assert r1.winner is None and r1.eliminated == "C"
    assert r1.delegations["ZH"] == "C" and r1.delegation_breakdown["ZH"] == {"C": 17, "B": 15}
    assert r2.candidates == ["A", "B"]
    assert r2.tallies == {"A": 7, "B": 5}  # C's members moved to their closest finalist (A)
    assert r2.delegation_breakdown["ZH"] == {"A": 17, "B": 15}
    assert res.winner == "A" and res.winner_party == "PA"
    assert res.outcome == OUTCOME_ELECTED and res.decided_by == "house_delegations"
    assert res.ballots == 2
    # VP by the Senate: top-2 by EV are A and B; PC senators prefer A → 14 ≥ 13
    vp = res.vice_president
    assert vp.method == "senate" and vp.finalists == ["A", "B"] and vp.required == 13
    assert vp.rounds[0].tallies == {"A": 14, "B": 10}
    assert vp.winner_line == "A" and vp.vice_president == "vp-anna" and vp.decided_by == "senate"


def test_divided_delegations_cast_no_vote() -> None:
    house = deleg(
        {
            "GR": [("PA", 2), ("PB", 2), ("PC", 1)],  # divided in ballot 1
            "FR": [("PA", 3), ("PB", 3)],  # divided (no strict majority) in every ballot
            "DR": [("PA", 3), ("PB", 1)],
            "OV": [("PA", 6), ("PB", 4)],
            "FL": [("PA", 3), ("PB", 1)],
            "GE": [("PB", 10), ("PA", 8)],
            "UT": [("PB", 7), ("PA", 5)],
            "NH": [("PB", 13), ("PA", 12)],
            "ZE": [("PB", 2), ("PA", 1)],
            "LI": [("PB", 5), ("PA", 4)],
            "ZH": [("PA", 17), ("PB", 15)],
            "NB": [("PC", 12), ("PA", 10)],
        }
    )
    res = run(three_way_outcome(), house, ContingentElectionConfig())
    r1 = res.rounds[0]
    assert set(r1.divided) == {"GR", "FR"}
    assert r1.delegations["GR"] is None and r1.delegations["FR"] is None
    assert r1.tallies == {"A": 4, "B": 5, "C": 1}
    assert sum(r1.tallies.values()) + len(r1.divided) == 12
    # C eliminated; GR's PC member moves to A (3 of 5) and NB goes to A; FR stays divided
    r2 = res.rounds[1]
    assert r2.divided == ["FR"] and r2.tallies == {"A": 6, "B": 5}
    # deadlock between the final two → popular-vote fallback (A leads the popular vote)
    assert res.winner == "A" and res.outcome == OUTCOME_FALLBACK_POPULAR_VOTE
    assert res.decided_by == "popular_vote_fallback"
    assert res.ballots == 10 and all(r.winner is None for r in res.rounds)
    assert res.rounds[-1].note.startswith("no majority; ballot limit")


def test_deadlock_fallback_popular_vote_and_vp_acts() -> None:
    half = {
        p: [("PA", HOUSE_SEATS[p])] if i % 2 == 0 else [("PB", HOUSE_SEATS[p])]
        for i, p in enumerate(PROVINCES)
    }
    house = deleg(half)
    cfg = ContingentElectionConfig(max_ballots=3, deadlock_fallback="popular_vote")
    pv = {"A": 2_000_000, "B": 2_500_000, "C": 1}
    res = run(two_way_tie_outcome(), house, cfg, pv=pv)
    assert res.finalists == ["B", "A"]  # 87–87: EV tie broken by popular vote
    assert [r.tallies for r in res.rounds] == [{"B": 6, "A": 6}] * 3
    assert all(r.eliminated is None for r in res.rounds)
    assert res.winner == "B" and res.outcome == OUTCOME_FALLBACK_POPULAR_VOTE and res.ballots == 3

    cfg2 = ContingentElectionConfig(max_ballots=3, deadlock_fallback="vice_president_acts")
    senate = ["PA"] * 13 + ["PB"] * 11
    res2 = run(two_way_tie_outcome(), house, cfg2, pv=pv, senate=senate)
    assert res2.winner is None and res2.winner_party is None
    assert res2.outcome == OUTCOME_VP_ACTS and res2.decided_by == "vice_president_acts"
    assert res2.vice_president.winner_line == "A" and res2.vice_president.vice_president == "vp-anna"
    assert res2.acting_president == "A"


def test_house_members_mode() -> None:
    members = ["PA"] * 70 + ["PB"] * 60 + ["PC"] * 20
    house = {p: [] for p in PROVINCES}
    i = 0
    for p in PROVINCES:
        house[p] = members[i : i + HOUSE_SEATS[p]]
        i += HOUSE_SEATS[p]
    cfg = ContingentElectionConfig(mode=ContingentElectionMode.HOUSE_MEMBERS)
    res = run(three_way_outcome(), house, cfg)
    r1, r2 = res.rounds
    assert r1.required == 76 and r1.body == "house_members"
    assert r1.tallies == {"A": 70, "B": 60, "C": 20} and r1.eliminated == "C"
    assert r2.tallies == {"A": 90, "B": 60}
    assert res.winner == "A" and res.decided_by == "house_members"

    tied = ["PA"] * 75 + ["PB"] * 75
    house2 = {"all": tied}
    res2 = run(
        two_way_tie_outcome(), house2, ContingentElectionConfig(mode="house_members", max_ballots=2), pv=PV
    )
    assert [r.tallies for r in res2.rounds] == [{"A": 75, "B": 75}] * 2
    assert res2.winner == "A" and res2.outcome == OUTCOME_FALLBACK_POPULAR_VOTE


def test_member_without_known_ideology_follows_popular_vote() -> None:
    house = (
        {p: ["PA"] for p in ("GR", "FR", "DR", "OV", "FL")}
        | {p: ["PB"] for p in ("GE", "UT", "NH", "ZE", "LI")}
        | {"ZH": [None], "NB": ["PZ"]}
    )
    assert len(house) == 12
    res = run(two_way_tie_outcome(), house, ContingentElectionConfig(max_ballots=1), pv={"A": 10, "B": 20})
    r1 = res.rounds[0]
    assert (
        r1.delegations["ZH"] == "B" and r1.delegations["NB"] == "B"
    )  # independents / unknown party → PV leader
    assert r1.tallies == {"B": 7, "A": 5} and res.winner == "B"


def test_far_party_members_vote_for_closest_finalist() -> None:
    house = {p: ["PE"] * 3 for p in PROVINCES}
    res = run(three_way_outcome(), house, ContingentElectionConfig())
    assert res.rounds[0].tallies == {"A": 0, "B": 12, "C": 0} and res.winner == "B"


def test_national_popular_vote_mode() -> None:
    cfg = ContingentElectionConfig(mode="national_popular_vote")
    pv = {"A": 100, "B": 300, "C": 200, "D": 999}
    res = run(three_way_outcome(), ELIMINATION_HOUSE, cfg, pv=pv)
    assert res.finalists == ["A", "B", "C"]  # D has no electoral votes → not a finalist
    assert res.winner == "B" and res.decided_by == "national_popular_vote" and res.ballots == 1
    assert res.rounds[0].tallies == {"A": 100, "B": 300, "C": 200}


def test_national_runoff_mode() -> None:
    cfg = ContingentElectionConfig(mode="national_runoff")
    seen: list[list[str]] = []

    def runoff(finalists: list[str]) -> dict[str, int]:
        seen.append(finalists)
        return {"A": 4_000_000, "B": 4_100_000}

    res = run(three_way_outcome(), ELIMINATION_HOUSE, cfg, runoff_fn=runoff)
    assert seen == [["A", "B"]]
    assert res.winner == "B" and res.decided_by == "national_runoff"
    assert res.rounds[0].tallies == {"A": 4_000_000, "B": 4_100_000}
    res2 = run(three_way_outcome(), ELIMINATION_HOUSE, cfg, runoff_fn=lambda f: "A")
    assert res2.winner == "A" and res2.decided_by == "national_runoff"
    res3 = run(three_way_outcome(), ELIMINATION_HOUSE, cfg, runoff_fn=None)
    assert res3.winner == "A" and res3.decided_by == "national_popular_vote"  # fallback among top two
    res4 = run(three_way_outcome(), ELIMINATION_HOUSE, cfg, runoff_fn=lambda f: "Z")
    assert res4.winner == "A"


def test_vp_senate_deadlock_and_follow_president() -> None:
    senate = ["PA"] * 12 + ["PB"] * 12
    res = run(three_way_outcome(), ELIMINATION_HOUSE, ContingentElectionConfig(max_ballots=2), senate=senate)
    vp = res.vice_president
    assert [r.tallies for r in vp.rounds] == [{"A": 12, "B": 12}] * 2
    assert vp.winner_line == "A" and vp.decided_by == "popular_vote_fallback"

    cfg = ContingentElectionConfig(vice_president_by_senate=False)
    res2 = run(three_way_outcome(), ELIMINATION_HOUSE, cfg)
    assert res2.vice_president.method == "follows_president"
    assert res2.vice_president.winner_line == res2.winner == "A"
    assert res2.vice_president.vice_president == "vp-anna"


def test_vp_senate_elects_other_ticket_than_president() -> None:
    senate = ["PB"] * 13 + ["PA"] * 11
    res = run(three_way_outcome(), ELIMINATION_HOUSE, ContingentElectionConfig(), senate=senate)
    assert res.winner == "A" and res.vice_president.winner_line == "B"
    assert res.vice_president.vice_president == "vp-bram"


def test_finalist_selection_ev_ties_broken_by_popular_vote() -> None:
    ev = {"P1": 60, "P2": 60, "P3": 30, "P4": 24}
    tabs = {
        p: tabulate_totals(p, ["A", "B", "C", "D"], [10 if k == w else 1 for k in "ABCD"])
        for p, w in zip(ev, "ABCD", strict=True)
    }
    out = allocate(tabs, ev)
    assert out.needs_contingent
    assert select_finalists(out, {"A": 5, "B": 9, "C": 1, "D": 99}, 3, seed=1) == ["B", "A", "C"]
    assert select_finalists(out, {"A": 5, "B": 9, "C": 1, "D": 99}, 2, seed=1) == ["B", "A"]
    # exact EV + PV tie → seeded lot, deterministic
    a = select_finalists(out, {"A": 5, "B": 5}, 2, seed=3)
    assert a == select_finalists(out, {"A": 5, "B": 5}, 2, seed=3) and set(a) == {"A", "B"}


def test_deterministic_and_serialisable() -> None:
    cfg = ContingentElectionConfig()
    a = run(three_way_outcome(), ELIMINATION_HOUSE, cfg, seed=7).to_dict()
    b = run(three_way_outcome(), ELIMINATION_HOUSE, cfg, seed=7).to_dict()
    assert a == b
    assert a["mode"] == "province_delegations" and len(a["rounds"]) == 2
    assert a["config"]["finalists"] == 3


def test_full_pipeline_87_87_tie_goes_to_contingent() -> None:
    out = two_way_tie_outcome()
    house = deleg(
        {
            p: [("PA", HOUSE_SEATS[p])]
            if p in {"GR", "FR", "DR", "FL", "ZE", "LI", "OV"}
            else [("PB", HOUSE_SEATS[p])]
            for p in PROVINCES
        }
    )
    res = run(out, house, ContingentElectionConfig())
    assert res.finalists == ["A", "B"]  # only two tickets received electoral votes
    assert res.rounds[0].tallies == {"A": 7, "B": 5}
    assert res.winner == "A" and res.outcome == OUTCOME_ELECTED and res.ballots == 1


def test_rejects_decided_electoral_college() -> None:
    out = allocate({p: _tab(p, "A") for p in PROVINCES}, EV)
    assert out.winner == "A"
    with pytest.raises(ElectionError):
        run(out, ELIMINATION_HOUSE, ContingentElectionConfig())
    with pytest.raises(ElectionError):
        run(three_way_outcome(), ELIMINATION_HOUSE, ContingentElectionConfig(), finalists_n=1)
