"""Fixtures for the election-night tests: synthetic (FICTIONAL) elections built offline.

The political model is written concurrently by another subsystem, so these fixtures build
``RaceVotes`` directly: expected shares vary by urbanity/province/unit, the final result is
the expectation plus a national swing, province, municipality and unit noise (log-share
space), and votes are multinomial draws.  All randomness flows from ``make_rng``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pytest

from app.core.config import get_constitution
from app.core.constitution import RaceType
from app.core.rng import make_rng
from app.elections.types import RaceVotes
from app.geography.frame import GeographyFrame
from app.reporting.config import NightConfig, default_night_config
from app.reporting.live import RaceMeta

TICKETS = ["T-ORANJE", "T-BLAUW", "T-GROEN", "T-ROOD"]
PARTIES = ["ORA", "BLA", "GRO", "ROO"]
COLORS = ["#f28c28", "#1f4e9c", "#2e9e44", "#c8102e"]


def softmax(z: np.ndarray) -> np.ndarray:
    z = z - z.max(axis=-1, keepdims=True)
    e = np.exp(z)
    return e / e.sum(axis=-1, keepdims=True)


def hamilton(pop: np.ndarray, seats: int, min_seats: int = 1) -> np.ndarray:
    """Largest-remainder apportionment (test helper; the real one lives in app.districts)."""
    base = np.full(len(pop), min_seats)
    rest = seats - base.sum()
    quota = pop / pop.sum() * rest
    extra = np.floor(quota).astype(int)
    rem = rest - extra.sum()
    extra[np.argsort(-(quota - extra), kind="stable")[:rem]] += 1
    return base + extra


def make_race_votes(
    key: str,
    units: np.ndarray,
    line_keys: list[str],
    log_expected: np.ndarray,
    actual_shift: np.ndarray,
    eligible: np.ndarray,
    expected_turnout: np.ndarray,
    ballots: np.ndarray,
    rng: np.random.Generator,
) -> RaceVotes:
    """Votes = multinomial(valid, softmax(log_expected + actual_shift))."""
    exp_shares = softmax(log_expected)
    shares = softmax(log_expected + actual_shift)
    blank = rng.binomial(ballots, 0.004)
    invalid = rng.binomial(ballots - blank, 0.003)
    valid = ballots - blank - invalid
    votes = rng.multinomial(valid, shares).astype(np.int64)
    rv = RaceVotes(
        race_key=key,
        line_keys=list(line_keys),
        unit_index=np.asarray(units, dtype=np.int64),
        votes=votes,
        ballots_cast=ballots.astype(np.int64),
        blank=blank.astype(np.int64),
        invalid=invalid.astype(np.int64),
        eligible=eligible.astype(np.int64),
        expected_shares=exp_shares,
        expected_turnout=expected_turnout,
    )
    rv.check()
    return rv


@dataclass
class SyntheticElection:
    frame: GeographyFrame
    ballots: np.ndarray  # (U,) ballots cast per unit
    races: dict[str, RaceVotes]
    meta: dict[str, RaceMeta]
    ev: dict[str, int]
    holdover: dict[str, int]
    swing: np.ndarray
    unit_wijk: list[str] = field(default_factory=list)


def build_election(
    frame: GeographyFrame,
    seed: int = 1,
    base_logits: tuple[float, ...] = (0.0, -0.08, -0.9, -1.1),
    swing_sd: float = 0.06,
    house: bool = True,
    senate: bool = True,
    governors: bool = True,
    ticket_swing: np.ndarray | None = None,
    pres_province_winner: dict[str, int] | None = None,
) -> SyntheticElection:
    """A complete FICTIONAL general election on ``frame`` (President, House, Senate, Governors)."""
    const = get_constitution()
    rng = make_rng(seed, "test-election")
    U, P = frame.n_units, frame.n_provinces
    L = len(TICKETS)
    urb = frame.unit_urbanity_class.astype(float)
    urb_z = (urb - urb.mean()) / max(urb.std(), 1e-9)
    party_urban = np.array([-0.25, 0.20, 0.30, 0.10])
    prov_lean = rng.normal(0, 0.25, (P, L))
    unit_lean = rng.normal(0, 0.25, (U, L))
    log_exp = (
        np.array(base_logits)[None, :]
        + urb_z[:, None] * party_urban
        + prov_lean[frame.unit_province]
        + unit_lean
    )
    # calibrate the intercepts so the expected national shares follow softmax(base_logits)
    target = softmax(np.array(base_logits, dtype=float))
    w = frame.unit_eligible.astype(float)
    for _ in range(30):
        log_exp += np.log(target / (w @ softmax(log_exp) / w.sum()))
    # realised shocks: national swing + province + municipality + unit
    swing = rng.normal(0, swing_sd, L) if ticket_swing is None else np.asarray(ticket_swing, dtype=float)
    prov_shock = rng.normal(0, 0.03, (P, L))[frame.unit_province]
    muni_shock = rng.normal(0, 0.03, (frame.n_munis, L))[frame.unit_muni]
    unit_shock = rng.normal(0, 0.05, (U, L))
    shift = swing + prov_shock + muni_shock + unit_shock
    eligible = frame.unit_eligible.astype(np.int64)
    exp_turnout = np.clip(0.78 + 0.03 * rng.standard_normal(U) - 0.02 * urb_z, 0.45, 0.95)
    act_turnout = np.clip(exp_turnout * np.exp(rng.normal(0, 0.02) + rng.normal(0, 0.03, U)), 0.05, 0.99)
    ballots = rng.binomial(eligible, act_turnout).astype(np.int64)

    races: dict[str, RaceVotes] = {}
    meta: dict[str, RaceMeta] = {}
    labels = {k: f"Ticket {k[2:].title()}" for k in TICKETS}
    parties = dict(zip(TICKETS, PARTIES, strict=True))
    colors = dict(zip(TICKETS, COLORS, strict=True))
    pop = frame.province_population().astype(float)
    seats = hamilton(pop, const.house_seats)
    ev: dict[str, int] = {}
    for p, pcode in enumerate(frame.province_codes):
        units = frame.units_in_province(p)
        key = f"PRES-{pcode}"
        boost = np.zeros(L)
        if pres_province_winner and pcode in pres_province_winner:
            boost[pres_province_winner[pcode]] = 2.0
        races[key] = make_race_votes(
            key,
            units,
            TICKETS,
            log_exp[units] + boost,
            shift[units],
            eligible[units],
            exp_turnout[units],
            ballots[units],
            make_rng(seed, "test-votes", key),
        )
        ev[key] = int(seats[p] + const.senators_per_province)
        meta[key] = RaceMeta(
            race_type=RaceType.PRESIDENT_PROVINCE,
            electoral_votes=ev[key],
            province_code=pcode,
            line_labels=labels,
            line_parties=parties,
            line_colors=colors,
            parent="PRES",
            name=f"President — {frame.province_names[p]}",
        )
    party_lines = [f"{pt}-cand" for pt in PARTIES]
    pmap = dict(zip(party_lines, PARTIES, strict=True))
    cmap = dict(zip(party_lines, COLORS, strict=True))
    if house:
        for p, pcode in enumerate(frame.province_codes):
            units = frame.units_in_province(p)
            for d, du in enumerate(np.array_split(units, seats[p]), start=1):
                key = f"HOUSE-{pcode}-{d:02d}"
                rr = make_rng(seed, "test-votes", key)
                cand = rr.normal(0, 0.08, L)
                inc = PARTIES[int(np.argmax(np.exp(log_exp[du]).sum(axis=0)))]
                races[key] = make_race_votes(
                    key,
                    du,
                    party_lines,
                    log_exp[du] + cand,
                    shift[du],
                    eligible[du],
                    exp_turnout[du],
                    ballots[du],
                    rr,
                )
                meta[key] = RaceMeta(
                    race_type=RaceType.HOUSE,
                    province_code=pcode,
                    district_code=f"{pcode}-{d:02d}",
                    line_parties=pmap,
                    line_colors=cmap,
                    incumbent_party=inc,
                )
    holdover: dict[str, int] = {}
    if senate:
        up = frame.province_codes[: const.seats_per_senate_class]
        for p, pcode in enumerate(frame.province_codes):
            if pcode not in up:
                continue
            units = frame.units_in_province(p)
            key = f"SEN-{pcode}-1"
            rr = make_rng(seed, "test-votes", key)
            races[key] = make_race_votes(
                key,
                units,
                party_lines,
                log_exp[units] + rr.normal(0, 0.05, L),
                shift[units],
                eligible[units],
                exp_turnout[units],
                ballots[units],
                rr,
            )
            meta[key] = RaceMeta(
                race_type=RaceType.SENATE, province_code=pcode, line_parties=pmap, line_colors=cmap
            )
        n_hold = const.senate_seats - len(up)
        holdover = dict(
            zip(PARTIES, hamilton(np.array([5.0, 4.0, 2.0, 1.0]), n_hold, 0).tolist(), strict=True)
        )
    if governors:
        for p, pcode in enumerate(frame.province_codes):
            units = frame.units_in_province(p)
            key = f"GOV-{pcode}"
            rr = make_rng(seed, "test-votes", key)
            races[key] = make_race_votes(
                key,
                units,
                party_lines,
                log_exp[units] + rr.normal(0, 0.1, L),
                shift[units],
                eligible[units],
                exp_turnout[units],
                ballots[units],
                rr,
            )
            meta[key] = RaceMeta(
                race_type=RaceType.GOVERNOR, province_code=pcode, line_parties=pmap, line_colors=cmap
            )
    return SyntheticElection(
        frame=frame, ballots=ballots, races=races, meta=meta, ev=ev, holdover=holdover, swing=swing
    )


@pytest.fixture(scope="session")
def night_config() -> NightConfig:
    return default_night_config()


@pytest.fixture(scope="session")
def election_builder() -> Callable[..., SyntheticElection]:
    return build_election


@pytest.fixture(scope="session")
def small_election(synthetic) -> SyntheticElection:  # type: ignore[no-untyped-def]
    return build_election(synthetic.frame, seed=1)


@pytest.fixture(scope="session")
def lite_election(synthetic) -> SyntheticElection:  # type: ignore[no-untyped-def]
    """President + governors only (24 races) — for tests that replay the night several times."""
    return build_election(synthetic.frame, seed=2, house=False, senate=False)


@pytest.fixture(scope="session")
def race_votes_factory() -> Callable[..., RaceVotes]:
    return make_race_votes


@pytest.fixture(scope="session")
def softmax_fn() -> Callable[[np.ndarray], np.ndarray]:
    return softmax
