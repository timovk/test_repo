"""Shared plumbing of the command-line interface: console, logging flags, friendly errors,
database sessions, election resolution and small rendering helpers.

The CLI contains no election mathematics: every command calls a service and renders its result.
"""

from __future__ import annotations

import functools
import json
import logging
import os
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any, TypeVar

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import (
    ConfigError,
    DataNotPreparedError,
    DistrictingError,
    DownloadError,
    ElectionError,
    ElectionNightError,
    NLFedError,
    NotFoundError,
    ScenarioError,
    ValidationError,
)

F = TypeVar("F", bound=Callable[..., Any])

#: Console for command output (stdout) and for diagnostics (stderr).
console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)

#: Exit codes of the CLI.
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_NOT_PREPARED = 3
EXIT_NOT_FOUND = 4
EXIT_INVALID = 5
EXIT_CONFLICT = 6
EXIT_INTERRUPTED = 130

_EXIT_CODES: tuple[tuple[type[BaseException], int, str], ...] = (
    (DataNotPreparedError, EXIT_NOT_PREPARED, "Data not prepared"),
    (NotFoundError, EXIT_NOT_FOUND, "Not found"),
    (ScenarioError, EXIT_INVALID, "Invalid scenario"),
    (ValidationError, EXIT_INVALID, "Validation failed"),
    (ConfigError, EXIT_INVALID, "Configuration error"),
    (ElectionNightError, EXIT_CONFLICT, "Election night"),
    (ElectionError, EXIT_CONFLICT, "Election"),
    (DistrictingError, EXIT_FAILURE, "Districting failed"),
    (DownloadError, EXIT_FAILURE, "Download failed"),
    (NLFedError, EXIT_FAILURE, "Error"),
)


def exit_code_for(exc: BaseException) -> tuple[int, str]:
    """(exit code, headline) of an application error."""
    for cls, code, title in _EXIT_CODES:
        if isinstance(exc, cls):
            return code, title
    return EXIT_FAILURE, "Error"


def friendly(fn: F) -> F:
    """Command decorator: application errors become a clear message and an exit code instead of
    a traceback (``--verbose`` adds the traceback)."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except NLFedError as exc:
            code, title = exit_code_for(exc)
            err_console.print(f"[bold red]{title}:[/] {exc}")
            if isinstance(exc, DataNotPreparedError):
                err_console.print(
                    "[dim]Prepare the REAL geography with `python -m app geography download` and "
                    "`python -m app geography build` (or run `python -m app demo`).[/]"
                )
            if logging.getLogger().isEnabledFor(logging.DEBUG):
                err_console.print_exception()
            raise typer.Exit(code) from exc
        except KeyboardInterrupt as exc:
            err_console.print("[yellow]Interrupted.[/]")
            raise typer.Exit(EXIT_INTERRUPTED) from exc

    return wrapper  # type: ignore[return-value]


# =========================================================================== logging
def setup_logging(verbose: int, json_logs: bool) -> None:
    """Configure logging for a CLI run: warnings only by default, ``-v`` info, ``-vv`` debug;
    human-readable logs go to stderr so command output stays clean."""
    from app.core.logging import configure_logging

    level = "DEBUG" if verbose >= 2 else ("INFO" if verbose == 1 else "WARNING")
    configure_logging(level, json_lines=json_logs)
    if not json_logs:
        for handler in logging.getLogger().handlers:
            if hasattr(handler, "console"):
                handler.console = Console(stderr=True)


def set_database_url(url: str | None) -> None:
    """Point this run at another database (same as ``NLFED_DATABASE_URL``)."""
    if not url:
        return
    from app.core.settings import reset_settings_cache
    from app.db.session import reset_engines
    from app.services.night import reset_night_manager

    os.environ["NLFED_DATABASE_URL"] = url
    reset_settings_cache()
    reset_engines()
    reset_night_manager()


# =========================================================================== database
@contextmanager
def session_scope() -> Iterator[Session]:
    """A committed unit of work on the configured database."""
    from app.db.session import session_scope as scope

    with scope() as s:
        yield s


def resolve_election(session: Session, ref: str | int | None) -> int:
    """Election id from an id, a year (the most recent election of that year), ``latest`` or
    ``demo`` (``app_meta.demo_election_id``)."""
    from app.models import AppMeta, Election

    if ref is None or str(ref).strip() == "":
        raise NotFoundError("no election given (use --election <id|year|latest|demo>)")
    text = str(ref).strip().lower()
    if text == "demo":
        row = session.get(AppMeta, "demo_election_id")
        if row is not None and row.value.isdigit() and session.get(Election, int(row.value)) is not None:
            return int(row.value)
        text = "latest"
    if text == "latest":
        eid = session.scalar(select(Election.id).order_by(Election.election_date.desc(), Election.id.desc()))
        if eid is None:
            raise NotFoundError("there are no elections yet (run `python -m app demo`)")
        return int(eid)
    if not text.isdigit():
        raise NotFoundError(f"election {ref!r} not understood (use an id, a year, latest or demo)")
    value = int(text)
    if value >= 1000:
        eid = session.scalar(select(Election.id).where(Election.year == value).order_by(Election.id.desc()))
        if eid is None:
            raise NotFoundError(f"no election in {value}")
        return int(eid)
    if session.get(Election, value) is None:
        raise NotFoundError(f"election {value} not found")
    return value


# =========================================================================== rendering
def print_json(data: Any) -> None:
    """Machine-readable output (sorted keys, ISO dates)."""
    console.print_json(json.dumps(data, default=str, ensure_ascii=False, sort_keys=True))


def table(
    title: str | None, columns: Sequence[str | tuple[str, str]], rows: Sequence[Sequence[Any]]
) -> Table:
    """A rich table; a column given as ``(name, "right")`` is right-aligned."""
    t = Table(title=title, title_justify="left", header_style="bold")
    for col in columns:
        if isinstance(col, tuple):
            t.add_column(col[0], justify=col[1])  # type: ignore[arg-type]
        else:
            t.add_column(col)
    for row in rows:
        t.add_row(*["" if v is None else str(v) for v in row])
    return t


def fmt_int(v: Any) -> str:
    return "" if v is None else f"{int(v):,}"


def fmt_pct(v: Any, digits: int = 1) -> str:
    return "" if v is None else f"{float(v):.{digits}f}%"


def swatch(color: str | None, text: str) -> str:
    """``text`` in a party colour (rich markup)."""
    return f"[{color}]{text}[/]" if color else text


@contextmanager
def spinner(label: str, enabled: bool = True) -> Iterator[None]:
    """A progress spinner on stderr for long steps."""
    if not enabled or not err_console.is_terminal:
        yield
        return
    with err_console.status(label, spinner="dots"):
        yield


def data_badge(category: str) -> str:
    colors = {"REAL": "green", "DERIVED": "cyan", "FICTIONAL": "magenta", "SIMULATED": "yellow"}
    return f"[bold {colors.get(category, 'white')}]{category}[/]"
