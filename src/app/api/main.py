"""FastAPI application factory.

    from app.api.main import create_app
    app = create_app()                       # configured database (NLFED_DATABASE_URL / data/nlfed.db)
    # uvicorn app.api.main:app  |  python -m app run

* JSON API under ``/api`` (OpenAPI docs at ``/api/docs``, schema at ``/api/openapi.json``);
  every endpoint and payload is documented in docs/API.md.
* The single-page UI (``src/app/ui/static``) is mounted at ``/`` **after** the API routes;
  unknown non-API paths serve ``index.html`` (a placeholder page until the UI exists), unknown
  ``/api`` paths answer a JSON 404.
* Start-up never fails on an unprepared system: the lifespan configures logging and records the
  database / geography readiness, which ``GET /api/health`` reports; data endpoints answer
  ``503 {"code": "not_prepared"}`` until the setup has run.
* Errors are ``{"detail", "code"}`` with a proper status (:mod:`app.api.errors`); responses are
  gzip-compressed (except Server-Sent Events); CORS is off unless origins are configured
  (``cors_origins`` or ``NLFED_CORS_ORIGINS``, comma-separated).

The API performs no election mathematics: every payload comes from :mod:`app.services.read`
(read models), :mod:`app.services.read.actions` / :mod:`app.services.forecast_runner` (writes) and
the night service.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

import app as app_pkg
from app.api.deps import ApiJSON, Database
from app.api.errors import install_error_handlers
from app.api.routers import (
    analytics,
    campaigns,
    candidates,
    data,
    districts,
    elections,
    export,
    forecast,
    geography,
    history,
    meta,
    night,
    polls,
    scenarios,
)
from app.api.routers import settings as ui_settings
from app.api.static import install_api_catch_all, mount_ui
from app.core.logging import configure_logging, get_logger, log_ctx
from app.core.settings import get_settings

log = get_logger(__name__)

API_PREFIX = "/api"
CORS_ENV = "NLFED_CORS_ORIGINS"

OPENAPI_TAGS = [
    {"name": "system", "description": "Health, meta (constitution, elections), UI settings, provenance."},
    {"name": "geography", "description": "REAL geography and the FICTIONAL electoral geography; GeoJSON."},
    {"name": "elections", "description": "Elections and results pages (hidden until reported)."},
    {"name": "election night", "description": "Live SIMULATED election nights (night service)."},
    {"name": "forecast", "description": "Monte Carlo forecasts — model estimates, not predictions."},
    {"name": "polls", "description": "FICTIONAL polls and SIMULATED averages."},
    {"name": "campaigns", "description": "Campaign plans and effects."},
    {"name": "history", "description": "History across reported elections."},
    {"name": "analytics", "description": "Per-election analytics."},
    {"name": "parties & candidates", "description": "FICTIONAL parties and people."},
    {"name": "scenarios", "description": "Scenario editor."},
    {"name": "export", "description": "Stable-schema CSV / JSON exports."},
]


def create_app(
    db_url: str | None = None,
    *,
    ui_dir: Path | str | None = None,
    user_scenarios_dir: Path | str | None = None,
    cors_origins: Sequence[str] | None = None,
    configure_logs: bool = True,
) -> FastAPI:
    """Build the application.

    Args:
        db_url: database URL (default: the configured ``settings.db_url``).
        ui_dir: directory of the static UI (default ``src/app/ui/static``).
        user_scenarios_dir: where the scenario editor saves user scenarios (default
            ``config/scenarios/user``).
        cors_origins: allowed CORS origins (default: ``NLFED_CORS_ORIGINS``; none = CORS off).
        configure_logs: install the application's logging configuration at start-up.
    """
    settings = get_settings()
    url = db_url or settings.db_url
    if cors_origins is None:
        env = os.environ.get(CORS_ENV, "")
        cors_origins = [o.strip() for o in env.split(",") if o.strip()]

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if configure_logs:
            configure_logging(settings.log_level, settings.log_json)
        ready, reason = application.state.db.ready()
        from app.services.read.meta import health

        report = health(url, application.state.ui_index)
        application.state.startup = report
        log.info(
            "API started",
            extra=log_ctx(
                ready=report["ready"],
                database=report["database"]["database"],
                geography=report["geography"]["vintage_year"],
                ui_built=report["ui_built"],
            ),
        )
        if not ready:
            log.warning("database not ready: %s — data endpoints answer 503 until the setup has run", reason)
        yield
        from app.services.read.live import close_manager_for_url

        close_manager_for_url(url)

    application = FastAPI(
        title="NL Federal Election Simulator API",
        version=app_pkg.__version__,
        description=(
            "JSON API of a FICTIONAL U.S.-style federal electoral system over the REAL geography of the "
            "Netherlands. Every payload carries a data_category (REAL / DERIVED / FICTIONAL / SIMULATED). "
            "See docs/API.md for the full contract."
        ),
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=f"{API_PREFIX}/redoc",
        openapi_url=f"{API_PREFIX}/openapi.json",
        swagger_ui_oauth2_redirect_url=f"{API_PREFIX}/docs/oauth2-redirect",
        openapi_tags=OPENAPI_TAGS,
        default_response_class=ApiJSON,
        lifespan=lifespan,
    )
    application.state.db = Database(url)
    application.state.user_scenarios_dir = (
        Path(user_scenarios_dir) if user_scenarios_dir else _default_user_dir()
    )
    install_error_handlers(application)
    application.add_middleware(GZipMiddleware, minimum_size=1024)
    if cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["*"],
        )
    for module in (
        meta,
        ui_settings,
        data,
        geography,
        districts,
        elections,
        night,
        forecast,
        polls,
        campaigns,
        history,
        analytics,
        candidates,
        scenarios,
        export,
    ):
        application.include_router(module.router, prefix=API_PREFIX)
    install_api_catch_all(application)
    application.state.ui_index = mount_ui(application, Path(ui_dir) if ui_dir is not None else None)
    return application


def _default_user_dir() -> Path:
    from app.services.read.scenarios import default_user_dir

    return default_user_dir()


_app: FastAPI | None = None


def __getattr__(name: str) -> FastAPI:
    """``app.api.main:app`` for ``uvicorn`` (built lazily on first access)."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(name)
