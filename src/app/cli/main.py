"""The ``nlfed`` / ``python -m app`` command-line interface (Typer + rich).

Every command calls the services layer (no election mathematics here); see docs/CLI.md for the
full reference with examples.  Global options: ``--verbose/-v`` (repeat for debug logs),
``--json-logs`` (JSON lines on stderr) and ``--database-url`` (same as ``NLFED_DATABASE_URL``).
"""

from __future__ import annotations

import typer

from app import __version__
from app.cli._common import console, set_database_url, setup_logging
from app.cli.db import db_app
from app.cli.districts import apportion, districts_app
from app.cli.elections import (
    election_app,
    export,
    finalize,
    forecast,
    history,
    polls,
    simulate,
)
from app.cli.geography import geography_app
from app.cli.night import election_night
from app.cli.scenario import scenario_app
from app.cli.system import demo, init, run, validate

app = typer.Typer(
    name="nlfed",
    help=(
        "NL Federal Election Simulator — the REAL geography of the Netherlands under a FICTIONAL "
        "U.S.-style federal constitution (174 electoral votes, 88 to win; 150 House districts, 76 for "
        "control; 24 senators, 13 for control).  All elections are SIMULATED."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
    rich_markup_mode="rich",
)


def _version(value: bool) -> None:
    if value:
        console.print(f"nlfed {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    verbose: int = typer.Option(0, "--verbose", "-v", count=True, help="More logging (-v info, -vv debug)."),
    json_logs: bool = typer.Option(False, "--json-logs", help="Log JSON lines to stderr."),
    database_url: str | None = typer.Option(
        None,
        "--database-url",
        help="Database URL for this run (default: NLFED_DATABASE_URL or data/nlfed.db).",
    ),
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    """NL Federal Election Simulator."""
    setup_logging(verbose, json_logs)
    set_database_url(database_url)


# ---- system
app.command("init")(init)
app.command("demo")(demo)
app.command("run")(run)
app.command("validate")(validate)
# ---- geography, apportionment, districts
app.add_typer(geography_app, name="geography", help="REAL geography: download, build and load the CBS data.")
app.command("apportion")(apportion)
app.add_typer(districts_app, name="districts", help="FICTIONAL House districts: generate, validate, show.")
# ---- elections
app.add_typer(election_app, name="election", help="Create, list and inspect elections.")
app.command("simulate")(simulate)
app.command("finalize")(finalize)
app.command("forecast")(forecast)
app.command("election-night")(election_night)
app.command("export")(export)
app.command("polls")(polls)
app.command("history")(history)
# ---- scenarios and database
app.add_typer(scenario_app, name="scenario", help="FICTIONAL scenario documents.")
app.add_typer(db_app, name="db", help="Database schema migrations (Alembic).")


def main() -> None:
    """Console-script entry point (``nlfed``) and ``python -m app``."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
