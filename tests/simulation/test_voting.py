"""Election simulation: reconciliation, determinism, turnout, ballot composition, correlation."""

from __future__ import annotations

import json
import time

import numpy as np
import pytest

from app.core.constitution import ElectoralSystem, RaceType
from app.elections.types import BallotLine, RaceSpec
from app.simulation.races import presidential_races, ticket_lines
from app.simulation.structural import StructuralModel
from app.simulation.voting import (
    ElectionContext,
    IncumbentInfo,
    draw_shocks,
    expected_race_shares,
    race_expectation,
    simulate_election,
)


def _race(frame, key, lines, units=None, race_type=RaceType.GOVERNOR, **kw) -> RaceSpec:
    idx = np.arange(frame.n_units) if units is None else np.asarray(units)
    return RaceSpec(key=key, race_type=race_type, unit_index=idx, lines=lines, **kw)


def _party_lines(codes, **kw):
    return [BallotLine(key=c.lower(), party_code=c, candidate_key=None, label=c, **kw) for c in codes]


def _share(rv, key):
    tot = rv.totals()
    return tot[rv.line_keys.index(key)] / tot.sum()


# --------------------------------------------------------------------------- reconciliation
def test_full_election_reconciles(demo_model, full_races):
    assert len(full_races) == 12 + 1 + 150 + 8 + 12
    draw = simulate_election(demo_model, full_races, seed=42)
    assert set(draw.races) == {r.key for r in full_races}
    t = draw.turnout
    assert (t.ballots_cast <= t.eligible).all() and (t.ballots_cast >= 0).all()
    for r in full_races:
        rv = draw.races[r.key]
        rv.check()
        assert rv.line_keys == r.line_keys
        assert rv.expected_shares.shape == rv.votes.shape
        assert np.allclose(rv.expected_shares.sum(axis=1), 1.0)
        assert ((rv.expected_turnout > 0) & (rv.expected_turnout < 1)).all()
        # turnout is election-wide: every race sees the same ballots in a unit
        assert np.array_equal(rv.ballots_cast, t.ballots_cast[rv.unit_index])
    # the national presidential race is exactly the sum of the province contests
    parent = draw.races["PRES"]
    children = sum(draw.races[f"PRES-{pv}"].totals() for pv in demo_model.frame.province_codes)
    assert np.array_equal(parent.totals(), children)
    assert np.array_equal(np.sort(parent.unit_index), np.arange(demo_model.frame.n_units))
    json.dumps(draw.environment)  # JSON-serialisable audit record


def test_determinism_and_seed_sensitivity(demo_model, full_races):
    a = simulate_election(demo_model, full_races, seed=7)
    b = simulate_election(demo_model, full_races, seed=7)
    c = simulate_election(demo_model, full_races, seed=8)
    assert np.array_equal(a.turnout.ballots_cast, b.turnout.ballots_cast)
    for k in a.races:
        assert np.array_equal(a.races[k].votes, b.races[k].votes)
        assert np.array_equal(a.races[k].blank, b.races[k].blank)
    assert json.dumps(a.environment, sort_keys=True) == json.dumps(b.environment, sort_keys=True)
    assert not np.array_equal(a.turnout.ballots_cast, c.turnout.ballots_cast)
    assert not np.array_equal(a.races["PRES"].votes, c.races["PRES"].votes)
    # expectations do not depend on the seed
    assert np.array_equal(a.races["PRES-NB"].expected_shares, c.races["PRES-NB"].expected_shares)


def test_unrelated_races_do_not_perturb_draws(demo_model, full_races):
    """Keyed RNG streams: removing races leaves the remaining races' votes unchanged."""
    subset = [r for r in full_races if r.race_type in (RaceType.GOVERNOR,)]
    a = simulate_election(demo_model, full_races, seed=3)
    b = simulate_election(demo_model, subset, seed=3)
    for r in subset:
        assert np.array_equal(a.races[r.key].votes, b.races[r.key].votes)


