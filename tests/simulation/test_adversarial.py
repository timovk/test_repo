"""Adversarial tests for the political model and the election simulation (synthetic country).

Each test targets a failure mode found in review: silent data loss, double-counted effects,
ignored scenario fields, invalid numerics and invariants of the linear ballot maps.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.core.constitution import ElectoralSystem, RaceType
from app.core.errors import ElectionError, ScenarioError
from app.core.rng import make_rng
from app.elections.types import BallotLine, RaceSpec
from app.scenarios.schema import CandidateSpec, ContestRule, DownBallotSpec, ScenarioDocument
from app.simulation.candidates import RaceSlot, generate_race_candidates
from app.simulation.config import model_config_from_dict
from app.simulation.races import presidential_races, races_from_candidates, ticket_lines
from app.simulation.regions import regions_config_from_dict
from app.simulation.structural import StructuralModel, routing_matrix
from app.simulation.voting import (
    ElectionContext,
    IncumbentInfo,
    draw_shocks,
    expected_party_state,
    expected_race_shares,
    race_expectation,
    race_line_shocks,
    realised_party_state,
    simulate_election,
)


def _data(make_doc, **overrides) -> dict:
    """Plain (JSON) data of the small unit-test scenario with top-level overrides."""
    return make_doc(**overrides).model_dump(mode="json")


def _lines(codes, **kw):
    return [BallotLine(key=c.lower(), party_code=c, label=c, **kw) for c in codes]


def _race(frame, key, lines, units=None, race_type=RaceType.GOVERNOR, **kw) -> RaceSpec:
    idx = np.arange(frame.n_units) if units is None else units
    return RaceSpec(key=key, race_type=race_type, unit_index=idx, lines=lines, **kw)


# --------------------------------------------------------------------------- context
def test_from_scenario_president_party_resolution(make_doc):
    data = _data(make_doc, environment={"turnout_base": 0.75, "president_party": "RIGHT"})
    doc = ScenarioDocument.model_validate(data)
    assert ElectionContext.from_scenario(doc).president_party == "RIGHT"  # environment field
    assert ElectionContext.from_scenario(doc, president_party="LEFT").president_party == "LEFT"
    data["environment"]["president_party"] = None
    data["candidates"][2]["incumbent_office"] = "PRES"  # right-pres
    doc = ScenarioDocument.model_validate(data)
    assert ElectionContext.from_scenario(doc).president_party == "RIGHT"  # derived from candidates
    assert ElectionContext.from_scenario(make_doc()).president_party is None


def test_from_scenario_accepts_field_overrides(small_doc):
    ctx = ElectionContext.from_scenario(small_doc, year=2032, election_type="midterm", home_province_bonus=0)
    assert (ctx.year, ctx.election_type, ctx.home_province_bonus) == (2032, "midterm", 0)
    assert ctx.vp_home_province_bonus == small_doc.president.vp_home_province_bonus


def test_environment_president_party_drives_midterm_penalty(frame, make_doc, no_regions):
    data = _data(make_doc, environment={"turnout_base": 0.75, "president_party": "LEFT"})
    data["scenario"]["election_type"] = "midterm"
    m = StructuralModel.build(frame, ScenarioDocument.model_validate(data), regions=no_regions)
    race = _race(frame, "HOUSE-X", _lines(["LEFT", "RIGHT"]), race_type=RaceType.HOUSE)
    mid = expected_race_shares(m, race)  # default context from the scenario
    gen = expected_race_shares(m, race, ElectionContext(year=2026))
    assert (mid[:, 0] < gen[:, 0]).all()


def test_unknown_president_party_is_rejected(frame, make_doc, no_regions):
    data = _data(make_doc, environment={"turnout_base": 0.75, "president_party": "GHOST"})
    with pytest.raises(ScenarioError, match="president_party"):
        StructuralModel.build(frame, ScenarioDocument.model_validate(data), regions=no_regions)


# --------------------------------------------------------------------------- campaign effects
def test_party_list_race_effect_applied_once(small_model, frame):
    """Council lines use key == party code: a race effect keyed by it must count once."""
    lines = [BallotLine(key=c, party_code=c, label=c) for c in ("LEFT", "RIGHT", "CENTRE")]
    race = _race(
        frame,
        "COUNCIL-GM0101",
        lines,
        units=frame.units_in_muni(0),
        race_type=RaceType.MUNICIPAL_COUNCIL,
        electoral_system=ElectoralSystem.PROPORTIONAL_DHONDT,
        seats=9,
    )
    base = race_expectation(small_model, race, ElectionContext(year=2028))
    eff = race_expectation(
        small_model, race, ElectionContext(year=2028, campaign_effects={race.key: {"LEFT": 0.1}})
    )
    assert np.allclose(eff.delta[:, 0] - base.delta[:, 0], 0.1)
    # a line key and a different party key both apply (line-specific + party-wide effect)
    lines2 = _lines(["LEFT", "RIGHT"])
    race2 = _race(frame, "GOV-Y", lines2)
    ctx = ElectionContext(year=2028, campaign_effects={"GOV-Y": {"left": 0.05, "LEFT": 0.02}})
    d = race_expectation(small_model, race2, ctx).delta - race_expectation(small_model, race2).delta
    assert np.allclose(d[:, 0], 0.07)


def test_party_turnout_effects_mobilise_supporters(small_model, frame):
    lines = _lines(["LEFT", "RIGHT", "CENTRE"])
    race = _race(frame, "GOV-T", lines)
    base = race_expectation(small_model, race, ElectionContext(year=2028))
    ctx = ElectionContext(year=2028, party_turnout_effects={("province", "NB"): {"RIGHT": 0.5}})
    eff = race_expectation(small_model, race, ctx)
    nb = frame.unit_province == frame.province_index("NB")
    assert (eff.expected_turnout[nb] > base.expected_turnout[nb]).all()
    assert np.allclose(eff.expected_turnout[~nb], base.expected_turnout[~nb])
    assert (eff.expected_shares[nb, 1] > base.expected_shares[nb, 1]).all()
    # the same effect reaches the realised draw
    a = simulate_election(small_model, [race], seed=4, context=ElectionContext(year=2028))
    b = simulate_election(small_model, [race], seed=4, context=ctx)
    assert b.turnout.ballots_cast[nb].sum() > a.turnout.ballots_cast[nb].sum()


def test_district_level_effects_need_unit_district(small_model, frame):
    lines = _lines(["LEFT", "RIGHT"])
    race = _race(frame, "GOV-D", lines)
    ud = np.where(frame.unit_province == 0, "GR-01", "XX-01").astype(object)
    ctx = ElectionContext(
        year=2028, campaign_effects={("district", "GR-01"): {"LEFT": 0.3}}, unit_district=ud
    )
    base = race_expectation(small_model, race, ElectionContext(year=2028)).expected_shares
    eff = race_expectation(small_model, race, ctx).expected_shares
    inside = frame.unit_province == 0
    assert (eff[inside, 0] > base[inside, 0]).all() and np.allclose(eff[~inside], base[~inside])
    with pytest.raises(ElectionError, match="unit_district"):
        race_expectation(
            small_model, race, ElectionContext(year=2028, campaign_effects={"district:GR-01": {}})
        )
    with pytest.raises(ElectionError, match="unknown district"):
        race_expectation(
            small_model,
            race,
            ElectionContext(
                year=2028, campaign_effects={("district", "ZZ-99"): {"LEFT": 0.1}}, unit_district=ud
            ),
        )


def test_non_numeric_effects_are_rejected(small_model, frame):
    race = _race(frame, "GOV-N", _lines(["LEFT", "RIGHT"]))
    for ctx in (
        ElectionContext(year=2028, campaign_effects={"national": {"LEFT": "a lot"}}),
        ElectionContext(year=2028, turnout_effects={"province:NB": float("nan")}),
        ElectionContext(year=2028, campaign_effects={"GOV-N": {"LEFT": None}}),
    ):
        with pytest.raises(ElectionError, match="not"):
            race_expectation(small_model, race, ctx)


# --------------------------------------------------------------------------- units and races
def test_boolean_masks_are_not_silently_cast(small_model, frame):
    mask = frame.unit_province == frame.province_index("UT")
    idx = np.flatnonzero(mask)
    a = small_model.party_state(mask)
    b = small_model.party_state(idx)
    assert a.turnout.shape == (len(idx),) and np.array_equal(a.vote_share, b.vote_share)
    assert np.array_equal(expected_party_state(small_model, None, mask).turnout, b.turnout)
    r_mask = race_expectation(small_model, _race(frame, "GOV-UT", _lines(["LEFT", "RIGHT"]), units=mask))
    r_idx = race_expectation(small_model, _race(frame, "GOV-UT", _lines(["LEFT", "RIGHT"]), units=idx))
    assert np.array_equal(r_mask.units, idx) and np.allclose(r_mask.expected_shares, r_idx.expected_shares)
    with pytest.raises(ValueError, match="out of range"):
        small_model.party_state([0, frame.n_units])


def test_duplicate_or_out_of_range_units_rejected(small_model, frame):
    lines = _lines(["LEFT", "RIGHT"])
    with pytest.raises(ElectionError, match="duplicate unit"):
        simulate_election(small_model, [_race(frame, "GOV-D", lines, units=np.array([3, 1, 3]))], seed=1)
    with pytest.raises(ElectionError, match="out of range"):
        simulate_election(small_model, [_race(frame, "GOV-D", lines, units=np.array([-1, 2]))], seed=1)
    # unsorted but unique units are fine
    d = simulate_election(small_model, [_race(frame, "GOV-D", lines, units=np.array([5, 2, 9]))], seed=1)
    d.races["GOV-D"].check()


def test_partial_presidential_children_are_rejected(small_model, small_doc, frame):
    races = presidential_races(frame, ticket_lines(small_doc, frame))
    with pytest.raises(ElectionError, match="province contests cover"):
        simulate_election(small_model, [r for r in races if r.key in ("PRES", "PRES-GR", "PRES-NB")], seed=1)
    # without children the national race is simulated directly
    d = simulate_election(small_model, [r for r in races if r.key == "PRES"], seed=1)
    assert len(d.races["PRES"].unit_index) == frame.n_units


def test_draw_order_and_race_order_independence(small_model, small_doc, frame):
    races = [
        *presidential_races(frame, ticket_lines(small_doc, frame)),
        _race(frame, "GOV-ALL", _lines(small_model.party_codes)),
    ]
    a = simulate_election(small_model, races, seed=12)
    b = simulate_election(small_model, list(reversed(races)), seed=12)
    assert list(a.races) == [r.key for r in races]  # results keep the input order
    assert list(b.races) == [r.key for r in reversed(races)]
    for k in a.races:
        assert np.array_equal(a.races[k].votes, b.races[k].votes)
        assert np.array_equal(a.races[k].blank, b.races[k].blank)


def test_presidential_ticket_shock_is_national(small_model, small_doc, frame):
    """One national campaign: every province contest shares the ticket's performance shock."""
    races = presidential_races(frame, ticket_lines(small_doc, frame))
    shocks = {r.key: race_line_shocks(small_model, r, 3) for r in races}
    first = shocks["PRES-GR"]
    assert all(np.array_equal(v, first) for v in shocks.values())
    gov = _race(frame, "GOV-GR", races[0].lines)
    assert not np.array_equal(race_line_shocks(small_model, gov, 3), first)
    d = simulate_election(small_model, races, seed=3)
    assert d.environment["race_line_shocks"]["PRES-NB"] == d.environment["race_line_shocks"]["PRES-GR"]


