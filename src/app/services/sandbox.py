"""A complete small world on the SYNTHETIC test country, for tests and offline experiments.

:func:`build_sandbox` sets up the fictional system on the synthetic geography
(:func:`app.services.bootstrap.setup_synthetic_system`), then — with the REAL demo scenarios
(``strict=False``: their references to real municipalities that the toy country lacks are
skipped) — creates, simulates and finalizes the founding election and creates and simulates (but
does not finalize) a second election, which is left SIMULATED for election-night tests::

    from app.services.sandbox import build_sandbox
    world = build_sandbox(session)             # ≈ 10 s, offline
    world.founding_election_id                 # FINAL
    world.second_election_id                   # SIMULATED (hidden result + timeline)

The caller owns the transaction (typically an in-memory or temporary SQLite database).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger, log_ctx
from app.services.bootstrap import SetupReport, setup_synthetic_system
from app.services.elections import create_election, finalize_election, simulate_election

log = get_logger(__name__)

FOUNDING_SCENARIO = "founding-2024"
SECOND_SCENARIO = "demo-2028"


@dataclass
class Sandbox:
    """Identifiers of the sandbox world."""

    seed: int
    setup: SetupReport
    founding_election_id: int
    second_election_id: int | None
    founding_status: str
    second_status: str | None
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["setup"] = self.setup.to_dict()
        return d


def build_sandbox(
    session: Session,
    seed: int = 1,
    *,
    finalize_founding: bool = True,
    second_scenario: str | None = SECOND_SCENARIO,
    simulate_second: bool = True,
    election_seed: int | None = None,
) -> Sandbox:
    """Build the sandbox world (see the module docstring).

    Args:
        seed: seed of the synthetic geography and of its House plan.
        finalize_founding: finalize the founding election (office holders exist for the second).
        second_scenario: scenario of the second election (``None`` = no second election).
        simulate_second: simulate the second election (it stays SIMULATED, never finalized).
        election_seed: election seed of both elections (default: each scenario's own seed).
    """
    timings: dict[str, float] = {}
    t = time.perf_counter()
    setup = setup_synthetic_system(session, seed)
    timings["setup"] = time.perf_counter() - t
    t = time.perf_counter()
    first = create_election(session, FOUNDING_SCENARIO, seed=election_seed, strict=False)
    simulate_election(session, first.id)
    if finalize_founding:
        finalize_election(session, first.id)
    timings["founding"] = time.perf_counter() - t
    second = None
    if second_scenario is not None:
        t = time.perf_counter()
        second = create_election(session, second_scenario, seed=election_seed, strict=False)
        if simulate_second:
            simulate_election(session, second.id)
        timings["second"] = time.perf_counter() - t
    session.flush()
    world = Sandbox(
        seed=int(seed),
        setup=setup,
        founding_election_id=first.id,
        second_election_id=second.id if second is not None else None,
        founding_status=first.status,
        second_status=second.status if second is not None else None,
        timings={k: round(v, 3) for k, v in timings.items()},
    )
    log.info("sandbox ready", extra=log_ctx(**{k: v for k, v in world.timings.items()}))
    return world
