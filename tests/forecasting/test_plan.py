"""Forecast plan: cells, anchoring on the model expectation, shock sensitivities, validation."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from app.core.constitution import RaceType
from app.core.errors import ElectionError
from app.elections.types import RaceSpec
from app.forecasting import ForecastConfig, ForecastResult, kernel, prepare_forecast
from app.forecasting.plan import SOURCE_FRAGMENT, SOURCE_MUNICIPALITY
from app.simulation.structural import StructuralModel
from app.simulation.voting import ElectionContext, context_shifts, expected_party_state, prepare_race

from .conftest import HOLDOVER


def _no_errors(cfg: ForecastConfig) -> ForecastConfig:
    e = cfg.errors.model_copy(
        update={
            "scale": 0.0,
            "turnout_national_sd": 0.0,
            "turnout_local_sd": 0.0,
            "party_turnout_sd": 0.0,
            "events": False,
        }
    )
    return cfg.model_copy(update={"errors": e})


def test_layers_and_cells(demo_model, context, full_races, test_config, ev_by_province) -> None:
    plan = prepare_forecast(
        demo_model,
        full_races,
        context,
        config=test_config,
        ev_by_province=ev_by_province,
        holdover_senate=HOLDOVER,
    )
    by_type = {lp.race_type: lp for lp in plan.layers}
    assert set(by_type) == {"PRESIDENT_PROVINCE", "HOUSE", "SENATE", "GOVERNOR"}
    assert by_type["PRESIDENT_PROVINCE"].source == SOURCE_MUNICIPALITY
    assert by_type["GOVERNOR"].source == SOURCE_MUNICIPALITY
    assert by_type["HOUSE"].source == SOURCE_FRAGMENT
    # every fragment lies in one municipality and one House district, and House cells cover all
    house = by_type["HOUSE"]
    assert sorted(house.cell_src.tolist()) == list(range(plan.n_fragments))
    assert np.all(np.diff(plan.frag_muni) >= 0)
    # presidential tickets share one national performance shock; House lines have their own
    pres = by_type["PRESIDENT_PROVINCE"]
    first = pres.cell_shock[pres.cell_race == 0][0]
    assert all((pres.cell_shock[pres.cell_race == r][0] == first).all() for r in range(pres.n_races))
    assert house.n_shocks == sum(len(r.lines) for r in full_races if r.race_type == RaceType.HOUSE)
    # the national race is derived from the provinces
    parent = [r for r in plan.races if r.derived]
    assert [r.key for r in parent] == ["PRES"]
    assert plan.president is not None and plan.president.total_ev == 174 and plan.president.majority == 88
    assert plan.senate is not None and plan.senate.seats_total == 24
    assert plan.metadata["cells"]["fragments"] == plan.n_fragments


def test_zero_errors_reproduce_the_model_expectation(
    run: Callable[..., ForecastResult],
    demo_model: StructuralModel,
    context: ElectionContext,
    full_races: list[RaceSpec],
    test_config: ForecastConfig,
) -> None:
    res = run(full_races, 300, 5, config=_no_errors(test_config))
    exp = expected_party_state(demo_model, context)
    E = demo_model.eligible
    for race in full_races:
        if race.race_type == RaceType.PRESIDENT:
            continue
        rp = prepare_race(demo_model, race, context, exp)
        u = rp.units
        valid = E[u] * exp.turnout[u] * (1.0 - rp.blank_probability(exp.vote_share[u]) - rp.invalid_p)
        want = (rp.expected_shares * valid[:, None]).sum(axis=0) / valid.sum()
        got = np.array([ln.share_mean for ln in res.races[race.key].lines])
        np.testing.assert_allclose(got, want, atol=2e-5, err_msg=race.key)
        np.testing.assert_allclose([ln.expected_share for ln in res.races[race.key].lines], want, atol=1e-9)
        # no uncertainty: the expected winner wins every draw
        assert res.races[race.key].favourite.win_probability == pytest.approx(1.0)
    assert res.turnout.p95 - res.turnout.p05 < 1e-6


def test_shock_response_matches_unit_level_model(demo_model, context, pres_races, test_config) -> None:
    """A national shock moves the fragment model as much as it moves the exact unit-level model."""
    plan = prepare_forecast(demo_model, pres_races, context, config=test_config)
    shift, ts = context_shifts(demo_model, context)
    vlp = demo_model.party_index["VLP"]
    delta = np.zeros(demo_model.n_parties)
    delta[vlp] = 0.1

    def unit_share(d: np.ndarray, t: float) -> float:
        st = demo_model.party_state(
            None, utility_shift=shift + demo_model.elasticity[:, None] * d[None, :], turnout_shift=ts + t
        )
        votes = st.vote_share * (demo_model.eligible * st.turnout)[:, None]
        return float(votes[:, vlp].sum() / votes.sum())

    def plan_share(d: np.ndarray, t: float) -> float:
        util = np.matmul(np.broadcast_to(d, (plan.n_fragments, 1, len(d))), plan.A_nat_T.astype(float))
        pi, T = kernel.fragment_state(
            plan.V.astype(float),
            plan.Tq.astype(float),
            util,
            np.full((plan.n_fragments, 1), t),
            None,
            plan.q_min,
            plan.q_max,
            plan.turnout_gain.astype(float),
        )
        votes = pi[:, 0, :] * (plan.frag_eligible * T[:, 0])[:, None]
        return float(votes[:, vlp].sum() / votes.sum())

    base_u, base_p = unit_share(np.zeros_like(delta), 0.0), plan_share(np.zeros_like(delta), 0.0)
    assert base_p == pytest.approx(base_u, abs=1e-6)
    du, dp = unit_share(delta, 0.0) - base_u, plan_share(delta, 0.0) - base_p
    assert dp == pytest.approx(du, rel=0.01)
    tu, tp = unit_share(np.zeros_like(delta), 0.2) - base_u, plan_share(np.zeros_like(delta), 0.2) - base_p
    assert tp == pytest.approx(tu, rel=0.05, abs=2e-5)


def test_poll_shift_is_a_national_shift(run, pres_races, context) -> None:
    from dataclasses import replace

    shift = {"CVU": 0.08, "PA": -0.03}
    a = run(pres_races, 500, 9, poll_shift=shift)
    b = run(pres_races, 500, 9, context=replace(context, national_shifts=dict(shift)))
    assert a.metadata["poll_shift"] == shift
    for key in a.races:
        assert [ln.share_mean for ln in a.races[key].lines] == [ln.share_mean for ln in b.races[key].lines]
    base = run(pres_races, 500, 9)
    cvu = a.president.ticket("marieke-pijnenburg")
    assert cvu.pv.mean > base.president.ticket("marieke-pijnenburg").pv.mean


def test_validation_errors(demo_model, context, pres_races, down_ballot, test_config, ev_by_province) -> None:
    def prep(races, **kw):
        kw.setdefault("ev_by_province", ev_by_province)
        return prepare_forecast(demo_model, races, context, config=test_config, **kw)

    with pytest.raises(ElectionError, match="at least one race"):
        prep([])
    with pytest.raises(ElectionError, match="duplicate"):
        prep(pres_races + pres_races[:1])
    with pytest.raises(ElectionError, match="unknown party"):
        prep(pres_races, poll_shift={"NOPE": 0.1})
    with pytest.raises(ElectionError, match="EV map"):
        prep(pres_races, ev_by_province={k: v for k, v in list(ev_by_province.items())[:-1]})
    with pytest.raises(ElectionError, match="exceed"):
        prep(down_ballot["SENATE"], holdover_senate={"VLP": 20})
    with pytest.raises(ElectionError, match="negative"):
        prep(down_ballot["SENATE"], holdover_senate={"VLP": -1})
    with pytest.raises(ElectionError, match="DISTRICT"):
        prep(pres_races, ev_method="district")