def test_public_helpers_reproduce_the_draw(small_model, frame):
    """Monte Carlo reuse contract: realised state + RacePlan.shares + line shocks give the draw's
    expected province shares (only multinomial / binomial noise remains)."""
    ctx = ElectionContext.from_scenario(small_model.scenario)
    race = _race(frame, "GOV-ALL", _lines(small_model.party_codes))
    seed = 21
    d = simulate_election(small_model, [race], seed=seed, context=ctx)
    plan = race_expectation(small_model, race, ctx)
    st = realised_party_state(small_model, draw_shocks(small_model, seed, ctx), ctx)
    shares = plan.shares(st.vote_share[plan.units], race_line_shocks(small_model, race, seed))
    valid = d.races["GOV-ALL"].valid
    predicted = (shares * valid[:, None]).sum(axis=0) / valid.sum()
    realised = d.races["GOV-ALL"].totals() / valid.sum()
    assert np.abs(predicted - realised).max() < 0.004
    exp_ballots = (small_model.eligible * st.turnout).sum()
    assert d.turnout.ballots_cast.sum() == pytest.approx(exp_ballots, rel=0.01)


def test_degenerate_ballots(small_model, frame):
    one = simulate_election(small_model, [_race(frame, "GOV-1", _lines(["LEFT"]))], seed=2).races["GOV-1"]
    one.check()
    assert (one.votes[:, 0] == one.valid).all()
    wd = [BallotLine(key=c.lower(), party_code=c, withdrawn=True) for c in ("LEFT", "RIGHT")]
    allwd = simulate_election(small_model, [_race(frame, "GOV-W", wd)], seed=2).races["GOV-W"]
    allwd.check()
    empty = simulate_election(
        small_model, [_race(frame, "GOV-E", _lines(["LEFT", "RIGHT"]), units=[])], seed=2
    )
    assert empty.races["GOV-E"].votes.shape == (0, 2)


