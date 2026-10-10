"""``people`` — check config/people.yaml and see where your own people ran and what they hold
(docs/PEOPLE.md)."""

from __future__ import annotations

import typer

from app.cli._common import EXIT_CONFLICT, console, data_badge, friendly, print_json, session_scope, table

_CHANCE_WORD = {0.0: "never", 0.15: "rarely", 0.35: "sometimes", 0.65: "often", 1.0: "always"}


@friendly
def people(
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
    runs: bool = typer.Option(True, "--runs/--no-runs", help="List every race each person ran in."),
) -> None:
    """Your own people (config/people.yaml): home, party, chance, races run and offices held."""
    from app.services.read.people import custom_people

    with session_scope() as s:
        data = custom_people(s)
    if as_json:
        print_json(data)
        if data["error"]:
            raise typer.Exit(EXIT_CONFLICT)
        return
    if data["error"]:
        console.print(f"[red]config/people.yaml has a problem:[/] {data['error']}")
        raise typer.Exit(EXIT_CONFLICT)
    if not data["people"]:
        console.print(f"No custom people yet. Add them to {data['file']} (see docs/PEOPLE.md).")
        return
    body = []
    for p in data["people"]:
        party = p["party"] or "—"
        if not p["party_known"]:
            party += " (unknown party!)"
        won = sum(1 for r in p["runs"] if r["won"])
        body.append(
            (
                p["name"],
                f"{p['home_name']} ({p['province']})",
                (p["born"] or "")[:4],
                party,
                _CHANCE_WORD.get(round(p["chance"], 2), f"{p['chance']:.2f}"),
                ", ".join(p["offices"]) if p["offices"] else "any",
                f"{len(p['runs'])} ({won} won)",
                ", ".join(h["name"] for h in p["holds"]) or "—",
            )
        )
    console.print(
        table(
            f"{data_badge('FICTIONAL')} your people ({data['count']}) · {data['file']}",
            ["name", "home", "born", "party", "chance", "offices", "races", "holds"],
            body,
        )
    )
    if runs:
        for p in data["people"]:
            for r in p["runs"]:
                result = {True: "[green]won[/]", False: "lost"}.get(r["won"], "[dim]on the ballot[/]")
                console.print(
                    f"  {p['name']}: {r['date']} {r['race']} ({r['party'] or 'nonpartisan'}) — {result}"
                )
