"""Local election commands: ``local calendar | list | create | schedule`` and ``finish-earlier``.

Between the big November elections every province holds a few local election days a year
(school boards, water boards, ballot measures, special elections and mayor recalls — FICTIONAL
contests over REAL geography; docs/LOCAL_ELECTIONS.md).  All provinces voting on a date form one
local election with one combined election night.  Elections are certified in strict date
order: ``finish-earlier`` holds every unfinished election before a given one.
"""

from __future__ import annotations

from datetime import date, timedelta

import typer

from app.cli._common import (
    console,
    data_badge,
    friendly,
    print_json,
    resolve_election,
    session_scope,
    spinner,
    table,
)

local_app = typer.Typer(no_args_is_help=True)

_KIND_SHORT = {
    "school_board": "school boards",
    "water_board": "water boards",
    "measure": "measures",
    "mayor_special": "mayor specials",
    "council_seat": "council specials",
    "recall": "recalls",
}


def _parse_date(text: str | None, what: str) -> date | None:
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise typer.BadParameter(f"{what} must be a date YYYY-MM-DD") from exc


@local_app.command("calendar")
@friendly
def calendar_cmd(
    start: str | None = typer.Option(
        None, "--start", help="First date (default: the latest reported election)."
    ),
    end: str | None = typer.Option(None, "--end", help="Last date (default: start + 12 months)."),
    year: int | None = typer.Option(None, "--year", help="One calendar year (overrides --start/--end)."),
    province: str | None = typer.Option(None, "--province", "-p", help="Province code, e.g. GE."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Planned local election days and what is on each ballot."""
    from app.services.read.local import calendar

    lo, hi = _parse_date(start, "--start"), _parse_date(end, "--end")
    if year is not None:
        lo, hi = date(year, 1, 1), date(year, 12, 31)
    with session_scope() as s:
        data = calendar(s, start=lo, end=hi, province=province)
    if as_json:
        print_json(data)
        return
    body = [
        (
            d["date"],
            ", ".join(d["provinces"]),
            d["races"],
            ", ".join(f"{n} {_KIND_SHORT.get(k, k)}" for k, n in sorted(d["counts"].items())),
            len(d["municipalities"]),
            "" if d["election_id"] is None else f"{d['election_id']} ({d['status']})",
        )
        for d in data["days"]
    ]
    console.print(
        table(
            f"{data_badge('FICTIONAL')} local election days {data['start']} – {data['end']}",
            ["date", "provinces", ("races", "right"), "contests", ("municipalities", "right"), "election"],
            body,
        )
    )


@local_app.command("list")
@friendly
def list_cmd(
    year: int | None = typer.Option(None, "--year", help="Only this year."),
    province: str | None = typer.Option(None, "--province", "-p", help="Province code."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """The stored local elections (newest first)."""
    from app.services.read.local import elections

    with session_scope() as s:
        data = elections(s, year=year, province=province)
    if as_json:
        print_json(data)
        return
    body = [
        (e["id"], e["date"], ", ".join(e["provinces"]), e["status"], e["races"], e["name"])
        for e in data["elections"]
    ]
    console.print(
        table(
            f"{data_badge('SIMULATED')} local elections ({data['count']})",
            [("id", "right"), "date", "provinces", "status", ("races", "right"), "name"],
            body,
        )
    )


@local_app.command("create")
@friendly
def create_cmd(
    on: str = typer.Option(..., "--date", "-d", help="A local election date (YYYY-MM-DD)."),
    simulate: bool = typer.Option(False, "--simulate", help="Simulate the hidden result right away."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Create the local election of a date (every province voting that day)."""
    from app.services.elections import election_summary, simulate_election
    from app.services.local import create_local_election

    day = _parse_date(on, "--date")
    assert day is not None
    with session_scope() as s:
        el = create_local_election(s, day)
        if simulate:
            simulate_election(s, el.id)
        eid = el.id
    with session_scope() as s:
        summary = election_summary(s, eid)
    if as_json:
        print_json(summary)
        return
    counts = (summary.get("contents") or {}).get("race_counts") or {}
    console.print(
        f"{data_badge('SIMULATED')} local election {eid}: {summary['name']} · {sum(counts.values())} races "
        f"· status {summary['status']}"
    )


@local_app.command("schedule")
@friendly
def schedule_cmd(
    until: str = typer.Option(..., "--until", help="Create every planned local election up to this date."),
) -> None:
    """Create (SCHEDULED) every planned local election up to a date."""
    from app.services.local import ensure_local_elections

    day = _parse_date(until, "--until")
    assert day is not None
    with session_scope() as s, spinner("scheduling local elections …"):
        ids = ensure_local_elections(s, day)
    console.print(f"{data_badge('FICTIONAL')} {len(ids)} local elections scheduled up to {day.isoformat()}")


@friendly
def finish_earlier(
    election: str = typer.Option(..., "--election", "-e", help="Election id, year, 'latest' or 'demo'."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Finish at most this many."),
) -> None:
    """Finish every unfinished election held before an election (strict date order): each is
    created if needed, simulated and run through an instant election night."""
    from app.models import Election
    from app.services import local as local_service
    from app.services.elections import simulate_election
    from app.services.night import run_instant_night

    with session_scope() as s:
        eid = resolve_election(s, election)
        target = s.get(Election, eid)
        assert target is not None
        on = target.election_date
        local_service.ensure_local_elections(s, on - timedelta(days=1))
    done = 0
    while limit is None or done < limit:
        with session_scope() as s:
            pending = local_service.unreported_before(s, on, exclude_id=eid)
            if not pending:
                break
            el = pending[0]
            with spinner(f"finishing {el.name} …"):
                if el.status == "scheduled":
                    simulate_election(s, el.id)
                run_instant_night(s, el.id)
            console.print(f"[dim]finished[/] {el.election_date} {el.name}")
            done += 1
    console.print(f"{data_badge('SIMULATED')} {done} earlier election(s) finished")