def test_zero_eligible_units(frame, small_doc, no_regions):
    import dataclasses

    elig = frame.unit_eligible.copy()
    elig[::7] = 0
    f2 = dataclasses.replace(frame, unit_eligible=elig)
    m = StructuralModel.build(f2, small_doc, regions=no_regions)
    d = simulate_election(m, [_race(f2, "GOV-Z", _lines(m.party_codes))], seed=1)
    rv = d.races["GOV-Z"]
    rv.check()
    assert (rv.ballots_cast[::7] == 0).all()


# --------------------------------------------------------------------------- ballot-map invariants
@pytest.mark.parametrize("seed", range(25))
def test_routing_and_strategic_maps_conserve_mass(small_model, frame, seed):
    """For random ballots (absent parties, withdrawn lines, independents, duplicate party lines):
    R rows + undervote = 1, G is a non-negative stochastic matrix, shares are distributions."""
    rng = make_rng(seed, "adversarial-ballot")
    codes = small_model.party_codes
    n_lines = int(rng.integers(1, 7))
    lines = []
    for j in range(n_lines):
        party = None if rng.random() < 0.2 else codes[int(rng.integers(len(codes)))]
        lines.append(
            BallotLine(
                key=f"l{j}",
                party_code=party,
                quality=float(rng.normal()),
                withdrawn=bool(rng.random() < 0.2),
                incumbent=bool(rng.random() < 0.1),
            )
        )
    units = np.sort(rng.choice(frame.n_units, size=60, replace=False))
    rtype = [RaceType.HOUSE, RaceType.GOVERNOR, RaceType.MAYOR][seed % 3]
    plan = race_expectation(small_model, _race(frame, "HOUSE-R", lines, units=units, race_type=rtype))
    assert np.allclose(plan.R.sum(axis=1) + plan.a, 1.0)
    assert (plan.R >= 0).all() and (plan.a >= 0).all()
    assert (plan.G >= -1e-15).all() and np.allclose(plan.G.sum(axis=1), 1.0)
    assert np.allclose(plan.expected_shares.sum(axis=1), 1.0)
    assert plan.jurisdiction_shares.sum() == pytest.approx(1.0)
    p = plan.blank_probability(np.full((len(units), len(codes)), 1.0 / len(codes)))
    assert ((p >= 0) & (p <= 0.95)).all()


