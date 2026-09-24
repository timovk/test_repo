"""Static single-page UI hosting.

The UI (``src/app/ui/static``) is mounted at ``/`` after every API route.  Unknown non-API paths
serve ``index.html`` (client-side routing); when the UI has not been built yet a minimal page
says so and links to the API docs.  Unknown ``/api/...`` paths never fall through to the UI —
:func:`install_api_catch_all` answers them with a JSON 404.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app.api.deps import ApiJSON

#: Default location of the built UI.
UI_DIR = Path(__file__).resolve().parents[1] / "ui" / "static"

PLACEHOLDER_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NL Federal Election Simulator</title>
<style>body{font-family:system-ui,sans-serif;background:#0f1420;color:#e8ecf3;display:grid;place-items:center;
min-height:100vh;margin:0}main{max-width:560px;padding:24px}a{color:#7fb2ff}code{background:#1c2433;padding:2px 6px;border-radius:4px}
small{color:#9aa4b5}</style></head>
<body><main><h1>NL Federal Election Simulator</h1>
<p>The user interface is being built. The JSON API is running: explore it at <a href="/api/docs">/api/docs</a>
or check <a href="/api/health">/api/health</a>.</p>
<p><small>FICTIONAL constitutional system over the REAL geography of the Netherlands (CBS/PDOK).
All results are simulations, not predictions.</small></p></main></body></html>
"""


class SPAStaticFiles(StaticFiles):
    """``StaticFiles`` that serves ``index.html`` for unknown paths outside ``/api``."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or path.startswith("api"):
                raise
        index = Path(str(self.directory)) / "index.html"
        if index.exists():
            return await super().get_response("index.html", scope)
        return HTMLResponse(PLACEHOLDER_HTML)


def install_api_catch_all(app: FastAPI) -> None:
    """JSON 404 for unknown ``/api`` paths (registered after every API router)."""

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def _api_not_found(path: str, request: Request) -> ApiJSON:
        return ApiJSON(
            {"detail": f"no API endpoint {request.method} /api/{path}", "code": "not_found"}, status_code=404
        )


def mount_ui(app: FastAPI, ui_dir: Path | None = None) -> Path:
    """Mount the UI at ``/`` (placeholder page when ``index.html`` does not exist yet)."""
    directory = Path(ui_dir) if ui_dir is not None else UI_DIR
    if directory.is_dir():
        app.mount("/", SPAStaticFiles(directory=str(directory), html=True), name="ui")
    else:

        @app.get("/{path:path}", include_in_schema=False)
        async def _placeholder(path: str) -> HTMLResponse:
            return HTMLResponse(PLACEHOLDER_HTML)

    return directory / "index.html"
