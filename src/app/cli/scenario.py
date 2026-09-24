"""``scenario list | show | export | import | duplicate | validate`` — FICTIONAL scenario
documents (parties, candidates, tickets, environment, seeds).

Built-in scenarios are ``config/scenarios/*.yaml``; ``import`` and ``duplicate`` write user
scenarios to ``config/scenarios/user/<slug>.yaml`` — the directory of the API's scenario editor —
so scenarios made in the UI and in the CLI are the same documents (a user scenario shadows a
built-in one with the same slug)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from app.cli._common import (
    EXIT_INVALID,
    console,
    data_badge,
    friendly,
    print_json,
    table,
)
from app.core.errors import ScenarioError
from app.scenarios.schema import ScenarioDocument

scenario_app = typer.Typer(no_args_is_help=True)


def load_any(scenario: str | Path) -> ScenarioDocument:
    """A user scenario, a built-in scenario (by slug) or a YAML file."""
    from app.core.errors import NotFoundError
    from app.services.read.scenarios import load_document

    try:
        return load_document(scenario)[1]
    except NotFoundError as exc:
        raise ScenarioError(str(exc)) from None


def _user_infos() -> list[Any]:
    from app.scenarios.loader import list_scenarios
    from app.services.read.scenarios import default_user_dir

    d = default_user_dir()
    return list_scenarios(d) if d.exists() else []


@scenario_app.command("list")
@friendly
def list_cmd(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """List the built-in and user scenario files (invalid ones with their error)."""
    from app.scenarios.loader import list_scenarios

    items = [("builtin", i) for i in list_scenarios()] + [("user", i) for i in _user_infos()]
    if as_json:
        print_json(
            [
                {
                    "slug": i.slug,
                    "name": i.name,
                    "year": i.year,
                    "election_type": i.election_type,
                    "seed": i.seed,
                    "source": source,
                    "path": str(i.path),
                    "valid": i.valid,
                    "error": i.error,
                }
                for source, i in items
            ]
        )
        return
    rows = [
        (
            i.slug,
            i.year,
            i.election_type,
            i.seed,
            i.name,
            source,
            "yes" if i.valid else f"[red]no[/] {i.error}",
        )
        for source, i in items
    ]
    console.print(
        table(
            f"{data_badge('FICTIONAL')} scenarios",
            ["slug", "year", "type", ("seed", "right"), "name", "source", "valid"],
            rows,
        )
    )


@scenario_app.command("show")
@friendly
def show(
    scenario: str = typer.Argument(..., help="Scenario slug or YAML file."),
    as_yaml: bool = typer.Option(False, "--yaml", help="Print the full merged document as YAML."),
) -> None:
    """Show a scenario: parties, tickets and the political environment."""
    from app.scenarios.loader import dump_scenario

    doc = load_any(scenario)
    if as_yaml:
        console.print(dump_scenario(doc), markup=False, highlight=False)
        return
    m = doc.scenario
    console.print(
        f"{data_badge('FICTIONAL')} [bold]{m.name}[/] ({m.slug}) · {m.election_type} {m.year} · seed {m.seed}"
    )
    if m.description:
        console.print(m.description, markup=False)
    rows = [
        (f"[{p.color}]■[/] {p.code}", p.name, f"{100 * p.base_share:.1f}%", p.family or "")
        for p in sorted(doc.parties, key=lambda p: -p.base_share)
    ]
    console.print(table("parties", ["party", "name", ("base share", "right"), "family"], rows))
    names = {c.key: f"{c.first_name} {c.last_name}" for c in doc.candidates}
    tickets = [
        (
            t.party or "independent",
            names.get(t.president, t.president),
            names.get(t.vice_president, t.vice_president),
            "incumbent" if t.incumbent else ("withdrawn" if t.withdrawn else ""),
        )
        for t in doc.president.tickets
    ]
    if tickets:
        console.print(table("presidential tickets", ["party", "President", "Vice-President", ""], tickets))
    env = doc.environment
    swing = ", ".join(f"{k} {v:+.3f}" for k, v in sorted(env.national.items()))
    console.print(
        f"environment: turnout base {100 * env.turnout_base:.0f}%"
        + (f", national shifts {swing}" if swing else "")
        + (f", president's party {env.president_party}" if env.president_party else "")
    )


@scenario_app.command("export")
@friendly
def export(
    scenario: str = typer.Argument(..., help="Scenario slug or YAML file."),
    out: Path | None = typer.Option(None, "--out", "-o", help="Output file (default: stdout)."),
) -> None:
    """Export the fully merged scenario document as YAML."""
    from app.scenarios.loader import dump_scenario, save_scenario

    doc = load_any(scenario)
    if out is None:
        typer.echo(dump_scenario(doc), nl=False)
        return
    path = save_scenario(doc, out)
    console.print(f"scenario {doc.scenario.slug} written to {path}")


def _slug_taken(slug: str) -> bool:
    from app.services.read.scenarios import slug_exists

    return slug_exists(slug)


@scenario_app.command("import")
@friendly
def import_cmd(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="Scenario YAML file to import."),
    slug: str | None = typer.Option(None, "--slug", help="Store under this slug (default: the document's)."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing scenario with that slug."),
) -> None:
    """Validate a scenario file and add it as a user scenario (config/scenarios/user)."""
    from app.scenarios.loader import duplicate_scenario, load_scenario_text
    from app.services.read.scenarios import write_user_file

    doc = load_scenario_text(path.read_text(encoding="utf-8"), base_dir=path.parent)
    if slug and slug != doc.scenario.slug:
        doc = duplicate_scenario(doc, slug, doc.scenario.seed, name=doc.scenario.name)
    if _slug_taken(doc.scenario.slug) and not force:
        raise ScenarioError(f"a scenario {doc.scenario.slug!r} already exists (use --force or --slug)")
    target = write_user_file(doc)
    console.print(f"imported scenario {doc.scenario.slug} → {target}")


@scenario_app.command("duplicate")
@friendly
def duplicate(
    scenario: str = typer.Argument(..., help="Scenario to copy (slug or file)."),
    new_slug: str = typer.Argument(..., help="Slug of the copy."),
    seed: int | None = typer.Option(None, "--seed", help="Seed of the copy (default: derived)."),
    name: str | None = typer.Option(None, "--name", help="Name of the copy."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing scenario with that slug."),
) -> None:
    """Copy a scenario under a new slug and seed (the political geography seed is kept) as a user
    scenario (config/scenarios/user, editable in the UI)."""
    from app.scenarios.loader import duplicate_scenario
    from app.services.read.scenarios import write_user_file

    doc = duplicate_scenario(load_any(scenario), new_slug, seed, name=name)
    if _slug_taken(new_slug) and not force:
        raise ScenarioError(f"a scenario {new_slug!r} already exists (use --force)")
    target = write_user_file(doc)
    console.print(f"scenario {new_slug} (seed {doc.scenario.seed}) → {target}")


@scenario_app.command("validate")
@friendly
def validate(
    scenario: str = typer.Argument(..., help="Scenario slug or YAML file."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Validate a scenario: schema, then its references against the REAL geography (when built)."""
    from app.geography.store import is_prepared, load_frame
    from app.scenarios.loader import validate_scenario

    doc = load_any(scenario)
    checked_geography = is_prepared()
    problems = validate_scenario(doc, load_frame()) if checked_geography else []
    if as_json:
        print_json(
            {
                "slug": doc.scenario.slug,
                "schema_valid": True,
                "geography_checked": checked_geography,
                "problems": problems,
                "ok": not problems,
            }
        )
    else:
        console.print(f"scenario {doc.scenario.slug}: schema valid")
        if not checked_geography:
            console.print("[yellow]REAL geography not built: references to places were not checked[/]")
        for p in problems:
            console.print(f"[red]problem:[/] {p}", markup=True)
        if checked_geography and not problems:
            console.print("every reference fits the REAL geography")
    if problems:
        raise typer.Exit(EXIT_INVALID)