def test_strategic_weight_above_one_is_clipped(frame, make_doc, no_regions):
    cfg = model_config_from_dict({"strategic": {"race_type_weights": {"GOVERNOR": 3.0}}})
    doc = make_doc(environment={"turnout_base": 0.75, "strategic_voting": 1.0})
    m = StructuralModel.build(frame, doc, model_config=cfg, regions=no_regions)
    plan = race_expectation(m, _race(frame, "GOV-S", _lines(m.party_codes)))
    assert plan.defect.max() <= 1.0 and (plan.G >= 0).all()


def test_routing_matrix_withdrawn_only_ballot():
    ideo = np.array([[-0.5, 0, 0], [0.5, 0, 0]])
    R, a = routing_matrix(
        ideo,
        np.array([0, 1]),
        ideo.copy(),
        np.array([True, True]),
        dim_weights=np.ones(3),
        temperature=0.3,
        abstain_share=0.1,
        withdrawn_residual=0.05,
        independent_extra_distance=0.4,
    )
    assert np.allclose(R.sum(axis=1) + a, 1.0) and (R >= 0).all()


# --------------------------------------------------------------------------- structural model
def test_invalid_calibration_targets_are_errors(frame, make_doc, no_regions):
    for calib, needle in (
        ({"national": {"MINOR": 0.0}}, "strictly between"),
        ({"national": {"MINOR": 1.2}}, "strictly between"),
        ({"provinces": {"NB": {"LEFT": -0.1}}}, r"\[0, 1\]"),
        ({"municipalities": {"GM0101": {"LEFT": float("nan")}}}, r"\[0, 1\]"),
    ):
        doc = make_doc(calibration=calib)
        with pytest.raises(ScenarioError, match=needle):
            StructuralModel.build(frame, doc, regions=no_regions)


