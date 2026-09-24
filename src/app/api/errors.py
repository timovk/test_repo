"""Exception handlers: application errors → ``{"detail": str, "code": str}`` with a proper
HTTP status.

| exception | status | code |
|---|---|---|
| ``NotFoundError`` | 404 | ``not_found`` |
| ``ValidationError``, ``ScenarioError``, ``ConfigError``, ``ValueError``, request validation | 422 | ``invalid`` |
| ``ElectionError``, ``ElectionNightError`` (state conflicts) | 409 | ``conflict`` |
| ``DataNotPreparedError`` | 503 | ``not_prepared`` |
| ``ServiceUnavailableError`` | 503 | ``unavailable`` |
| other ``NLFedError`` | 400 | ``error`` |
| unexpected exceptions | 500 | ``internal`` |
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.deps import ApiJSON
from app.core.errors import (
    ConfigError,
    DataNotPreparedError,
    DownloadError,
    ElectionError,
    ElectionNightError,
    NLFedError,
    NotFoundError,
    ScenarioError,
    ValidationError,
)
from app.core.logging import get_logger
from app.services.read.live import ServiceUnavailableError

log = get_logger(__name__)

_HTTP_CODES = {
    400: "error",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "too_large",
    415: "unsupported_media_type",
    422: "invalid",
    503: "unavailable",
}


def error_response(status: int, detail: str, code: str, **extra: object) -> ApiJSON:
    return ApiJSON({"detail": detail, "code": code, **extra}, status_code=status)


def status_of(exc: Exception) -> tuple[int, str]:
    """HTTP status and error code of an application exception."""
    if isinstance(exc, NotFoundError):
        return 404, "not_found"
    if isinstance(exc, DataNotPreparedError):
        return 503, "not_prepared"
    if isinstance(exc, ServiceUnavailableError):
        return 503, "unavailable"
    if isinstance(exc, ElectionError | ElectionNightError):
        return 409, "conflict"
    if isinstance(exc, ValidationError | ScenarioError | ConfigError | ValueError):
        return 422, "invalid"
    if isinstance(exc, DownloadError):
        return 502, "upstream"
    return 400, "error"


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers on ``app``."""

    @app.exception_handler(NLFedError)
    async def _app_error(_request: Request, exc: NLFedError) -> ApiJSON:
        status, code = status_of(exc)
        extra = {"problems": exc.problems} if isinstance(exc, ValidationError) and exc.problems else {}
        return error_response(status, str(exc), code, **extra)

    @app.exception_handler(ValueError)
    async def _value_error(_request: Request, exc: ValueError) -> ApiJSON:
        return error_response(422, str(exc), "invalid")

    @app.exception_handler(RequestValidationError)
    async def _request_invalid(_request: Request, exc: RequestValidationError) -> ApiJSON:
        errors = [
            {
                "loc": [str(x) for x in e.get("loc", ())],
                "msg": str(e.get("msg", "")),
                "type": str(e.get("type", "")),
            }
            for e in exc.errors()
        ]
        detail = "; ".join(f"{'.'.join(e['loc'])}: {e['msg']}" for e in errors) or "invalid request"
        return error_response(422, detail, "invalid", errors=errors)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> ApiJSON:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return ApiJSON(
            {"detail": detail, "code": _HTTP_CODES.get(exc.status_code, "error")},
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> ApiJSON:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return error_response(500, f"internal error: {type(exc).__name__}", "internal")
