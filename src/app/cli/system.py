"""System commands: ``init``, ``demo``, ``run`` and ``validate``."""

from __future__ import annotations

import typer

from app.cli._common import (
    EXIT_FAILURE,
    console,
    data_badge,
    err_console,
    fmt_int,
    friendly,
    print_json,
    resolve_election,
    session_scope,
    spinner,
    table,
)
from app.core.errors import NotFoundError


@friendly
def init(
    with_geography: bool = typer.Option(
        False,
        "--with-geography",
        help="Also prepare the REAL geography (download + build when missing) and set up the system.",
    ),
    year: int | None = typer.Option(None, "--year", help="Geography vintage (default 2025)."),
    district_seed: int = typer.Option(2028, "--district-seed", help="Seed of the House district plan."),
    workers: int = typer.Option(0, "--workers", help="Worker processes for districting (0 = automatic)."),
) -> None:
    """Create or upgrade the database schema (optionally: geography, apportionment, districts, offices)."""
    from app.core.settings import get_settings
    from app.services.bootstrap import init_db, prepare_geography, setup_system

    settings = get_settings()
    settings.ensure_dirs()
    with spinner("migrating the database …"):
        init_db()
    console.print(f"database ready: {settings.db_url if '@' not in settings.db_url else '(configured URL)'}")
    if not with_geography:
        return
    from app.geography.store import is_prepared

    if not is_prepared(year):
        with spinner("downloading and building the REAL geography …"):
            prepare_geography(year, download=True)
    with session_scope() as s, spinner("setting up the system (apportionment, districts, offices) …"):
        rep = setup_system(s, year=year, district_seed=district_seed, workers=workers)
    console.print(
        f"{data_badge('REAL')} {rep.provinces} provinces, {fmt_int(rep.municipalities)} municipalities, "
        f"{fmt_int(rep.units)} units · {data_badge('FICTIONAL')} {rep.districts} House districts "
        f"(plan {rep.plan_id}{', reused' if rep.plan_reused else ''}), {rep.senate_seats} Senate seats, "
        f"{fmt_int(rep.offices)} offices"
    )


@friendly
def demo(
    seed: int = typer.Option(2028, "--seed", help="Seed of the House plan and the 2028 forecast."),
    force: bool = typer.Option(False, "--force", help="Rebuild the database from scratch."),
    forecast_simulations: int = typer.Option(
        10000, "--forecast-simulations", min=0, help="Monte Carlo draws of the 2028 forecast (0 = none)."
    ),
    election_seed: int | None = typer.Option(
        None, "--election-seed", help="Derive every election's seed from this (default: scenario seeds)."
    ),
    synthetic: bool = typer.Option(
        False, "--synthetic", help="Use the synthetic toy country instead of the REAL geography (offline)."
    ),
    workers: int = typer.Option(0, "--workers", help="Worker processes (0 = automatic)."),
    as_json: bool = typer.Option(False, "--json", help="Print the summary as JSON."),
) -> None:
    """Build the reproducible demo: 2024 and 2026 as history, 2028 ready for a live election night."""
    from app.services.demo import build_demo

    status_ctx = (
        err_console.status("building the demo …", spinner="dots") if err_console.is_terminal else None
    )

    def progress(step: str, status: str, detail: str) -> None:
        if status == "start":
            if status_ctx is not None:
                status_ctx.update(f"{step} …")
            return
        style = {"done": "green", "skipped": "dim", "warning": "yellow"}.get(status, "white")
        err_console.print(f"[{style}]{status:>7}[/] {step}: {detail}")

    if status_ctx is not None:
        status_ctx.start()
    try:
        summary = build_demo(
            seed=seed,
            force=force,
            forecast_simulations=forecast_simulations,
            progress=progress,
            synthetic=synthetic,
            election_seed=election_seed,
            workers=workers,
        )
    finally:
        if status_ctx is not None:
            status_ctx.stop()
    if as_json:
        print_json(summary.to_dict())
        return
    rows = [(s.name, s.status, f"{s.seconds:.1f}s", s.detail) for s in summary.steps]
    console.print(table("demo build", ["step", "status", ("time", "right"), "detail"], rows))
    size = f" · database {summary.database_bytes / 1e6:.0f} MB" if summary.database_bytes else ""
    console.print(
        f"demo ready in {summary.total_seconds:.1f}s{size} · elections {summary.elections} · "
        f"demo election {summary.demo_election_id} (SIMULATED, night at polls closing) · "
        f"validation {'ok' if summary.validation_ok else 'FAILED'}"
    )
    console.print("next: `python -m app run` (web UI) or `python -m app election-night --election demo`")


@friendly
def run(
    host: str | None = typer.Option(None, "--host", help="Bind address (default from settings)."),
    port: int | None = typer.Option(None, "--port", help="Port (default from settings, 8000)."),
    reload: bool = typer.Option(False, "--reload", help="Reload on code changes (development)."),
) -> None:
    """Serve the API and the web UI (uvicorn, app.api.main:create_app)."""
    import importlib.util

    import uvicorn

    from app.core.settings import get_settings

    if importlib.util.find_spec("app.api.main") is None:
        raise NotFoundError("the API application (app.api.main) is not installed")
    settings = get_settings()
    uvicorn.run(
        "app.api.main:create_app",
        factory=True,
        host=host or settings.host,
        port=int(port or settings.port),
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@friendly
def validate(
    election: str | None = typer.Option(None, "--election", "-e", help="Validate only this election."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Validate the constitution, the geography, the House plan, the Senate and every election
    (exit code 1 on failure)."""
    from app.services.validation import validate_election, validate_system

    with session_scope() as s, spinner("validating …"):
        rep = validate_election(s, resolve_election(s, election)) if election else validate_system(s)
    if as_json:
        print_json(rep.to_dict())
    else:
        rows = [
            (
                "[green]ok[/]"
                if c.ok
                else ("[yellow]warn[/]" if c.severity == "warning" else "[red]FAIL[/]"),
                c.name,
                c.detail,
            )
            for c in rep.checks
        ]
        console.print(table("validation", ["", "check", "detail"], rows))
        console.print(
            f"[bold {'green' if rep.ok else 'red'}]{'valid' if rep.ok else 'INVALID'}[/] · "
            f"{len(rep.checks)} checks · {len(rep.errors)} errors · {len(rep.warnings)} warnings"
        )
    if not rep.ok:
        raise typer.Exit(EXIT_FAILURE)
