"""The vectorised kernels reproduce the simulation model's formulas exactly (float64)."""

from __future__ import annotations

import numpy as np

from app.core.rng import make_rng
from app.forecasting import kernel
from app.forecasting.plan import share_jacobian
from app.simulation.structural import StructuralModel
from app.simulation.voting import ElectionContext, expected_party_state, prepare_race


def _logit(p: np.ndarray) -> np.ndarray:
    return np.log(p / (1.0 - p))


def test_line_shares_match_race_plan(
    demo_model: StructuralModel, context: ElectionContext, down_ballot
) -> None:
    exp = expected_party_state(demo_model, context)
    # a House race with strategic voting and a governor race
    races = [r for r in down_ballot["HOUSE"] if len(r.lines) >= 5][:1] + down_ballot["GOVERNOR"][:1]
    rng = make_rng(3, "test-kernel")
    for race in races:
        rp = prepare_race(demo_model, race, context, exp)
        n, L = len(rp.units), len(race.lines)
        P = demo_model.n_parties
        noise = rng.normal(0.0, 0.2, (n, P))
        pi = exp.vote_share[rp.units] * np.exp(noise)
        pi /= pi.sum(axis=1, keepdims=True)
        shock = rng.normal(0.0, 0.05, L)
        want = rp.shares(pi, shock)
        # every unit is its own cell; one draw
        got, valid = kernel.line_shares(
            pi[:, None, :],
            np.broadcast_to(rp.R[None], (n, P, L)),
            np.broadcast_to(rp.a[None], (n, P)),
            np.broadcast_to(rp.indep_mass[None], (n, L)),
            rp.delta,
            np.broadcast_to(rp.G[None], (n, L, L)),
            None,
            rp.blank_base,
            rp.invalid_p,
            np.broadcast_to(shock[None, None, :], (n, 1, L)),
        )
        np.testing.assert_allclose(got[:, 0, :], want, rtol=1e-10, atol=1e-13)
        np.testing.assert_allclose(valid[:, 0], 1.0 - rp.invalid_p - rp.blank_probability(pi), rtol=1e-12)


def test_fragment_state_matches_party_state(demo_model: StructuralModel, context: ElectionContext) -> None:
    exp = expected_party_state(demo_model, context)
    s, T = exp.preference, exp.turnout
    q = exp.vote_share * T[:, None] / s
    V, Tq = np.log(s), _logit(np.clip(q, 1e-9, 1 - 1e-9))
    rng = make_rng(4, "test-kernel")
    U, P = s.shape
    util = rng.normal(0.0, 0.1, (U, P))
    tshift = rng.normal(0.0, 0.05, U)
    party_t = rng.normal(0.0, 0.05, P)
    tc = demo_model.config.turnout
    pi, Tk = kernel.fragment_state(
        V, Tq, util[:, None, :], tshift[:, None], party_t[None, :], tc.min_probability, tc.max_probability
    )
    # the same shocks through the structural model (its expectation plus the shifts)
    from app.simulation.voting import context_shifts

    shift, ts = context_shifts(demo_model, context)
    want = demo_model.party_state(
        None, utility_shift=shift + util, turnout_shift=ts + tshift, party_turnout_shift=party_t
    )
    # q from the expectation is already clipped; units at a bound stay at the bound
    free = ((q > tc.min_probability + 1e-9) & (q < tc.max_probability - 1e-9)).all(axis=1)
    np.testing.assert_allclose(pi[free, 0, :], want.vote_share[free], rtol=1e-9, atol=1e-12)
    np.testing.assert_allclose(Tk[free, 0], want.turnout[free], rtol=1e-9)


def test_share_jacobian_matches_finite_differences(
    demo_model: StructuralModel, context: ElectionContext
) -> None:
    exp = expected_party_state(demo_model, context)
    f = demo_model.frame
    s, T = exp.preference, exp.turnout
    q = exp.vote_share * T[:, None] / s
    E = demo_model.eligible
    group = f.unit_muni
    G, P = f.n_munis, demo_model.n_parties
    J = share_jacobian(group, G, E, demo_model.elasticity, s, q, T)

    def agg(delta: np.ndarray) -> np.ndarray:
        V = np.log(s) + demo_model.elasticity[:, None] * delta[None, :]
        ss = np.exp(V - V.max(axis=1, keepdims=True))
        ss /= ss.sum(axis=1, keepdims=True)
        votes = np.column_stack(
            [np.bincount(group, weights=E * ss[:, p] * q[:, p], minlength=G) for p in range(P)]
        )
        return votes / votes.sum(axis=1, keepdims=True)

    h = 1e-6
    for k in (0, 3):
        d = np.zeros(P)
        d[k] = h
        num = (agg(d) - agg(-d)) / (2 * h)
        np.testing.assert_allclose(J[:, :, k], num, rtol=1e-4, atol=1e-8)
    # shares always sum to one and a common shift changes nothing
    np.testing.assert_allclose(J.sum(axis=1), 0.0, atol=1e-12)
    np.testing.assert_allclose(J.sum(axis=2), 0.0, atol=1e-12)