def test_expected_race_shares_match_draw_expectation(demo_model, full_races):
    ctx = ElectionContext.from_scenario(demo_model.scenario)
    draw = simulate_election(demo_model, full_races, seed=1, context=ctx)
    for key in ("PRES-ZH", "GOV-NB", "SEN-UT-1"):
        race = next(r for r in full_races if r.key == key)
        exp = expected_race_shares(demo_model, race, ctx)
        assert exp.shape == (len(race.unit_index), len(race.lines))
        assert np.allclose(exp, draw.races[key].expected_shares)


# --------------------------------------------------------------------------- turnout / blank / invalid
def test_turnout_close_to_target(small_model, frame):
    race = _race(frame, "GOV-ALL", _party_lines(small_model.party_codes))
    realised = []
    for s in range(5):
        d = simulate_election(small_model, [race], seed=s)
        realised.append(d.turnout.ballots_cast.sum() / d.turnout.eligible.sum())
    env = small_model.scenario.environment
    assert abs(np.mean(realised) - env.turnout_base) < 0.04
    assert np.std(realised) > 0.001  # national turnout shock varies


def test_blank_and_invalid_rates(small_model, frame):
    race = _race(frame, "GOV-ALL", _party_lines(small_model.party_codes))
    d = simulate_election(small_model, [race], seed=5)
    rv = d.races["GOV-ALL"]
    env = small_model.scenario.environment
    b = rv.ballots_cast.sum()
    assert rv.invalid.sum() / b == pytest.approx(env.invalid_rate, rel=0.25)
    assert rv.blank.sum() / b == pytest.approx(env.blank_rate, rel=0.25)  # every party present → no undervote


def test_absent_parties_undervote_and_transfer(small_model, frame):
    full = _race(frame, "GOV-FULL", _party_lines(small_model.party_codes))
    part = _race(frame, "GOV-PART", _party_lines(["LEFT", "RIGHT"]))
    d = simulate_election(small_model, [full, part], seed=9)
    f, p = d.races["GOV-FULL"], d.races["GOV-PART"]
    assert p.blank.sum() > 3 * f.blank.sum()
    assert _share(p, "left") > _share(f, "left")
    assert _share(p, "right") > _share(f, "right")


def test_withdrawn_line_keeps_only_residual(small_model, frame):
    lines = _party_lines(["LEFT", "RIGHT", "CENTRE"])
    wd = [lines[0], lines[1], BallotLine(key="centre", party_code="CENTRE", label="C", withdrawn=True)]
    a = simulate_election(small_model, [_race(frame, "GOV-A", lines)], seed=2).races["GOV-A"]
    b = simulate_election(small_model, [_race(frame, "GOV-A", wd)], seed=2).races["GOV-A"]
    resid = small_model.config.candidates.withdrawn_residual_share
    assert _share(b, "centre") < 0.2 * _share(a, "centre")
    assert _share(b, "centre") == pytest.approx(resid * _share(a, "centre"), rel=0.6)
    assert _share(b, "left") > _share(a, "left")


def test_strategic_voting_shrinks_minor_parties(frame, make_doc, no_regions):
    lines = _party_lines(["LEFT", "RIGHT", "CENTRE", "RURAL", "MINOR"])
    shares = {}
    for sv in (0.0, 1.0):
        doc = make_doc(environment={"turnout_base": 0.75, "strategic_voting": sv})
        m = StructuralModel.build(frame, doc, regions=no_regions)
        plan = race_expectation(m, _race(frame, "GOV-X", lines, electoral_system=ElectoralSystem.FPTP))
        shares[sv] = dict(zip([ln.key for ln in lines], plan.jurisdiction_shares, strict=True))
        if sv == 1.0:
            assert plan.defect[[ln.key for ln in lines].index("minor")] > 0.5
    assert shares[1.0]["minor"] < 0.5 * shares[0.0]["minor"]
    assert shares[1.0]["left"] > shares[0.0]["left"]
    # proportional races are not subject to strategic voting
    doc = make_doc(environment={"turnout_base": 0.75, "strategic_voting": 1.0})
    m = StructuralModel.build(frame, doc, regions=no_regions)
    plan = race_expectation(
        m,
        _race(
            frame,
            "COUNCIL-X",
            lines,
            race_type=RaceType.MUNICIPAL_COUNCIL,
            electoral_system=ElectoralSystem.PROPORTIONAL_DHONDT,
            seats=9,
        ),
    )
    assert not plan.defect.any()


