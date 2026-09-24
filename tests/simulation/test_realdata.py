"""Checks against the REAL CBS geography (skipped when the processed store is not built).

These are regression guards for the demo scenarios' qualitative electoral geography — all
parties and numbers are FICTIONAL — and the full-country performance target.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from app.districts.apportionment import apportion
from app.scenarios.loader import load_scenario, validate_scenario
from app.simulation.candidates import generate_down_ballot
from app.simulation.races import (
    governor_slots,
    house_slots,
    presidential_races,
    races_from_candidates,
    senate_slots,
    ticket_lines,
)
from app.simulation.regions import resolve_regions
from app.simulation.structural import StructuralModel
from app.simulation.voting import ElectionContext, expected_race_shares, simulate_election

pytestmark = pytest.mark.realdata

SLUGS = ("founding-2024", "midterm-2026", "demo-2028")


@pytest.fixture(scope="module")
def real_model_2028(real_frame):  # type: ignore[no-untyped-def]
    return StructuralModel.build(real_frame, load_scenario("demo-2028"), strict=True)


def test_regions_resolve_on_real_frame(real_frame):
    r = resolve_regions(real_frame)
    assert r.resolution_rate >= 0.95, r.unresolved
    assert r.muni_mask("catholic_south").sum() > 50
    assert 15 <= r.muni_mask("bible_belt").sum() <= 40


@pytest.mark.parametrize("slug", SLUGS)
def test_demo_scenarios_fit_real_geography(real_frame, slug):
    doc = load_scenario(slug)
    assert validate_scenario(doc, real_frame) == []
    m = StructuralModel.build(real_frame, doc, strict=True)
    assert m.calibration.converged
    assert not m.warnings


def test_demo_geography_is_plausible(real_model_2028, real_frame):
    m = real_model_2028
    mun = m.municipality_shares(environment=False)
    prov = m.province_shares(environment=False)
    nat = m.national_shares(environment=False)

    def share(name: str, party: str) -> float:
        return float(mun.loc[real_frame.muni_codes[real_frame.muni_names.index(name)], party])

    assert share("Staphorst", "RV") > 0.3 and share("Urk", "RV") > 0.3
    assert share("Amsterdam", "PA") > 1.3 * nat["PA"]
    assert share("Wassenaar", "VLP") > 1.5 * nat["VLP"]
    assert share("Kerkrade", "NVB") > 1.5 * nat["NVB"]
    assert share("Tubbergen", "CVU") > 1.5 * nat["CVU"]
    assert prov.loc["DR", "PLB"] > 2 * nat["PLB"]
    assert prov.loc["LI", "RV"] < 0.005
    assert prov.loc["NB", "CVU"] > nat["CVU"] and prov.loc["LI", "NVB"] > nat["NVB"]
    t = m.expected_turnout_by("municipality", environment=False)
    assert t.min() > 0.5 and t.max() < 0.97


def test_full_country_performance(real_frame, real_model_2028):
    m = real_model_2028
    doc = m.scenario
    f = real_frame
    pops = f.province_population().astype(float)
    seats = np.maximum(1, np.round(pops / pops.sum() * 150)).astype(int)
    seats[int(np.argmax(pops))] += 150 - seats.sum()
    ud = np.empty(f.n_units, dtype=object)
    for p, pv in enumerate(f.province_codes):
        idx = f.units_in_province(p)
        order = idx[np.argsort(f.unit_xy[idx, 1], kind="stable")]
        d = (np.arange(len(order)) * seats[p]) // len(order)
        for k in range(seats[p]):
            ud[order[d == k]] = f"{pv}-{k + 1:02d}"
    sen = [(pv, 1) for pv in ("NH", "UT", "FR", "FL")] + [(pv, 2) for pv in ("ZH", "OV", "LI", "ZE")]
    slots = house_slots(f, ud) + senate_slots(f, sen) + governor_slots(f)
    fielded = generate_down_ballot(m, slots, seed=doc.scenario.seed)
    ev = {pv: int(s) + 2 for pv, s in zip(f.province_codes, seats, strict=True)}
    races = presidential_races(f, ticket_lines(doc, f), ev) + races_from_candidates(slots, fielded)
    assert len(races) == 183
    simulate_election(m, races, seed=1)  # warm-up (Cholesky cache)
    t0 = time.perf_counter()
    draw = simulate_election(m, races, seed=2)
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"{elapsed:.2f}s"
    for rv in draw.races.values():
        rv.check()


# --------------------------------------------------------------------------- demo-2028 Electoral College
#: Huntington–Hill electoral votes on the REAL 2025 frame (House seats + 2).
REAL_EV = {
    "ZH": 34, "NH": 27, "NB": 24, "GE": 20, "UT": 14, "OV": 12,
    "LI": 11, "FR": 8, "GR": 7, "DR": 6, "FL": 6, "ZE": 5,
}  # fmt: skip
N_DRAWS = 300


@pytest.fixture(scope="module")
def ev_outcomes_2028(real_frame, real_model_2028):  # type: ignore[no-untyped-def]
    """Winner-take-all EV outcomes of demo-2028 over N_DRAWS shock draws (campaigns off)."""
    f, m = real_frame, real_model_2028
    doc = m.scenario
    pops = {pv: int(p) for pv, p in zip(f.province_codes, f.province_population(), strict=True)}
    ev = apportion(pops, 150).electoral_votes
    lines = ticket_lines(doc, f)
    party = {ln.key: ln.party_code for ln in lines}
    races = presidential_races(f, lines, ev, include_national=False)
    ctx = ElectionContext.from_scenario(doc)
    draws = []
    for seed in range(1, N_DRAWS + 1):
        d = simulate_election(m, races, seed=seed, context=ctx)
        winners = {
            r.province_code: party[d.races[r.key].line_keys[int(np.argmax(d.races[r.key].totals()))]]
            for r in races
        }
        turnout = d.turnout.ballots_cast.sum() / d.turnout.eligible.sum()
        draws.append((winners, float(turnout)))
    return ev, draws


def test_real_electoral_votes_are_huntington_hill(ev_outcomes_2028):
    ev, _ = ev_outcomes_2028
    assert ev == REAL_EV and sum(ev.values()) == 174


def test_demo_2028_electoral_college_design(ev_outcomes_2028):
    """FICTIONAL design targets of the flagship demo (docs/SIMULATION.md §7.1): two tickets with
    real paths to 88, a minority chance of a contingent election, several competitive provinces,
    third parties carrying provinces and a plausible turnout."""
    ev, draws = ev_outcomes_2028
    n = len(draws)
    outright = {"PA": 0, "VLP": 0}
    contingent = third = 0
    prov_wins: dict[str, dict[str, int]] = {pv: {} for pv in ev}
    for winners, _ in draws:
        tot: dict[str, int] = {}
        for pv, p in winners.items():
            tot[p] = tot.get(p, 0) + ev[pv]
            prov_wins[pv][p] = prov_wins[pv].get(p, 0) + 1
        top, top_ev = max(tot.items(), key=lambda kv: kv[1])
        if top_ev >= 88:
            outright[top] = outright.get(top, 0) + 1
        else:
            contingent += 1
        third += any(p not in ("PA", "VLP") for p in winners.values())
    assert outright["PA"] / n >= 0.25 and outright["VLP"] / n >= 0.30, outright
    assert 0.06 <= contingent / n <= 0.20, contingent / n
    assert sum(outright.values()) + contingent == n
    competitive = [pv for pv, w in prov_wins.items() if 0.25 <= max(w.values()) / n <= 0.75]
    assert len(competitive) >= 5, prov_wins
    assert {"ZH", "GE", "UT"} <= set(competitive)
    assert third / n >= 0.5  # the NVB (Limburg) and CVU (Brabant/Overijssel) carry provinces
    assert prov_wins["LI"].get("NVB", 0) / n > 0.6
    assert prov_wins["NH"].get("PA", 0) / n > 0.9 and prov_wins["GR"].get("PA", 0) / n > 0.9
    assert sum(prov_wins["NB"].get(p, 0) for p in ("CVU",)) > 0  # CVU competes in Brabant
    turnout = np.array([t for _, t in draws])
    assert 0.75 <= turnout.mean() <= 0.82 and turnout.std() > 0.003


def test_demo_2028_popular_vote_is_plausible(real_model_2028, real_frame):
    nat = real_model_2028.national_shares()
    assert nat["PA"] > nat["VLP"] > nat["NVB"] and 0.2 < nat["PA"] < 0.32
    t = float(real_model_2028.expected_turnout_by("national").iloc[0])
    assert 0.75 <= t <= 0.82


@pytest.mark.parametrize(("slug", "lo", "hi"), [("founding-2024", 0.76, 0.84), ("midterm-2026", 0.58, 0.68)])
def test_other_demo_turnout(real_frame, slug, lo, hi):
    m = StructuralModel.build(real_frame, load_scenario(slug), strict=True)
    assert lo <= float(m.expected_turnout_by("national").iloc[0]) <= hi


def test_midterm_president_party_penalty_on_real_frame(real_frame):
    doc = load_scenario("midterm-2026")
    m = StructuralModel.build(real_frame, doc, strict=True)
    mid = ElectionContext.from_scenario(doc)
    assert mid.president_party == "VLP"
    lines = presidential_races(real_frame, ticket_lines(load_scenario("demo-2028"), real_frame))[0].lines
    from app.core.constitution import RaceType
    from app.elections.types import RaceSpec

    race = RaceSpec(
        key="HOUSE-UT-01",
        race_type=RaceType.HOUSE,
        unit_index=real_frame.units_in_province(real_frame.province_index("UT")),
        lines=[ln for ln in lines if ln.party_code in ("VLP", "PA", "DM")],
    )
    with_penalty = expected_race_shares(m, race, mid)
    without = expected_race_shares(m, race, ElectionContext.from_scenario(doc, election_type="general"))
    j = [ln.party_code for ln in race.lines].index("VLP")
    assert (with_penalty[:, j] < without[:, j]).all()