def test_scenario_without_parties_is_an_error(frame, make_doc, no_regions):
    data = _data(make_doc, candidates=[], president={}, environment={}, parties=[])
    with pytest.raises(ScenarioError, match="no parties"):
        StructuralModel.build(frame, ScenarioDocument.model_validate(data), regions=no_regions)


def test_single_party_scenario(frame, make_doc, no_regions):
    data = _data(make_doc, candidates=[], president={}, environment={"turnout_base": 0.7})
    data["parties"] = data["parties"][:1]
    m = StructuralModel.build(frame, ScenarioDocument.model_validate(data), regions=no_regions)
    assert m.calibration.converged
    d = simulate_election(m, [_race(frame, "GOV-1", _lines(["LEFT"]))], seed=1)
    d.races["GOV-1"].check()


def test_nested_pinned_municipality_in_pinned_province(frame, make_doc, no_regions):
    """A pinned municipality inside a pinned province: municipality, province and national
    targets are all met (each steered through the units it controls)."""
    nb = frame.province_index("NB")
    muni = frame.muni_codes[int(frame.munis_in_province(nb)[0])]
    doc = make_doc(
        calibration={
            "national": {"LEFT": 0.28},
            "provinces": {"NB": {"RIGHT": 0.40, "RURAL": 0.05}},
            "municipalities": {muni: {"LEFT": 0.55}},
        }
    )
    m = StructuralModel.build(frame, doc, regions=no_regions)
    assert m.calibration.converged and m.calibration.max_abs_error < 1e-5
    assert m.province_shares(environment=False).loc["NB", "RIGHT"] == pytest.approx(0.40, abs=1e-4)
    assert m.municipality_shares(environment=False).loc[muni, "LEFT"] == pytest.approx(0.55, abs=1e-4)
    assert m.national_shares(environment=False)["LEFT"] == pytest.approx(0.28, abs=1e-4)


def test_fingerprint_tracks_everything_the_model_depends_on(frame, small_doc, no_regions, synth_regions):
    import dataclasses

    a = StructuralModel.build(frame, small_doc, regions=no_regions)
    b = StructuralModel.build(frame, small_doc, regions=no_regions)
    assert a.fingerprint == b.fingerprint
    assert StructuralModel.build(frame, small_doc, regions=synth_regions).fingerprint != a.fingerprint
    elig = frame.unit_eligible.copy()
    elig[0] += 1
    f2 = dataclasses.replace(frame, unit_eligible=elig)
    assert StructuralModel.build(f2, small_doc, regions=no_regions).fingerprint != a.fingerprint
    json.dumps(a.summary())


def test_turnout_probability_bounds_validated():
    with pytest.raises(Exception, match="min_probability"):
        model_config_from_dict({"turnout": {"min_probability": 0.9, "max_probability": 0.5}})


def test_region_membership_by_code_and_exclusion(frame):
    from app.simulation.regions import resolve_regions

    cfg = regions_config_from_dict(
        {"regions": {"r": {"label": "R", "provinces": ["GR"], "codes": ["GM1101"], "exclude": ["GM0101"]}}}
    )
    r = resolve_regions(frame, cfg)
    mask = r.muni_mask("r")
    assert mask[frame.muni_index("GM1101")] and not mask[frame.muni_index("GM0101")]
    assert mask[frame.munis_in_province(0)].sum() == len(frame.munis_in_province(0)) - 1


# --------------------------------------------------------------------------- candidates
@pytest.fixture()
def gr_units(frame):
    return frame.units_in_province(0)


def test_independent_incumbent_respects_max_candidates(small_model, gr_units, frame):
    inc = CandidateSpec(key="ind-inc", first_name="I", last_name="Nc", party=None, home_municipality="GM0101")
    for prob in (0.0, 1.0):
        rules = DownBallotSpec(
            incumbent_runs_again_prob=1.0,
            independents_prob=prob,
            max_candidates=3,
            default_rule=ContestRule(min_expected_share=0.0),
        )
        rc = generate_race_candidates(
            small_model, "GOV-GR", RaceType.GOVERNOR, gr_units, rules, 1, incumbent=inc
        )
        assert len(rc.lines) == 3
        assert sum(c.party is None for c in rc.candidates) == 1 and rc.incumbent_running


def test_max_two_with_independent_keeps_two_lines(small_model, gr_units):
    rules = DownBallotSpec(
        max_candidates=2, independents_prob=1.0, default_rule=ContestRule(min_expected_share=0.0)
    )
    rc = generate_race_candidates(small_model, "GOV-GR", RaceType.GOVERNOR, gr_units, rules, 1)
    assert len(rc.lines) == 2 and sum(c.party is None for c in rc.candidates) == 1


