"""Fixtures for the electoral-mechanics tests (RaceVotes are built here, independent of the model)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import numpy as np
import pytest

from app.core.rng import make_rng
from app.elections.tabulation import TabulatedRaceResult, tabulate_totals
from app.elections.types import RaceVotes
from app.geography.frame import GeographyFrame

#: A canonical-shaped Electoral-College map: House seats + 2 per province, 150 + 24 = 174 EV.
CANONICAL_EV: dict[str, int] = {
    "GR": 7,
    "FR": 8,
    "DR": 6,
    "OV": 12,
    "FL": 6,
    "GE": 20,
    "UT": 14,
    "NH": 27,
    "ZH": 34,
    "ZE": 5,
    "NB": 24,
    "LI": 11,
}


@pytest.fixture()
def ev_map() -> dict[str, int]:
    return dict(CANONICAL_EV)


def build_race_votes(
    frame: GeographyFrame,
    race_key: str,
    unit_index: np.ndarray,
    shares: Sequence[float],
    line_keys: Sequence[str],
    seed: int = 1,
    turnout: float = 0.75,
) -> RaceVotes:
    """Synthetic but internally consistent unit-level counts (multinomial around ``shares``)."""
    rng = make_rng(seed, "test-race-votes", race_key)
    idx = np.asarray(unit_index, dtype=np.int64)
    eligible = frame.unit_eligible[idx].astype(np.int64)
    cast = rng.binomial(eligible, turnout).astype(np.int64)
    blank = rng.binomial(cast, 0.004).astype(np.int64)
    invalid = rng.binomial(cast - blank, 0.003).astype(np.int64)
    valid = cast - blank - invalid
    p = np.asarray(shares, dtype=float)
    p = p / p.sum()
    local = rng.dirichlet(p * 200.0, size=len(idx))
    votes = np.stack([rng.multinomial(int(v), local[i]) for i, v in enumerate(valid)]).astype(np.int64)
    rv = RaceVotes(
        race_key=race_key,
        line_keys=list(line_keys),
        unit_index=idx,
        votes=votes,
        ballots_cast=cast,
        blank=blank,
        invalid=invalid,
        eligible=eligible,
    )
    rv.check()
    return rv


@pytest.fixture()
def race_votes_factory(synthetic) -> Callable[..., RaceVotes]:  # type: ignore[no-untyped-def]
    frame = synthetic.frame

    def make(
        race_key: str,
        shares: Sequence[float],
        line_keys: Sequence[str],
        unit_index: np.ndarray | None = None,
        seed: int = 1,
    ) -> RaceVotes:
        idx = np.arange(frame.n_units) if unit_index is None else unit_index
        return build_race_votes(frame, race_key, idx, shares, line_keys, seed)

    return make


@pytest.fixture()
def make_tab() -> Callable[..., TabulatedRaceResult]:
    """Factory: tabulated race from line totals ``make_tab(key, {line: votes}, tie_seed, resolve)``."""

    def tab(
        race_key: str, votes: Mapping[str, int], tie_seed: int | None = None, resolve: bool = True
    ) -> TabulatedRaceResult:
        return tabulate_totals(
            race_key, list(votes), list(votes.values()), tie_seed=tie_seed, resolve_ties=resolve
        )

    return tab
