"""Monte Carlo forecast runs for stored elections, plus a small background job registry.

:func:`run_forecast_for_election` rebuilds the election's engine inputs
(:func:`app.services.runtime.election_inputs`), optionally conditions the model on the polls —
the weighted average of the election's stored (FICTIONAL) polls as of the day before the
election (:func:`app.polling.aggregate.aggregate_polls`), turned into a national logit shift per
party (:func:`app.polling.blend.shift_from_poll_average`) — runs
:func:`app.forecasting.engine.run_forecast` and persists the result
(:func:`app.forecasting.service.store_forecast`, a ``simulation_run`` of kind ``forecast`` with
its seed).  Same election, seed, draws and polls ⇒ identical stored forecast.

:func:`start_forecast_job` / :func:`job_status` run it in a background thread (one job at a time;
the engine itself uses worker processes) so the API can start a 100k-draw run and poll it.

Every output is a SIMULATED model estimate of a FICTIONAL system — never a prediction.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

import app
from app.core.constitution import RaceType
from app.core.errors import ElectionError, NLFedError, ValidationError
from app.core.logging import Timer, get_logger, log_ctx
from app.core.rng import derive_seed
from app.forecasting.config import ForecastConfig, load_forecast_config
from app.forecasting.engine import run_forecast
from app.forecasting.service import store_forecast
from app.models import SimulationRun, utcnow
from app.polling.aggregate import aggregate_polls
from app.polling.blend import shift_from_poll_average
from app.polling.service import load_pollster_configs, polls_frames
from app.polling.types import NATIONAL_GEO
from app.services.runtime import ElectionInputs, election_inputs, race_expectations

log = get_logger(__name__)

__all__ = [
    "DEFAULT_RACE_TYPES",
    "ForecastJob",
    "ForecastJobs",
    "job_status",
    "list_jobs",
    "poll_shift_for_election",
    "run_forecast_for_election",
    "start_forecast_job",
]

#: Race types forecast by default (local races — mayors, councils, provincial legislatures — are
#: many and small; pass ``include_local=True`` to add them).
DEFAULT_RACE_TYPES: tuple[RaceType, ...] = (
    RaceType.PRESIDENT,
    RaceType.PRESIDENT_PROVINCE,
    RaceType.HOUSE,
    RaceType.SENATE,
    RaceType.GOVERNOR,
)
LOCAL_RACE_TYPES: tuple[RaceType, ...] = (
    RaceType.PROVINCIAL_LEGISLATURE,
    RaceType.MAYOR,
    RaceType.MUNICIPAL_COUNCIL,
)
_SEED_LIMIT = 2**62


# =========================================================================== polls → shift
def _national_expected_shares(inputs: ElectionInputs, codes: list[str]) -> dict[str, float]:
    """Model-expected national vote share per party over the races ``codes`` (expected votes =
    eligible × expected turnout × expected share, summed over the races' units)."""
    exp = race_expectations(inputs, codes)
    elig = np.asarray(inputs.frame.unit_eligible, dtype=np.float64)
    votes: dict[str, float] = {}
    for code in codes:
        if code not in exp:
            continue
        spec = inputs.races[code]
        shares, turnout = exp[code]
        w = elig[np.asarray(spec.unit_index, dtype=np.int64)] * np.asarray(turnout, dtype=np.float64)
        per_line = w @ np.asarray(shares, dtype=np.float64)
        for ln, v in zip(spec.lines, per_line.tolist(), strict=True):
            if ln.party_code is not None and not ln.withdrawn:
                votes[ln.party_code] = votes.get(ln.party_code, 0.0) + float(v)
    total = sum(votes.values())
    return {k: v / total for k, v in votes.items()} if total > 0 else {}


def poll_shift_for_election(
    session: Session, inputs: ElectionInputs, *, config: ForecastConfig | None = None
) -> tuple[dict[str, float] | None, dict[str, Any]]:
    """National logit shift per party from the election's polling average (the national
    presidential ballot, else the generic House ballot) as of the day before the election, and a
    description of what was used (``{"used": False, ...}`` when there are no usable polls)."""
    poll_type = "national_president" if "PRES" in inputs.races else "generic_house"
    as_of = inputs.election_date - timedelta(days=1)
    info: dict[str, Any] = {
        "used": False,
        "poll_type": poll_type,
        "geo_code": NATIONAL_GEO,
        "as_of": as_of.isoformat(),
    }
    pdf, rdf = polls_frames(session, inputs.election_id, poll_type)
    if pdf.empty:
        info["reason"] = "no polls"
        return None, info
    avgs = aggregate_polls(pdf, rdf, None, as_of, pollsters=load_pollster_configs(session))
    avg = avgs.get((poll_type, NATIONAL_GEO))
    if avg is None:
        info["reason"] = "too few polls for an average"
        return None, info
    if poll_type == "national_president":
        codes = ["PRES"]
    else:
        codes = [c for c, r in inputs.races.items() if RaceType(r.race_type) == RaceType.HOUSE]
    expected = _national_expected_shares(inputs, codes)
    if not expected:
        info["reason"] = "no model expectation for the polled races"
        return None, info
    prior_sd = float(inputs.scenario.environment.shocks.national_sd)
    shift = shift_from_poll_average(avg, expected, prior_sd)
    info.update(
        {
            "used": bool(shift),
            "n_polls": int(avg.n_polls),
            "effective_n": round(float(avg.effective_n), 1),
            "poll_mean_pct": {k: round(float(v), 3) for k, v in avg.mean.items()},
            "model_expected_pct": {k: round(100.0 * v, 3) for k, v in expected.items()},
            "model_prior_sd": prior_sd,
            "shift": {k: round(float(v), 6) for k, v in shift.items()},
        }
    )
    return (shift or None), info


# =========================================================================== run
def run_forecast_for_election(
    session: Session,
    election_id: int,
    simulations: int,
    seed: int,
    workers: int = 0,
    use_polls: bool = True,
    *,
    include_local: bool = False,
    progress: Callable[[int, int], None] | None = None,
    config: ForecastConfig | None = None,
) -> SimulationRun:
    """Run and store a Monte Carlo forecast of a stored election (see the module docstring).

    Args:
        simulations: number of simulated elections (1 … ``config.max_simulations``).
        seed: root seed (0 ≤ seed < 2**62), stored on the ``simulation_run``.
        workers: 0 = automatic (worker processes for large runs), 1 = in-process, ≥ 2 = processes.
        use_polls: condition the national environment on the polling average.
        include_local: also forecast provincial legislatures, mayors and councils.
        progress: ``progress(done, total)`` after every finished chunk.

    Returns the stored ``SimulationRun`` (kind ``forecast``); the caller owns the transaction.
    """
    cfg = config or load_forecast_config()
    n = int(simulations)
    if n < 1 or n > int(cfg.max_simulations):
        raise ValidationError(f"simulations must be between 1 and {cfg.max_simulations}")
    seed = int(seed)
    if seed < 0 or seed >= _SEED_LIMIT:
        raise ValidationError("seed must satisfy 0 ≤ seed < 2**62")
    inputs = election_inputs(session, int(election_id))
    types = set(DEFAULT_RACE_TYPES) | (set(LOCAL_RACE_TYPES) if include_local else set())
    codes = [c for c, r in inputs.races.items() if RaceType(r.race_type) in types]
    if not codes:
        raise ElectionError(f"election {election_id} has no races to forecast")
    shift, poll_info = (
        poll_shift_for_election(session, inputs, config=cfg) if use_polls else (None, {"used": False})
    )
    with Timer(log, f"forecast election {election_id} ({n} draws)"):
        result = run_forecast(
            inputs.model,
            [inputs.races[c] for c in codes],
            inputs.context,
            n,
            seed,
            ev_by_province=inputs.ev_by_province,
            constitution=inputs.constitution,
            holdover_senate=inputs.holdover_senate,
            poll_shift=shift,
            workers=int(workers),
            config=cfg,
            progress=progress,
            keep_draws=False,
        )
    result.metadata["election_id"] = int(election_id)
    result.metadata["polls"] = poll_info
    result.metadata["race_types"] = sorted(t.value for t in types)
    from app.models import Election

    el = session.get(Election, int(election_id))
    run = store_forecast(
        session,
        int(election_id),
        result,
        race_ids={c: inputs.race_ids[c] for c in codes},
        line_ids={c: inputs.line_ids[c] for c in codes},
        scenario_id=el.scenario_id if el is not None else None,
        code_version=app.__version__,
    )
    log.info(
        "forecast stored",
        extra=log_ctx(election_id=int(election_id), run_id=run.id, draws=n, seed=seed, polls=bool(shift)),
    )
    return run


def default_forecast_seed(election_seed: int, n_previous_runs: int) -> int:
    """Seed of the next forecast run of an election when none is given (derived, stored)."""
    return int(derive_seed(int(election_seed), "forecast", int(n_previous_runs)) % _SEED_LIMIT)


# =========================================================================== jobs
@dataclass
class ForecastJob:
    """A background forecast run."""

    job_id: str
    election_id: int
    simulations: int
    seed: int
    workers: int
    use_polls: bool
    include_local: bool
    status: str = "queued"  # queued | running | completed | failed
    done: int = 0
    total: int = 0
    run_id: int | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["progress"] = (
            round(self.done / self.total, 4) if self.total else (1.0 if self.status == "completed" else 0.0)
        )
        d["data_category"] = "SIMULATED"
        return d


class ForecastJobs:
    """Thread-safe registry of background forecast jobs (FIFO, ``max_workers`` at a time)."""

    def __init__(self, max_workers: int = 1, keep: int = 100) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="forecast")
        self._jobs: OrderedDict[str, ForecastJob] = OrderedDict()
        self._lock = threading.Lock()
        self._keep = keep

    def start(
        self,
        election_id: int,
        simulations: int,
        seed: int,
        *,
        workers: int = 0,
        use_polls: bool = True,
        include_local: bool = False,
        db_url: str | None = None,
    ) -> str:
        """Queue a forecast run on the database at ``db_url`` (default: configured) → job id."""
        job = ForecastJob(
            job_id=uuid.uuid4().hex[:16],
            election_id=int(election_id),
            simulations=int(simulations),
            seed=int(seed),
            workers=int(workers),
            use_polls=bool(use_polls),
            include_local=bool(include_local),
            total=int(simulations),
        )
        with self._lock:
            self._jobs[job.job_id] = job
            while len(self._jobs) > self._keep:
                oldest = next(iter(self._jobs))
                if self._jobs[oldest].status in ("queued", "running"):
                    break
                self._jobs.pop(oldest)
        self._executor.submit(self._run, job, db_url)
        log.info(
            "forecast job queued",
            extra=log_ctx(job_id=job.job_id, election_id=job.election_id, draws=job.simulations),
        )
        return job.job_id

    def _run(self, job: ForecastJob, db_url: str | None) -> None:
        from app.db.session import session_scope

        job.status = "running"
        job.started_at = utcnow().isoformat()
        t0 = time.perf_counter()

        def progress(done: int, total: int) -> None:
            job.done, job.total = int(done), int(total)

        try:
            with session_scope(db_url) as session:
                run = run_forecast_for_election(
                    session,
                    job.election_id,
                    job.simulations,
                    job.seed,
                    job.workers,
                    job.use_polls,
                    include_local=job.include_local,
                    progress=progress,
                )
                job.run_id = int(run.id)
            job.done = job.total
            job.status = "completed"
            from app.services.read._base import clear_read_cache

            clear_read_cache()
        except (NLFedError, ValueError, OSError, RuntimeError, MemoryError) as exc:
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            log.error("forecast job %s failed: %s", job.job_id, job.error)
        finally:
            job.finished_at = utcnow().isoformat()
            job.duration_s = round(time.perf_counter() - t0, 3)

    def status(self, job_id: str) -> dict[str, Any]:
        from app.core.errors import NotFoundError

        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"forecast job {job_id} not found")
        return job.to_dict()

    def list(self, election_id: int | None = None) -> list[dict[str, Any]]:
        with self._lock:
            jobs = list(self._jobs.values())
        return [j.to_dict() for j in reversed(jobs) if election_id is None or j.election_id == election_id]

    def wait(self, job_id: str, timeout: float = 60.0) -> dict[str, Any]:
        """Block until the job has finished (tests, CLI) and return its status."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.status(job_id)
            if st["status"] in ("completed", "failed"):
                return st
            time.sleep(0.05)
        return self.status(job_id)


_jobs = ForecastJobs()


def start_forecast_job(
    election_id: int,
    simulations: int,
    seed: int,
    *,
    workers: int = 0,
    use_polls: bool = True,
    include_local: bool = False,
    db_url: str | None = None,
) -> str:
    """Queue a background forecast of ``election_id`` → job id (see :func:`job_status`)."""
    return _jobs.start(
        election_id,
        simulations,
        seed,
        workers=workers,
        use_polls=use_polls,
        include_local=include_local,
        db_url=db_url,
    )


def job_status(job_id: str) -> dict[str, Any]:
    """``{job_id, election_id, status, progress, done, total, run_id, error, …}``."""
    return _jobs.status(job_id)


def list_jobs(election_id: int | None = None) -> list[dict[str, Any]]:
    """Known jobs (newest first), optionally of one election."""
    return _jobs.list(election_id)


def wait_for_job(job_id: str, timeout: float = 60.0) -> dict[str, Any]:
    """Block until a job finishes (tests / CLI)."""
    return _jobs.wait(job_id, timeout)