def test_incumbent_of_unknown_party_does_not_run(small_model, gr_units):
    inc = CandidateSpec(key="old", first_name="O", last_name="Ld", party="GONE", home_municipality="GM0101")
    rules = DownBallotSpec(incumbent_runs_again_prob=1.0, independents_prob=0.0)
    rc = generate_race_candidates(small_model, "GOV-GR", RaceType.GOVERNOR, gr_units, rules, 1, incumbent=inc)
    assert not rc.incumbent_running and "old" not in [c.key for c in rc.candidates]
    assert rc.incumbent_party == "GONE"


def test_open_seat_keeps_holder_party(small_model, gr_units, frame):
    inc = CandidateSpec(
        key="retiring", first_name="R", last_name="Et", party="RIGHT", home_municipality="GM0101"
    )
    rules = DownBallotSpec(incumbent_runs_again_prob=0.0, independents_prob=0.0)
    slot = RaceSlot(key="GOV-GR", race_type=RaceType.GOVERNOR, unit_index=gr_units, province_code="GR")
    rc = generate_race_candidates(small_model, "GOV-GR", RaceType.GOVERNOR, gr_units, rules, 1, incumbent=inc)
    (race,) = races_from_candidates([slot], {"GOV-GR": rc})
    assert race.is_open_seat and race.incumbent_party == "RIGHT"
    with_bonus = race_expectation(small_model, race).jurisdiction_shares
    plain = race_expectation(small_model, RaceSpec(**{**race.__dict__, "incumbent_party": None}))
    j = [ln.party_code for ln in race.lines].index("RIGHT")
    assert with_bonus[j] > plain.jurisdiction_shares[j]


def test_incumbent_info_counts_as_running_incumbent(small_model, frame):
    """An incumbent identified only through the context is not *also* treated as an open seat."""
    lines = [
        BallotLine(key="a", party_code="RIGHT", candidate_key="a"),
        BallotLine(key="b", party_code="RIGHT", candidate_key="b"),
        BallotLine(key="c", party_code="LEFT", candidate_key="c"),
    ]
    race = _race(frame, "HOUSE-Q", lines, race_type=RaceType.HOUSE)
    ctx = ElectionContext(
        year=2028, incumbents={"HOUSE-Q": IncumbentInfo(party="RIGHT", candidate_key="a", running=True)}
    )
    delta = race_expectation(small_model, race, ctx).delta
    inc = small_model.scenario.environment.incumbency.house
    assert np.allclose(delta[:, 0], inc) and np.allclose(delta[:, 1], 0.0)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("parties", 0, "demographics", "pct_education_high"), float("nan")),
        (("parties", 1, "turnout_propensity"), float("nan")),
        (("environment", "national", "LEFT"), float("inf")),
        (("environment", "shocks", "national_sd"), float("nan")),
        (("environment", "shocks", "unit_sd"), -0.1),
    ],
)
def test_non_finite_scenario_numbers_are_errors(frame, make_doc, no_regions, path, value):
    """YAML ``.nan`` / ``.inf`` pass the schema; they must not silently produce a NaN model."""
    data = _data(make_doc)
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    doc = ScenarioDocument.model_validate(data)
    with pytest.raises(ScenarioError, match=r"finite|>= 0"):
        StructuralModel.build(frame, doc, regions=no_regions)


def test_generated_keys_never_collide_with_scenario_candidates(small_model, frame):
    from app.simulation.candidates import generate_down_ballot
    from app.simulation.races import governor_slots

    taken: set[str] = set()  # caller-tracked keys that do not list the scenario's candidates
    out = generate_down_ballot(small_model, governor_slots(frame), seed=5, existing_keys=taken)
    scenario_keys = {c.key for c in small_model.scenario.candidates}
    generated = {k for rc in out.values() for k in rc.generated}
    assert scenario_keys <= taken and generated <= taken and not generated & scenario_keys


def test_unresolved_region_references_are_reported(frame, make_doc):
    cfg = regions_config_from_dict(
        {"regions": {"south": {"label": "S", "provinces": ["NB"], "municipalities": ["Atlantis"]}}}
    )
    data = _data(make_doc)
    data["parties"][0]["regions"] = {"south": 0.2}
    m = StructuralModel.build(frame, ScenarioDocument.model_validate(data), regions=cfg)
    assert any("Atlantis" in w for w in m.warnings)
