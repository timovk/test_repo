"""One-command, reproducible demo world (``python -m app demo``).

:func:`build_demo` brings a database to the state the UI is designed around:

1. **database** — Alembic migrations (``--force``: the schema is dropped and recreated first);
2. **geography** — the REAL processed CBS store (downloaded and built only when it is missing, so a
   prepared machine works offline);
3. **system** — geography in the database, apportionment (150 seats, 174 EV), the House plan
   (generated from ``seed``; reused when it already exists), Senate seats, offices, legislatures;
4. **history** — ``founding-2024`` and ``midterm-2026``: create → simulate → election night run
   at once (:func:`app.services.night.run_instant_night`, every race call stored) → FINAL;
5. **demo-2028** — create → simulate, left SIMULATED so its election night starts at polls closing
   (0 EV allocated, 174 available, 88 TO WIN);
6. **forecast** — a Monte Carlo forecast of 2028 (``app.services.forecast_runner``, imported
   lazily; skipped with a warning when that service is not installed);
7. ``app_meta.demo_election_id`` and a full :func:`~app.services.validation.validate_system`, which
   must pass.

Every step is idempotent: completed steps are skipped on a re-run (``force=True`` rebuilds the
database from scratch; the REAL geography store is never deleted).  Each step commits on its own,
so an interrupted build resumes where it stopped.  The elections use their scenario's seed unless
``election_seed`` is given; the House plan and the forecast use ``seed``.  ``synthetic=True``
builds the same world on the synthetic toy country (offline, seconds — tests and experiments).
Everything electoral in the result is FICTIONAL or SIMULATED; only the geography is REAL.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.constitution import ElectionStatus
from app.core.errors import ValidationError
from app.core.logging import get_logger, log_ctx
from app.core.rng import derive_seed
from app.core.settings import get_settings
from app.models import Election, Scenario, SimulationRun
from app.scenarios.loader import load_scenario
from app.services._common import REPORTED_STATUSES, set_meta

log = get_logger(__name__)

__all__ = [
    "DEMO_ELECTION_KEY",
    "DEMO_SCENARIOS",
    "DemoStep",
    "DemoSummary",
    "build_demo",
    "find_scenario_election",
]

#: ``app_meta`` key of the election the UI opens by default.
DEMO_ELECTION_KEY = "demo_election_id"
#: History elections (finalized through an instant election night) and the live demo election.
HISTORY_SCENARIOS: tuple[str, ...] = ("founding-2024", "midterm-2026")
LIVE_SCENARIO = "demo-2028"
DEMO_SCENARIOS: tuple[str, ...] = (*HISTORY_SCENARIOS, LIVE_SCENARIO)
#: Seed of the synthetic toy country used by ``synthetic=True``.
SYNTHETIC_GEOGRAPHY_SEED = 7
#: Run kind of stored forecasts (:mod:`app.forecasting.service`).
FORECAST_RUN_KIND = "forecast"

#: ``progress(step, status, detail)`` with status ``start`` | ``done`` | ``skipped`` | ``warning``.
ProgressCallback = Callable[[str, str, str], None]


@dataclass
class DemoStep:
    """Outcome of one step of the demo build."""

    name: str
    status: str  # done | skipped | warning
    seconds: float
    detail: str = ""


@dataclass
class DemoSummary:
    """What :func:`build_demo` did (ids, per-step timings, validation)."""

    seed: int
    database_url: str
    synthetic: bool
    elections: dict[str, int] = field(default_factory=dict)
    demo_election_id: int | None = None
    forecast_run_id: int | None = None
    validation_ok: bool = False
    validation_errors: list[str] = field(default_factory=list)
    validation_warnings: list[str] = field(default_factory=list)
    steps: list[DemoStep] = field(default_factory=list)
    total_seconds: float = 0.0
    database_bytes: int | None = None
    nights: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def timings(self) -> dict[str, float]:
        """Seconds per step."""
        return {s.name: s.seconds for s in self.steps}

    @property
    def warnings(self) -> list[str]:
        return [f"{s.name}: {s.detail}" for s in self.steps if s.status == "warning"]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timings"] = self.timings
        d["warnings"] = self.warnings
        return d


# =========================================================================== helpers
def _redact(url: str) -> str:
    """A database URL without its password."""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def _sqlite_path(url: str) -> Path | None:
    if url.startswith("sqlite:///") and ":memory:" not in url:
        return Path(url.removeprefix("sqlite:///"))
    return None


def find_scenario_election(session: Session, slug: str, year: int) -> Election | None:
    """The most recent election of ``year`` created from scenario ``slug`` (or a stored variant
    ``<slug>--<hash>`` of it)."""
    return session.scalars(
        select(Election)
        .join(Scenario, Scenario.id == Election.scenario_id)
        .where(
            Election.year == int(year),
            or_(Scenario.slug == slug, Scenario.slug.like(f"{slug}--%")),
        )
        .order_by(Election.id.desc())
        .limit(1)
    ).first()


class _Builder:
    """Step runner: timing, progress callbacks, per-step transactions."""

    def __init__(self, summary: DemoSummary, url: str, progress: ProgressCallback | None) -> None:
        self.summary = summary
        self.url = url
        self.progress = progress

    def notify(self, name: str, status: str, detail: str = "") -> None:
        if self.progress is not None:
            self.progress(name, status, detail)

    @contextmanager
    def session(self) -> Iterator[Session]:
        from app.db.session import session_scope

        with session_scope(self.url) as s:
            yield s

    def run(self, name: str, fn: Callable[[], tuple[str, str]]) -> DemoStep:
        """Run one step; ``fn`` returns ``(status, detail)``."""
        self.notify(name, "start", "")
        t0 = time.perf_counter()
        status, detail = fn()
        step = DemoStep(name, status, round(time.perf_counter() - t0, 3), detail)
        self.summary.steps.append(step)
        log.info("demo step %s: %s", name, status, extra=log_ctx(seconds=step.seconds, detail=detail))
        self.notify(name, status, detail)
        return step


def _reset_schema(url: str) -> None:
    """Drop every table (Alembic downgrade to base) so the demo starts from an empty database."""
    from app.db.migrate import current_revision, downgrade_db

    if current_revision(url) is not None:
        downgrade_db("base", url)


def _election_seed(election_seed: int | None, slug: str) -> int | None:
    if election_seed is None:
        return None
    return int(derive_seed(int(election_seed), "demo", slug) % (2**62))


def _forecast_runner() -> Callable[..., Any] | None:
    try:
        from app.services.forecast_runner import run_forecast_for_election
    except ImportError as exc:
        log.warning("forecast service unavailable (%s); the demo forecast is skipped", exc)
        return None
    return run_forecast_for_election


# =========================================================================== build
def build_demo(
    *,
    seed: int = 2028,
    force: bool = False,
    forecast_simulations: int = 10000,
    progress: ProgressCallback | None = None,
    url: str | None = None,
    synthetic: bool = False,
    election_seed: int | None = None,
    workers: int = 0,
) -> DemoSummary:
    """Build (or complete) the demo world; returns a :class:`DemoSummary` with per-step timings.

    Args:
        seed: seed of the House district plan and of the 2028 forecast.
        force: rebuild the database from scratch (the REAL geography store is kept).
        forecast_simulations: Monte Carlo draws of the 2028 forecast (0 = no forecast).
        progress: ``progress(step, status, detail)`` callback (CLI spinners).
        url: database URL (default: the configured database).
        synthetic: use the synthetic toy country instead of the REAL geography (offline tests).
        election_seed: derive every election's seed from this value (default: scenario seeds).
        workers: worker processes of districting and forecasting (0 = automatic).

    Raises:
        ValidationError: the finished system fails :func:`validate_system`.
    """
    from app.services.bootstrap import init_db, prepare_geography, setup_synthetic_system, setup_system
    from app.services.elections import create_election, simulate_election
    from app.services.night import get_night_manager, run_instant_night
    from app.services.runtime import clear_caches
    from app.services.validation import validate_system

    t_start = time.perf_counter()
    db_url = url or get_settings().db_url
    summary = DemoSummary(seed=int(seed), database_url=_redact(db_url), synthetic=bool(synthetic))
    b = _Builder(summary, db_url, progress)
    strict = not synthetic

    # ---- 1. database
    def database() -> tuple[str, str]:
        if force:
            _reset_schema(db_url)
            clear_caches()
        init_db(db_url)
        return "done", "schema rebuilt" if force else "schema at the latest revision"

    b.run("database", database)

    # ---- 2. geography store
    def geography() -> tuple[str, str]:
        if synthetic:
            return "skipped", "synthetic toy country (no REAL data needed)"
        from app.geography.store import is_prepared, manifest

        if is_prepared():
            m = manifest()
            return (
                "skipped",
                f"REAL CBS store {m.get('year')} present ({m.get('counts', {}).get('units', '?')} units)",
            )
        prepare_geography(download=True)
        return "done", "downloaded and built the REAL CBS store"

    b.run("geography", geography)

    # ---- 3. fictional system on the geography
    def system() -> tuple[str, str]:
        with b.session() as s:
            if synthetic:
                rep = setup_synthetic_system(s, SYNTHETIC_GEOGRAPHY_SEED, district_seed=int(seed))
            else:
                rep = setup_system(s, district_seed=int(seed), workers=workers)
        return "done", (
            f"{rep.provinces} provinces, {rep.municipalities} municipalities, {rep.units} units, "
            f"{rep.districts} districts (plan {rep.plan_id}{', reused' if rep.plan_reused else ''}), "
            f"{rep.senate_seats} Senate seats"
        )

    b.run("system", system)

    # ---- 4. history elections through an instant election night
    def history(slug: str) -> tuple[str, str]:
        doc = load_scenario(slug)
        parts: list[str] = []
        with b.session() as s:
            el = find_scenario_election(s, slug, doc.scenario.year)
            if el is not None and el.status in REPORTED_STATUSES:
                summary.elections[slug] = el.id
                return "skipped", f"election {el.id} already {el.status}"
            if el is None:
                t = time.perf_counter()
                el = create_election(s, slug, seed=_election_seed(election_seed, slug), strict=strict)
                parts.append(f"created {time.perf_counter() - t:.1f}s")
            eid = el.id
        summary.elections[slug] = eid
        with b.session() as s:
            el = s.get(Election, eid)
            assert el is not None
            if el.status == ElectionStatus.SCHEDULED.value:
                t = time.perf_counter()
                simulate_election(s, eid)
                parts.append(f"simulated {time.perf_counter() - t:.1f}s")
        with b.session() as s:
            t = time.perf_counter()
            night = run_instant_night(s, eid)
            summary.nights[slug] = night
            parts.append(
                f"night of {night['events']} events, {night['calls']} calls, finalized "
                f"{time.perf_counter() - t:.1f}s"
            )
        return "done", f"election {eid}: " + ", ".join(parts)

    for slug in HISTORY_SCENARIOS:
        b.run(f"election {slug}", lambda slug=slug: history(slug))

    # ---- 5. the live demo election, simulated and ready at polls closing
    def live() -> tuple[str, str]:
        doc = load_scenario(LIVE_SCENARIO)
        parts: list[str] = []
        with b.session() as s:
            el = find_scenario_election(s, LIVE_SCENARIO, doc.scenario.year)
            if el is None:
                t = time.perf_counter()
                el = create_election(
                    s, LIVE_SCENARIO, seed=_election_seed(election_seed, LIVE_SCENARIO), strict=strict
                )
                parts.append(f"created {time.perf_counter() - t:.1f}s")
            eid, status = el.id, el.status
        summary.elections[LIVE_SCENARIO] = eid
        summary.demo_election_id = eid
        if status == ElectionStatus.SCHEDULED.value:
            with b.session() as s:
                t = time.perf_counter()
                simulate_election(s, eid)
                parts.append(f"simulated {time.perf_counter() - t:.1f}s")
            status = ElectionStatus.SIMULATED.value
        if status == ElectionStatus.SIMULATED.value:
            if not parts:
                return "skipped", f"election {eid} already simulated; its night is ready at polls closing"
            return "done", f"election {eid}: " + ", ".join(parts) + "; night ready at polls closing"
        return "warning", (
            f"election {eid} is {status}: its night is not at polls closing "
            "(reset it with `election-night --reset` or rebuild with `demo --force`)"
        )

    b.run(f"election {LIVE_SCENARIO}", live)

    # ---- 6. forecast of the demo election
    def forecast() -> tuple[str, str]:
        eid = summary.demo_election_id
        if eid is None or forecast_simulations <= 0:
            return "skipped", "no forecast requested"
        with b.session() as s:
            existing = s.scalar(
                select(SimulationRun.id)
                .where(
                    SimulationRun.kind == FORECAST_RUN_KIND,
                    SimulationRun.election_id == eid,
                    SimulationRun.status == "completed",
                    SimulationRun.n_simulations == int(forecast_simulations),
                    SimulationRun.seed == int(seed),
                )
                .order_by(SimulationRun.id.desc())
                .limit(1)
            )
        if existing is not None and not force:
            summary.forecast_run_id = int(existing)
            return "skipped", f"forecast run {existing} ({forecast_simulations} draws, seed {seed}) exists"
        runner = _forecast_runner()
        if runner is None:
            return "warning", "forecast service (app.services.forecast_runner) not installed"
        with b.session() as s:
            run = runner(s, eid, int(forecast_simulations), int(seed), workers=int(workers))
            summary.forecast_run_id = int(run.id)
            detail = f"run {run.id}: {forecast_simulations} draws in {run.duration_s or 0:.1f}s"
        return "done", detail

    b.run("forecast", forecast)

    # ---- 7. demo election id + validation
    def finish() -> tuple[str, str]:
        with b.session() as s:
            if summary.demo_election_id is not None:
                set_meta(s, DEMO_ELECTION_KEY, str(summary.demo_election_id))
            report = validate_system(s)
        summary.validation_ok = report.ok
        summary.validation_errors = list(report.errors)
        summary.validation_warnings = list(report.warnings)
        if not report.ok:
            raise ValidationError("the demo system failed validation", report.errors)
        return "done", f"{len(report.checks)} checks passed ({len(report.warnings)} warnings)"

    b.run("validate", finish)
    get_night_manager().forget()
    path = _sqlite_path(db_url)
    if path is not None and path.exists():
        summary.database_bytes = int(path.stat().st_size)
    summary.total_seconds = round(time.perf_counter() - t_start, 3)
    log.info(
        "demo ready",
        extra=log_ctx(
            seconds=summary.total_seconds,
            demo_election_id=summary.demo_election_id,
            elections=summary.elections,
        ),
    )
    return summary
