"""Structured logging setup.

``configure_logging()`` installs either a human-friendly Rich handler or a JSON-lines
handler (``NLFED_LOG_JSON=1``).  Modules obtain loggers via ``get_logger(__name__)`` and pass
structured context through ``extra={"ctx": {...}}`` or keyword helpers.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update(ctx)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class ContextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict) and ctx:
            base += "  " + " ".join(f"{k}={v}" for k, v in ctx.items())
        return base


def configure_logging(level: str = "INFO", json_lines: bool = False) -> None:
    global _CONFIGURED
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    if json_lines:
        handler: logging.Handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter())
    else:
        try:
            from rich.logging import RichHandler

            handler = RichHandler(rich_tracebacks=True, show_path=False, markup=False)
            handler.setFormatter(ContextFormatter("%(message)s"))
        except ImportError:  # pragma: no cover
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(ContextFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("urllib3", "httpx", "httpcore", "fiona", "pyogrio", "shapely"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_ctx(**ctx: Any) -> dict[str, Any]:
    """Helper: ``logger.info("msg", extra=log_ctx(seed=42))``."""
    return {"ctx": ctx}


class Timer:
    """Context manager that logs elapsed time: ``with Timer(log, "build adjacency"): ...``."""

    def __init__(self, logger: logging.Logger, label: str, level: int = logging.INFO) -> None:
        self.logger, self.label, self.level = logger, label, level
        self.elapsed = 0.0

    def __enter__(self) -> Timer:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.elapsed = time.perf_counter() - self._t0
        self.logger.log(self.level, "%s took %.2fs", self.label, self.elapsed, extra=log_ctx(seconds=round(self.elapsed, 3)))
