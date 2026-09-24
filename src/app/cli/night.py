"""``election-night`` — a broadcast-style live election night in the terminal (rich).

The night runs through the election-night service (:class:`app.services.night.NightManager`), so
every call is persisted exactly as in the web UI and the night can be paused (Ctrl+C), resumed
later, or finished at once.  The screen shows the SIMULATED clock and reporting, the Electoral
College bar with its 88 TO WIN mark, the tickets' electoral and popular votes, the House (76 FOR
CONTROL) and Senate (13 FOR CONTROL) counters, and the feed of race calls.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import typer
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.cli._common import (
    console,
    data_badge,
    fmt_int,
    friendly,
    print_json,
    resolve_election,
    session_scope,
)
from app.core.errors import ElectionNightError

#: Wall clock and sleep used by the live loop (tests replace them with a fake clock).
_now: Callable[[], float] = time.monotonic
_sleep: Callable[[float], None] = time.sleep

_STATUS_ICON = {"ready": "■", "running": "▶", "paused": "⏸", "finished": "✔"}
_CALL_STYLE = {
    "CALLED": "bold green",
    "PROJECTED_WINNER": "green",
    "FINAL": "bold",
    "RECOUNT": "bold magenta",
    "TOO_CLOSE_TO_CALL": "yellow",
    "LEAN": "cyan",
}
_NEUTRAL = "grey50"


# =========================================================================== labels
def line_labels(state: dict[str, Any]) -> dict[str, dict[str, tuple[str, str | None, str | None]]]:
    """race → line key → (label, party, colour) from a ``detail='full'`` state."""
    out: dict[str, dict[str, tuple[str, str | None, str | None]]] = {}
    snap = state.get("snapshot") or {}
    for race in snap.get("races") or []:
        labels = race.get("line_labels") or {}
        parties = race.get("line_parties") or {}
        colors = race.get("line_colors") or {}
        out[race["key"]] = {k: (labels.get(k, k), parties.get(k), colors.get(k)) for k in labels}
    pres = snap.get("president") or {}
    if pres:
        out["PRES"] = {
            t["key"]: (t.get("label") or t["key"], t.get("party"), t.get("color")) for t in pres["tickets"]
        }
    return out


def _label(
    labels: dict[str, dict[str, tuple[str, str | None, str | None]]], race: str, key: str | None
) -> str:
    if key is None:
        return ""
    info = labels.get(race, {}).get(key) or labels.get("PRES", {}).get(key)
    if info is None:
        return key
    label, party, color = info
    text = f"{label} ({party})" if party else label
    return f"[{color}]{text}[/]" if color else text


# =========================================================================== rendering
def ev_bar(president: dict[str, Any], width: int = 64) -> Text:
    """The Electoral College bar: decided EV (solid), leading EV (shaded), uncalled (grey) and the
    majority mark."""
    total = max(int(president.get("ev_total") or 0), 1)
    needed = int(president.get("ev_needed") or 0)
    cells: list[tuple[str, str]] = []
    tickets = president.get("tickets") or []
    for t in tickets:
        n = round(width * int(t.get("ev_decided") or 0) / total)
        cells += [("█", t.get("color") or "white")] * n
    for t in tickets:
        n = round(width * int(t.get("ev_leading") or 0) / total)
        cells += [("▒", t.get("color") or "white")] * n
    cells = cells[:width]
    cells += [("·", _NEUTRAL)] * (width - len(cells))
    mark = min(max(round(width * needed / total), 0), width - 1)
    ch, style = cells[mark]
    cells[mark] = ("┃", f"bold white on {style}" if ch != "·" else "bold white")
    text = Text()
    for ch, style in cells:
        text.append(ch, style=style)
    return text


def _ticket_table(president: dict[str, Any], popular: dict[str, Any] | None) -> Table:
    t = Table(box=None, pad_edge=False, show_header=True, header_style="bold")
    for col, just in (
        ("ticket", "left"),
        ("EV", "right"),
        ("leading", "right"),
        ("votes", "right"),
        ("%", "right"),
    ):
        t.add_column(col, justify=just)  # type: ignore[arg-type]
    pv = {ln["key"]: ln for ln in (popular or {}).get("lines", [])}
    shown = 0
    for tk in president.get("tickets") or []:
        line = pv.get(tk["key"], {})
        if shown >= 6 and not tk.get("ev_decided"):
            continue
        shown += 1
        color = tk.get("color") or "white"
        name = f"[{color}]■[/] {tk.get('label') or tk['key']}" + (
            f" ({tk['party']})" if tk.get("party") else ""
        )
        t.add_row(
            name,
            f"[bold]{tk.get('ev_decided', 0)}[/]",
            f"+{tk.get('ev_leading', 0)}" if tk.get("ev_leading") else "",
            fmt_int(line.get("votes", 0)),
            f"{line.get('pct', 0.0):.1f}",
        )
    return t


def _chamber_line(name: str, chamber: dict[str, Any] | None, label: str) -> Text | None:
    if not chamber:
        return None
    text = Text.from_markup(f"[bold]{name}[/] [dim]{label}[/]  ")
    for row in (chamber.get("by_party") or [])[:6]:
        decided = row.get("called", 0) + row.get("holdover", 0)
        extra = row.get("leading", 0)
        if not decided and not extra:
            continue
        color = row.get("color") or "white"
        text.append_text(Text.from_markup(f"[{color}]■[/] {row['party']} [bold]{decided}[/]"))
        if extra:
            text.append(f" (+{extra})", style="dim")
        text.append("  ")
    text.append(f"uncalled {chamber.get('uncalled', 0)}", style="dim")
    control = chamber.get("control")
    text.append("   control: ")
    text.append(control or "—", style="bold green" if control else "dim")
    return text


def _feed(snapshot: dict[str, Any], labels: dict[str, Any], n: int = 8) -> Table:
    t = Table(box=None, pad_edge=False, show_header=False)
    t.add_column("time", style="dim")
    t.add_column("race")
    t.add_column("status")
    t.add_column("line")
    t.add_column("detail", style="dim")
    for rec in (snapshot.get("recent_calls") or [])[:n]:
        status = rec["status"]
        clock = (rec.get("timestamp") or "")[11:16]
        style = _CALL_STYLE.get(status, "")
        wp = rec.get("win_probability")
        detail = f"{rec.get('reporting_pct', 0):.0f}% reporting"
        if wp is not None and status not in ("FINAL", "RECOUNT"):
            detail += f" · p={wp:.3f}"
        if rec.get("is_manual"):
            detail += " · MANUAL"
        if rec.get("retracted"):
            detail += " · RETRACTED"
        t.add_row(
            clock,
            rec["race_key"],
            f"[{style}]{status}[/]" if style else status,
            _label(labels, rec["race_key"], rec.get("key")),
            detail,
        )
    return t


def render_broadcast(state: dict[str, Any], labels: dict[str, Any] | None = None) -> RenderableType:
    """The broadcast screen of a night state (:meth:`NightManager.state`)."""
    labels = labels or {}
    snap = state.get("snapshot") or {}
    clk = state.get("clock") or {}
    rep = snap.get("reporting") or {}
    status = clk.get("status", "ready")
    speed = clk.get("speed", 1)
    head = Text.from_markup(
        f"[bold]{clk.get('clock', '--:--')}[/]  {_STATUS_ICON.get(status, '')} {status}"
        f"{f' {speed:g}×' if status == 'running' else ''}  ·  "
        f"[bold]{rep.get('pct_expected_ballots', 0.0):.1f}%[/] reporting  ·  "
        f"{rep.get('municipalities_reporting', 0)}/{rep.get('municipalities_total', 0)} municipalities  ·  "
        f"event {clk.get('seq', 0)}/{clk.get('total_events', 0)}  ·  {data_badge('SIMULATED')}"
    )
    parts: list[RenderableType] = [head]
    labels_top = state.get("labels") or {}
    pres = snap.get("president")
    if pres:
        winner = pres.get("winner")
        status_line = Text.from_markup(
            f"[bold]PRESIDENT[/]  [bold yellow]{labels_top.get('president', '')}[/]  ·  "
            f"{pres.get('ev_decided_total', 0)} EV allocated · {pres.get('ev_uncalled', 0)} uncalled of "
            f"{pres.get('ev_total', 0)}"
        )
        if winner:
            status_line.append_text(
                Text.from_markup(f"  ·  [bold green]WINNER: {_label(labels, 'PRES', winner)}[/]")
            )
        elif pres.get("contingent_likely"):
            status_line.append_text(Text.from_markup("  ·  [bold magenta]contingent election likely[/]"))
        parts += [Text(""), status_line, ev_bar(pres), _ticket_table(pres, snap.get("popular_vote"))]
    for name, key, lab in (("HOUSE", "house", "house"), ("SENATE", "senate", "senate")):
        line = _chamber_line(name, snap.get(key), labels_top.get(lab, ""))
        if line is not None:
            parts += [Text(""), line]
    govs = snap.get("governors") or []
    if govs:
        called = sum(1 for g in govs if g.get("called_key"))
        parts += [Text.from_markup(f"[bold]GOVERNORS[/]  {called}/{len(govs)} called")]
    parts += [Text(""), Text.from_markup("[bold]CALLS[/]"), _feed(snap, labels)]
    title = f"NL FEDERAL ELECTION {state.get('year', '')} · ELECTION NIGHT"
    return Panel(
        Group(*parts), title=f"[bold]{title}[/]", subtitle=state.get("name") or "", border_style="blue"
    )


def summary_lines(state: dict[str, Any], labels: dict[str, Any] | None = None) -> list[str]:
    """Plain-text summary of a night state (headless output)."""
    labels = labels or {}
    snap = state.get("snapshot") or {}
    clk = state.get("clock") or {}
    rep = snap.get("reporting") or {}
    lines = [
        f"{clk.get('clock')} {clk.get('status')} · {rep.get('pct_expected_ballots', 0.0):.1f}% reporting · "
        f"event {clk.get('seq')}/{clk.get('total_events')} · election {state.get('election_status')}"
    ]
    pres = snap.get("president")
    if pres:
        ev = ", ".join(
            f"{t.get('party') or t['key']} {t['ev_decided']}"
            + (f"(+{t['ev_leading']})" if t.get("ev_leading") else "")
            for t in pres["tickets"]
            if t.get("ev_decided") or t.get("ev_leading")
        )
        winner = pres.get("winner")
        lines.append(
            f"President ({pres['ev_needed']} TO WIN of {pres['ev_total']}): {ev or 'no EV allocated'} · "
            f"uncalled {pres['ev_uncalled']}"
            + (f" · WINNER {_plain(labels, 'PRES', winner)}" if winner else "")
        )
    for name, key in (("House", "house"), ("Senate", "senate")):
        ch = snap.get(key)
        if ch:
            rows = ", ".join(
                f"{r['party']} {r.get('called', 0) + r.get('holdover', 0)}"
                for r in ch["by_party"]
                if r.get("called", 0) + r.get("holdover", 0)
            )
            lines.append(
                f"{name} ({ch['majority']} FOR CONTROL): {rows or 'none decided'} · "
                f"uncalled {ch['uncalled']} · control {ch.get('control') or '-'}"
            )
    return lines


def _plain(labels: dict[str, Any], race: str, key: str | None) -> str:
    if key is None:
        return ""
    info = labels.get(race, {}).get(key)
    if info is None:
        return key
    return f"{info[0]} ({info[1]})" if info[1] else info[0]


def _parse_until(until: str | None, polls_close: str) -> float | None:
    """Simulated seconds after polls closing of a local ``HH:MM`` (the night crosses midnight)."""
    if not until:
        return None
    try:
        hh, mm = (int(x) for x in until.split(":"))
        ch, cm = (int(x) for x in polls_close.split(":"))
    except ValueError as exc:
        raise ElectionNightError(f"--until must be HH:MM, got {until!r}") from exc
    return float(((hh * 60 + mm) - (ch * 60 + cm)) % 1440) * 60.0


# =========================================================================== command
@friendly
def election_night(
    election: str = typer.Option("demo", "--election", "-e", help="Election id, year, 'latest' or 'demo'."),
    speed: float = typer.Option(10.0, "--speed", help="Playback speed (1, 2, 5, 10 or 25; 1× ≈ 8.7 min)."),
    instant: bool = typer.Option(False, "--instant", help="Apply every reporting event at once."),
    headless: bool = typer.Option(False, "--headless", help="No live screen: print calls and a summary."),
    reset: bool = typer.Option(False, "--reset", help="Reset the night to polls closing first."),
    until: str | None = typer.Option(None, "--until", help="Pause at this local clock time (HH:MM)."),
    as_json: bool = typer.Option(False, "--json", help="Print the final night state as JSON."),
) -> None:
    """Run an election night (persisted: Ctrl+C pauses it; run the command again to resume)."""
    from app.services.night import get_night_manager

    with session_scope() as s:
        eid = resolve_election(s, election)
    manager = get_night_manager()
    if reset:
        manager.control(eid, "reset", now=_now())
        console.print(f"election night {eid} reset to polls closing")
    state = manager.state(eid, detail="full", now=_now())
    labels = line_labels(state)
    if state["election_status"] == "final":
        _final(state, labels, as_json, note="this election is final; its night is complete")
        return
    if instant:
        manager.instant_finish(eid)
        _final(manager.state(eid, detail="full", now=_now()), labels, as_json)
        return
    stop_at = _parse_until(until, state["clock"]["polls_close_local"])
    manager.control(eid, "start", speed=speed, now=_now())
    try:
        if headless:
            state = _run_headless(manager, eid, labels, stop_at)
        else:
            state = _run_live(manager, eid, labels, stop_at)
    except KeyboardInterrupt:
        state = manager.control(eid, "pause", now=_now())
        console.print(
            f"[yellow]paused at {state['clock']['clock']} (event {state['clock']['seq']}/"
            f"{state['clock']['total_events']}); run the command again to resume[/]"
        )
        return
    if state["election_status"] == "final":
        _final(manager.state(eid, detail="full", now=_now()), labels, as_json)
    else:
        console.print(
            f"paused at {state['clock']['clock']} (event {state['clock']['seq']}/{state['clock']['total_events']})"
        )
        if as_json:
            print_json(state)


def _step(manager: Any, eid: int, tick: float, stop_at: float | None) -> tuple[dict[str, Any], bool]:
    """One loop iteration: the state now, then sleep one screen tick.  With ``--until`` the night
    is paused exactly when the simulated clock reaches it (the pause is given the wall-clock time
    of that moment).  Returns ``(state, done)``."""
    t = _now()
    state = manager.state(eid, now=t)
    clk = state["clock"]
    if clk["status"] == "finished" or state["election_status"] == "final":
        return state, True
    rate = float(clk.get("speed") or 1.0) * float(clk.get("base_rate") or 60.0)
    if stop_at is not None and clk["status"] == "running" and rate > 0:
        remaining = (stop_at - float(clk["sim_time_s"])) / rate
        if remaining <= tick:
            _sleep(max(remaining, 0.0))
            return manager.control(eid, "pause", now=t + max(remaining, 0.0)), True
    _sleep(tick)
    return state, False


def _run_live(manager: Any, eid: int, labels: dict[str, Any], stop_at: float | None) -> dict[str, Any]:
    from rich.live import Live

    state = manager.state(eid, now=_now())
    with Live(
        render_broadcast(state, labels), console=console, refresh_per_second=4, transient=False
    ) as live:
        while True:
            state, done = _step(manager, eid, 0.25, stop_at)
            live.update(render_broadcast(state, labels))
            if done:
                return state


def _run_headless(manager: Any, eid: int, labels: dict[str, Any], stop_at: float | None) -> dict[str, Any]:
    seen: set[tuple[str, int, str]] = set()
    last_report = -1.0
    while True:
        state, done = _step(manager, eid, 0.5, stop_at)
        snap = state["snapshot"]
        for rec in reversed(snap.get("recent_calls") or []):
            key = (rec["race_key"], int(rec["seq"]), rec["status"])
            if key in seen:
                continue
            seen.add(key)
            console.print(
                f"{(rec.get('timestamp') or '')[11:16]}  {rec['race_key']:<14} {rec['status']:<18} "
                f"{_plain(labels, rec['race_key'], rec.get('key'))}  ({rec.get('reporting_pct', 0):.0f}% reporting)"
                + ("  MANUAL" if rec.get("is_manual") else "")
                + ("  RETRACTED" if rec.get("retracted") else ""),
                markup=False,
            )
        sim = float(state["clock"]["sim_time_s"])
        if done or sim - last_report >= 1800.0:
            last_report = sim
            for line in summary_lines(state, labels):
                console.print(f"  {line}", markup=False, style="dim")
        if done:
            return state


def _final(state: dict[str, Any], labels: dict[str, Any], as_json: bool, note: str | None = None) -> None:
    if as_json:
        snap = state.get("snapshot") or {}
        out = {k: v for k, v in state.items() if k != "snapshot"}
        out.update({k: snap.get(k) for k in ("president", "house", "senate", "governors", "reporting")})
        out["summary"] = summary_lines(state, labels)
        print_json(out)
        return
    if note:
        console.print(f"[dim]{note}[/]")
    console.print(render_broadcast(state, labels))
    for line in summary_lines(state, labels):
        console.print(line, markup=False)
