"""World clock commands (docs/CLOCK.md): ``clock`` shows today; ``clock next`` goes to the next
election day and asks whether to watch its night or count it; ``clock watch | count | skip |
agenda | news``."""

from __future__ import annotations

from datetime import date, timedelta

import typer

from app.cli._common import console, data_badge, friendly, print_json, session_scope, spinner, table

clock_app = typer.Typer(invoke_without_command=True, no_args_is_help=False)


def _status() -> dict:
    from app.services.read.clock import state

    with session_scope() as s:
        return state(s)


def _print_status(st: dict) -> None:
    console.print(f"{data_badge('FICTIONAL')} today is [bold]{st['today_label']}[/]")
    for e in st["elections_today"]:
        where = f" · {', '.join(e['provinces'])}" if e.get("provinces") else ""
        console.print(f"  election day: {e['name']} (id {e['id']}){where} — [bold]{e['status']}[/]")
    if st["pending"]:
        console.print("  → watch it (`python -m app clock watch`) or count it (`python -m app clock count`)")
    nxt = st.get("next")
    if nxt:
        where = f" · {', '.join(nxt['provinces'])}" if nxt.get("provinces") else ""
        races = f" · {nxt['races']} races" if nxt.get("races") else ""
        console.print(f"  next election day: {nxt['label']} — {nxt['name']}{where}{races}")


def _print_news(items: list[dict], title: str) -> None:
    if not items:
        return
    console.print(f"[bold]{title}[/]")
    for n in items:
        mark = {"person": "★", "result": "■", "recall": "!", "vacancy": "·"}.get(n["kind"], "·")
        console.print(f"  {n['date']} {mark} {n['text']}")


@clock_app.callback()
@friendly
def clock_cmd(
    ctx: typer.Context,
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Show today, today's elections and the next election day."""
    if ctx.invoked_subcommand is not None:
        return
    st = _status()
    if as_json:
        print_json(st)
        return
    _print_status(st)


def _count() -> list[int]:
    from app.services import clock

    def progress(name: str, i: int, n: int) -> None:
        console.print(f"[dim]counting[/] {name} ({i + 1}/{n}) …")

    with session_scope() as s, spinner("counting …"):
        return clock.count_today(s, commit=s.commit, progress=progress)


def _watch(speed: float) -> None:
    from app.cli.night import election_night
    from app.services import clock

    with session_scope() as s:
        eid = clock.prepare_today(s).id
    election_night(
        election=str(eid), speed=speed, instant=False, headless=False, reset=False, until=None, as_json=False
    )


@clock_app.command("next")
@friendly
def next_cmd(
    watch: bool = typer.Option(False, "--watch", help="Watch the election night right away."),
    count: bool = typer.Option(False, "--count", help="Count the election instantly."),
    speed: float = typer.Option(25.0, "--speed", help="Night playback speed with --watch."),
) -> None:
    """Go to the next election day, then watch its night or count it (asks when neither is given)."""
    from app.services import clock

    with session_scope() as s, spinner("going to the next election day …"):
        out = clock.advance(s)
    _print_news(out["news"], f"since {out['previous']}:")
    st = _status()
    _print_status(st)
    choice = "w" if watch else "c" if count else None
    if choice is None and console.is_terminal:
        choice = typer.prompt(
            "Watch the night (w), count it instantly (c) or decide later (l)?", default="w"
        )[:1]
    if choice == "w":
        _watch(speed)
    elif choice == "c":
        done = _count()
        console.print(f"{data_badge('SIMULATED')} counted {len(done)} election(s)")
        with session_scope() as s:
            _print_news(clock.news(s, date.fromisoformat(out["previous"]), clock.today(s)), "results:")


@clock_app.command("watch")
@friendly
def watch_cmd(
    speed: float = typer.Option(25.0, "--speed", help="Playback speed (1, 2, 5, 10 or 25)."),
) -> None:
    """Watch today's election night in the terminal."""
    _watch(speed)


@clock_app.command("count")
@friendly
def count_cmd() -> None:
    """Count today's election instantly (every race call is stored)."""
    done = _count()
    console.print(f"{data_badge('SIMULATED')} counted {len(done)} election(s)")


@clock_app.command("skip")
@friendly
def skip_cmd(to: str = typer.Option(..., "--to", help="Skip to this date (YYYY-MM-DD).")) -> None:
    """Count every election day before a date instantly and move the clock there."""
    from app.services import clock

    target = date.fromisoformat(to)

    def progress(name: str, done: int, total: int) -> None:
        console.print(f"[dim]{done + 1}/{max(total, done + 1)}[/] {name}")

    with session_scope() as s:
        out = clock.skip_to(s, target, commit=s.commit, progress=progress)
    console.print(
        f"{data_badge('SIMULATED')} counted {len(out['counted'])} election(s); today is {out['today']}"
    )
    _print_news(out["news"], "what happened:")


@clock_app.command("agenda")
@friendly
def agenda_cmd(
    months: int = typer.Option(12, "--months", min=1, max=36, help="How far ahead."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """The coming election days (and the recent ones)."""
    from app.services.read.clock import agenda

    with session_scope() as s:
        from app.services import clock

        t = clock.today(s)
        data = agenda(s, start=t - timedelta(days=60), end=t + timedelta(days=31 * months))
    if as_json:
        print_json(data)
        return
    body = [
        (
            d["date"],
            d["when"],
            d["name"],
            ", ".join(d["provinces"]) if d["provinces"] else "",
            d["races"] or "",
            d["status"] or "planned",
        )
        for d in data["days"]
    ]
    console.print(
        table(
            f"{data_badge('FICTIONAL')} election days · today {data['today']}",
            ["date", "", "election", "provinces", ("races", "right"), "status"],
            body,
        )
    )


@clock_app.command("news")
@friendly
def news_cmd(days: int = typer.Option(180, "--days", min=1, help="How far back.")) -> None:
    """What happened lately: vacancies, recalls, results and your own people."""
    from app.services.read.clock import news

    with session_scope() as s:
        from app.services import clock

        t = clock.today(s)
        data = news(s, since=t - timedelta(days=days), until=t)
    if not data["items"]:
        console.print("no news")
        return
    _print_news(list(reversed(data["items"])), f"news {data['since']} – {data['until']}:")