def test_independent_candidates(small_model, frame):
    base = _party_lines(["LEFT", "RIGHT", "CENTRE"])
    weak = BallotLine(key="ind", party_code=None, label="Ind", quality=-1.0)
    strong = BallotLine(key="ind", party_code=None, label="Ind", quality=2.0)
    a = simulate_election(small_model, [_race(frame, "GOV-I", [*base, weak])], seed=4).races["GOV-I"]
    b = simulate_election(small_model, [_race(frame, "GOV-I", [*base, strong])], seed=4).races["GOV-I"]
    assert 0 < _share(a, "ind") < _share(b, "ind")


def test_candidate_effects_incumbency_home_and_midterm(small_model, frame):
    units = np.arange(frame.n_units)
    plain = _party_lines(["LEFT", "RIGHT", "CENTRE"])
    inc = [BallotLine(key="left", party_code="LEFT", label="L", incumbent=True), *plain[1:]]
    home = [BallotLine(key="left", party_code="LEFT", label="L", home_municipality="GM0501"), plain[1]]
    ctx = ElectionContext(year=2028)
    e0 = expected_race_shares(small_model, _race(frame, "HOUSE-X", plain, race_type=RaceType.HOUSE), ctx)
    e1 = expected_race_shares(small_model, _race(frame, "HOUSE-X", inc, race_type=RaceType.HOUSE), ctx)
    assert (e1[:, 0] > e0[:, 0]).all()
    # two-line race (no strategic voting): the bonus is purely local
    e2 = expected_race_shares(small_model, _race(frame, "HOUSE-X", home, race_type=RaceType.HOUSE), ctx)
    e0 = expected_race_shares(small_model, _race(frame, "HOUSE-X", plain[:2], race_type=RaceType.HOUSE), ctx)
    in_home = frame.unit_muni[units] == frame.muni_index("GM0501")
    assert (e2[in_home, 0] > e0[in_home, 0]).all()
    assert np.allclose(e2[~in_home, 0], e0[~in_home, 0])
    e0 = expected_race_shares(small_model, _race(frame, "HOUSE-X", plain, race_type=RaceType.HOUSE), ctx)
    mid = ElectionContext(year=2026, election_type="midterm", president_party="LEFT")
    e3 = expected_race_shares(small_model, _race(frame, "HOUSE-X", plain, race_type=RaceType.HOUSE), mid)
    assert (e3[:, 0] < e0[:, 0]).all()
    # open seat: the incumbent's party gets a small bonus
    ctx_open = ElectionContext(year=2028, incumbents={"HOUSE-X": IncumbentInfo(party="RIGHT", running=False)})
    e4 = expected_race_shares(small_model, _race(frame, "HOUSE-X", plain, race_type=RaceType.HOUSE), ctx_open)
    assert (e4[:, 1] > e0[:, 1]).all()


def test_presidential_home_bonuses(small_model, frame, small_doc):
    lines = ticket_lines(small_doc, frame)
    races = presidential_races(frame, lines, include_national=False)
    ctx = ElectionContext.from_scenario(small_doc)
    gr = next(r for r in races if r.key == "PRES-GR")  # LEFT presidential candidate lives in GM0101 (GR)
    with_bonus = race_expectation(small_model, gr, ctx).jurisdiction_shares[0]
    ctx0 = ElectionContext.from_scenario(small_doc, home_province_bonus=0.0)
    no_bonus = race_expectation(small_model, gr, ctx0).jurisdiction_shares[0]
    assert with_bonus > no_bonus


