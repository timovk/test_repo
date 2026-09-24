"""``geography download | build | load`` — the REAL CBS/PDOK geography (provinces, 342
municipalities, 14,729 buurten used as precincts)."""

from __future__ import annotations

import typer

from app.cli._common import console, data_badge, fmt_int, friendly, session_scope, spinner, table

geography_app = typer.Typer(no_args_is_help=True)

YEAR_HELP = "Geography vintage (CBS 'Wijk- en Buurtkaart' year; default from settings, 2025)."


@geography_app.command("download")
@friendly
def download(
    year: int | None = typer.Option(None, "--year", help=YEAR_HELP),
    force: bool = typer.Option(False, "--force", help="Download again even when the files exist."),
) -> None:
    """Download the REAL source datasets (CBS/PDOK) into data/raw (existing files are reused)."""
    from app.geography.download import download_all

    with spinner("downloading REAL geography sources …"):
        records = download_all(year, force=force)
    rows = [
        (r.key, r.publisher, fmt_int(r.size_bytes), r.sha256[:12], "downloaded" if r.downloaded else "reused")
        for r in records
    ]
    console.print(
        table(
            f"{data_badge('REAL')} sources",
            ["source", "publisher", ("bytes", "right"), "sha256", "status"],
            rows,
        )
    )


@geography_app.command("build")
@friendly
def build(
    year: int | None = typer.Option(None, "--year", help=YEAR_HELP),
    force: bool = typer.Option(False, "--force", help="Rebuild even when the store is up to date."),
) -> None:
    """Build the processed geography store (GeoParquet, adjacency, web GeoJSON)."""
    from app.geography.build import build_geography

    with spinner("building the processed geography store …"):
        report = build_geography(year, force=force)
    counts = report.counts or {}
    console.print(
        f"{data_badge('REAL')} store {report.year} at [bold]{report.path}[/]"
        f"{' (up to date, not rebuilt)' if report.skipped else ''}"
    )
    rows = [(k, fmt_int(v) if isinstance(v, int) else v) for k, v in sorted(counts.items())]
    if rows:
        console.print(table(None, ["item", ("count", "right")], rows))
    for w in report.warnings[:10]:
        console.print(f"[yellow]warning:[/] {w}")


@geography_app.command("load")
@friendly
def load(
    year: int | None = typer.Option(None, "--year", help=YEAR_HELP),
    force: bool = typer.Option(False, "--force", help="Reload the vintage even when it is up to date."),
) -> None:
    """Load the processed store into the database (provinces, municipalities, units)."""
    from app.geography.loader_db import load_into_db
    from app.services.bootstrap import load_geography

    with session_scope() as s, spinner("loading the geography into the database …"):
        if force:
            load_into_db(s, year, force=True)
        vintage = load_geography(s, year)
        info = (
            vintage.year,
            vintage.province_count,
            vintage.municipality_count,
            vintage.unit_count,
            vintage.population_total,
        )
    console.print(
        f"{data_badge('REAL')} vintage {info[0]}: {info[1]} provinces, {fmt_int(info[2])} municipalities, "
        f"{fmt_int(info[3])} units (buurten), population {fmt_int(info[4])}"
    )
