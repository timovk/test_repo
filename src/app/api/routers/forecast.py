"""Monte Carlo forecasts (SIMULATED model estimates, never predictions): background runs and
stored results."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import ApiJSON, ElectionDep, SessionDep, respond
from app.forecasting.config import load_forecast_config
from app.models import SimulationRun
from app.services import forecast_runner
from app.services.read import forecast as fc

router = APIRouter(prefix="/forecast", tags=["forecast"])


class ForecastRun(BaseModel):
    simulations: int = Field(10000, ge=1, description="number of simulated elections")
    seed: int | None = Field(
        None, ge=0, lt=2**62, description="root seed (default: derived from the election seed)"
    )
    workers: int = Field(0, ge=0, le=64, description="0 = automatic, 1 = in-process, ≥ 2 = worker processes")
    use_polls: bool = Field(True, description="condition the national environment on the polling average")
    include_local: bool = Field(
        False, description="also forecast mayors, councils and provincial legislatures"
    )


def _include(include: str | None) -> tuple[bool, bool]:
    parts = {p.strip() for p in (include or "").split(",") if p.strip()}
    return "races" in parts, "municipalities" in parts


@router.post("/{election_id}/run", status_code=202, summary="Start a background forecast run")
def run(ref: ElectionDep, body: ForecastRun, request: Request, session: SessionDep) -> ApiJSON:
    cfg = load_forecast_config()
    if body.simulations > int(cfg.max_simulations):
        from app.core.errors import ValidationError

        raise ValidationError(f"simulations must be ≤ {cfg.max_simulations}")
    seed = body.seed
    if seed is None:
        n = session.scalar(
            select(func.count())
            .select_from(SimulationRun)
            .where(SimulationRun.kind == "forecast", SimulationRun.election_id == ref.id)
        )
        seed = forecast_runner.default_forecast_seed(ref.seed, int(n or 0))
    job_id = forecast_runner.start_forecast_job(
        ref.id,
        body.simulations,
        seed,
        workers=body.workers,
        use_polls=body.use_polls,
        include_local=body.include_local,
        db_url=request.app.state.db.url,
    )
    status = forecast_runner.job_status(job_id)
    return respond({**status, "status_url": f"/api/forecast/jobs/{job_id}"}, status_code=202)


@router.get("/jobs", summary="Forecast jobs of this process (newest first)")
def jobs(election: int | None = None) -> ApiJSON:
    return respond({"jobs": forecast_runner.list_jobs(election)})


@router.get("/jobs/{job_id}", summary="Status of a forecast job")
def job(job_id: str) -> ApiJSON:
    return respond(forecast_runner.job_status(job_id))


@router.get("/runs/{run_id}", summary="A stored forecast run")
def run_view(
    run_id: int, session: SessionDep, include: str | None = Query(None, description="races,municipalities")
) -> ApiJSON:
    races, munis = _include(include)
    return respond(fc.forecast_view(session, run_id, include_races=races, include_municipalities=munis))


@router.get("/{election_id}/latest", summary="The newest forecast of an election")
def latest(
    ref: ElectionDep,
    session: SessionDep,
    include: str | None = Query(None, description="races,municipalities"),
) -> ApiJSON:
    races, munis = _include(include)
    return respond(fc.latest_view(session, ref, include_races=races, include_municipalities=munis))


@router.get("/{election_id}/runs", summary="Forecast runs of an election")
def runs(ref: ElectionDep, session: SessionDep) -> ApiJSON:
    return respond(fc.runs(session, ref))