def test_campaign_and_turnout_effects(small_model, frame):
    lines = _party_lines(["LEFT", "RIGHT", "CENTRE"])
    race = _race(frame, "GOV-C", lines)
    base = race_expectation(small_model, race, ElectionContext(year=2028))
    ctx = ElectionContext(
        year=2028,
        campaign_effects={
            "GOV-C": {"RIGHT": 0.2},
            ("province", "NB"): {"CENTRE": 0.3},
            "municipality:GM0101": {"LEFT": 0.2},
        },
        turnout_effects={("province", "LI"): 0.5},
        national_shifts={"LEFT": 0.05},
    )
    eff = race_expectation(small_model, race, ctx)
    assert eff.jurisdiction_shares[1] > base.jurisdiction_shares[1]
    nb = frame.unit_province == frame.province_index("NB")
    assert (eff.expected_shares[nb, 2] > base.expected_shares[nb, 2]).all()
    li = frame.unit_province == frame.province_index("LI")
    assert (eff.expected_turnout[li] > base.expected_turnout[li]).all()
    assert np.allclose(eff.expected_turnout[~li], base.expected_turnout[~li], atol=0.01)


def test_probabilistic_events(frame, make_doc, no_regions):
    env = {
        "turnout_base": 0.75,
        "events": [
            {"name": "scandal", "probability": 0.5, "national": {"RIGHT": -0.4}},
            {"name": "sure", "national": {"LEFT": 0.1}},
        ],
    }
    m = StructuralModel.build(frame, make_doc(environment=env), regions=no_regions)
    occurred = []
    for s in range(12):
        rec = draw_shocks(m, s).record["events"]
        assert rec[1]["occurred"] is True
        occurred.append(rec[0]["occurred"])
    assert 0 < sum(occurred) < 12
    # the expectation contains probability × effect
    assert m.env_national[m.party_index["RIGHT"]] == pytest.approx(-0.2)


def test_realised_shocks_are_geographically_correlated(demo_model, frame):
    """Neighbouring municipalities' realised shocks are more correlated than random pairs."""
    xy = frame.muni_xy
    d = np.sqrt(((xy[:, None, :] - xy[None, :, :]) ** 2).sum(-1))
    np.fill_diagonal(d, np.inf)
    nn = d.argmin(axis=1)
    rand = np.random.default_rng(3).permutation(len(xy))
    near, far = [], []
    for s in range(3):
        rec = draw_shocks(demo_model, s).record["municipalities"]
        tot = np.asarray(rec["spatial"]) + np.asarray(rec["iid"])
        for j in range(tot.shape[1]):
            near.append(np.corrcoef(tot[:, j], tot[nn, j])[0, 1])
            far.append(np.corrcoef(tot[:, j], tot[rand, j])[0, 1])
    assert np.mean(near) > np.mean(far) + 0.2


def test_environment_record_contents(demo_model, full_races):
    d = simulate_election(demo_model, full_races[:13], seed=2)
    env = d.environment
    assert set(env["shocks"]["national"]) == set(demo_model.party_codes)
    assert env["model_fingerprint"] == demo_model.fingerprint
    assert "PRES-ZH" in env["race_line_shocks"]
    assert 0.3 < env["shocks"]["turnout"]["realised"] < 1.0


def test_unknown_party_line_is_rejected(small_model, frame):
    from app.core.errors import ElectionError

    with pytest.raises(ElectionError):
        simulate_election(small_model, [_race(frame, "GOV-Z", _party_lines(["LEFT", "NOPE"]))], seed=1)


def test_performance_smoke(demo_model, full_races):
    simulate_election(demo_model, full_races, seed=100)  # warm caches
    t0 = time.perf_counter()
    simulate_election(demo_model, full_races, seed=101)
    elapsed = time.perf_counter() - t0
    assert elapsed < 3.0, f"simulation of {len(full_races)} races took {elapsed:.2f}s"
