"""The one-command demo builder: synthetic builds (steps, idempotency, forecast hook, force) and a
full build on the REAL geography (``realdata``)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.constitution import ElectionStatus, RaceType
from app.models import AppMeta, Election, NightSession, Race, RaceCall, SimulationRun
from app.services import demo as demo_service
from app.services.demo import DEMO_ELECTION_KEY, build_demo
from app.services.night import NightManager


@contextmanager
def _scope(url: str) -> Iterator[Session]:
    engine = create_engine(url, future=True)
    s = sessionmaker(bind=engine, future=True, expire_on_commit=False)()
    try:
        yield s
        s.commit()
    finally:
        s.close()
        engine.dispose()


def _night_state(url: str, election_id: int) -> dict[str, Any]:
    @contextmanager
    def factory() -> Iterator[Session]:
        with _scope(url) as s:
            yield s

    return NightManager(factory).state(election_id, now=0.0)


class _FakeRunner:
    """Stands in for ``app.services.forecast_runner.run_forecast_for_election``."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int, int]] = []

    def __call__(
        self, session: Session, election_id: int, simulations: int, seed: int, workers: int = 0
    ) -> Any:
        self.calls.append((election_id, simulations, seed, workers))
        run = SimulationRun(
            kind="forecast",
            election_id=election_id,
            seed=seed,
            n_simulations=simulations,
            status="completed",
            duration_s=0.1,
            summary_json="{}",
        )
        session.add(run)
        session.flush()
        return run


def test_build_demo_synthetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    runner = _FakeRunner()
    monkeypatch.setattr(demo_service, "HISTORY_SCENARIOS", ("founding-2024",))
    monkeypatch.setattr(demo_service, "_forecast_runner", lambda: runner)
    events: list[tuple[str, str]] = []
    summary = build_demo(
        url=url, synthetic=True, forecast_simulations=500, progress=lambda n, st, d: events.append((n, st))
    )
    names = [s.name for s in summary.steps]
    assert names == [
        "database",
        "geography",
        "system",
        "election founding-2024",
        "election demo-2028",
        "forecast",
        "validate",
    ]
    assert {s.name: s.status for s in summary.steps} == {
        "database": "done",
        "geography": "skipped",
        "system": "done",
        "election founding-2024": "done",
        "election demo-2028": "done",
        "forecast": "done",
        "validate": "done",
    }
    assert ("database", "start") in events and ("validate", "done") in events
    assert summary.validation_ok and not summary.validation_errors
    assert set(summary.timings) == set(names) and summary.total_seconds > 0
    assert summary.database_bytes and summary.database_bytes > 0
    founding, live = summary.elections["founding-2024"], summary.elections["demo-2028"]
    assert summary.demo_election_id == live
    assert runner.calls == [(live, 500, 2028, 0)] and summary.forecast_run_id is not None
    night = summary.nights["founding-2024"]
    assert night["status"] == ElectionStatus.FINAL.value and night["calls"] > 0
    with _scope(url) as s:
        assert s.get(Election, founding).status == ElectionStatus.FINAL.value
        assert s.get(Election, live).status == ElectionStatus.SIMULATED.value
        assert s.get(AppMeta, DEMO_ELECTION_KEY).value == str(live)
        calls = s.scalar(select(func.count()).select_from(RaceCall).where(RaceCall.election_id == founding))
        assert calls == night["calls"]
        ns = s.scalar(select(NightSession).where(NightSession.election_id == founding))
        assert ns is not None and ns.status == "finished"
        assert s.scalar(select(NightSession).where(NightSession.election_id == live)) is None
        assert s.scalar(select(func.count()).select_from(RaceCall).where(RaceCall.election_id == live)) == 0
        pres = s.scalar(
            select(Race).where(Race.election_id == founding, Race.race_type == RaceType.PRESIDENT.value)
        )
        assert pres.status == "FINAL"
        called = s.scalar(
            select(func.count())
            .select_from(Race)
            .where(
                Race.election_id == founding,
                Race.race_type == RaceType.HOUSE.value,
                Race.called_at.is_not(None),
            )
        )
        assert called > 100  # House seats called during the (instant) night
    st = _night_state(url, live)
    p = st["snapshot"]["president"]
    assert st["clock"]["status"] == "ready" and st["clock"]["seq"] == 0
    assert (p["ev_decided_total"], p["ev_uncalled"], p["ev_total"], p["ev_needed"]) == (0, 174, 174, 88)

    # a second run completes nothing new
    again = build_demo(url=url, synthetic=True, forecast_simulations=500)
    status = {s.name: s.status for s in again.steps}
    assert status["election founding-2024"] == "skipped"
    assert status["election demo-2028"] == "skipped"
    assert status["forecast"] == "skipped" and len(runner.calls) == 1
    assert again.elections == summary.elections and again.validation_ok


def test_build_demo_force_and_missing_forecast(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'force.db'}"
    monkeypatch.setattr(demo_service, "HISTORY_SCENARIOS", ())
    monkeypatch.setattr(demo_service, "_forecast_runner", lambda: None)
    first = build_demo(url=url, synthetic=True, forecast_simulations=100)
    assert {s.name: s.status for s in first.steps}["forecast"] == "warning"
    assert first.warnings and first.validation_ok
    live = first.demo_election_id
    with _scope(url) as s:
        s.get(Election, live).name = "changed by hand"
    rebuilt = build_demo(url=url, synthetic=True, forecast_simulations=0, force=True)
    assert {s.name: s.status for s in rebuilt.steps}["forecast"] == "skipped"
    assert rebuilt.demo_election_id == live and rebuilt.validation_ok
    with _scope(url) as s:
        assert s.get(Election, live).name != "changed by hand"  # the database was rebuilt
        assert s.scalar(select(func.count()).select_from(Election)) == 1


@pytest.mark.realdata
def test_build_demo_real(real_frame, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{tmp_path / 'nlfed_demo.db'}"
    t0 = time.perf_counter()
    summary = build_demo(url=url, forecast_simulations=2000)
    seconds = time.perf_counter() - t0
    print(
        "\ndemo build timings:", {k: round(v, 1) for k, v in summary.timings.items()}, f"total {seconds:.1f}s"
    )
    print("database bytes:", summary.database_bytes, "nights:", summary.nights)
    assert summary.validation_ok, summary.validation_errors
    assert seconds < 600.0  # target < 300 s on an unloaded machine
    with _scope(url) as s:
        counts = dict(
            s.execute(select(Election.year, func.count(Race.id)).join(Race).group_by(Election.year))
            .tuples()
            .all()
        )
        assert counts[2024] == 211 and counts[2028] == 195 and counts[2026] > 700
        statuses = dict(s.execute(select(Election.year, Election.status)).tuples().all())
        assert statuses == {2024: "final", 2026: "final", 2028: "simulated"}
        assert s.scalar(select(func.count()).select_from(NightSession)) == 2
    st = _night_state(url, summary.demo_election_id)
    p = st["snapshot"]["president"]
    assert (p["ev_decided_total"], p["ev_uncalled"], p["ev_total"], p["ev_needed"]) == (0, 174, 174, 88)
    assert st["snapshot"]["reporting"]["municipalities_total"] == 342
