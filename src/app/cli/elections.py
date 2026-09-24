"""Election commands: ``election create | list | show``, ``simulate``, ``finalize``,
``forecast``, ``polls``, ``history`` and ``export``.

Every election is a SIMULATED contest between FICTIONAL parties and candidates.  An election is
addressed by id, by year (its most recent election), ``latest`` or ``demo``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import typer
from sqlalchemy import select

from app.cli._common import (
    console,
    data_badge,
    err_console,
    fmt_int,
    fmt_pct,
    friendly,
    print_json,
    resolve_election,
    session_scope,
    spinner,
    table,
)
from app.core.errors import NotFoundError, ScenarioError

election_app = typer.Typer(no_args_is_help=True)

ELECTION_HELP = "Election id, year, 'latest' or 'demo'."


# =========================================================================== helpers
def _scenario_for_year(year: int) -> str:
    """The slug of the (single valid) scenario file of ``year``."""
    from app.scenarios.loader import list_scenarios

    found = [s for s in list_scenarios() if s.valid and s.year == int(year)]
    if not found:
        raise ScenarioError(f"no scenario for {year} in config/scenarios (pass --scenario)")
    if len(found) > 1:
        raise ScenarioError(
            f"several scenarios for {year}: {', '.join(s.slug for s in found)} (pass --scenario)"
        )
    return found[0].slug


def _synthetic_geography(session: Any) -> bool:
    from app.services.runtime import geography_source

    try:
        return geography_source(session).kind == "synthetic"
    except Exception:  # no geography: strict creation reports the real problem
        return False


def _print_results(summary: dict[str, Any]) -> None:
    """Final results of an election summary (President, chambers, governors)."""
    res = summary.get("results") or {}
    pres = res.get("president")
    if pres:
        ev = sorted((pres.get("electoral_votes") or {}).items(), key=lambda kv: -kv[1])
        winner = pres.get("winner_name") or "no winner (contingent deadlock)"
        console.print(
            f"[bold]President[/]: {winner} ({pres.get('winner_party') or '-'}) · "
            f"decided by {pres.get('decided_by') or '-'} · "
            + ", ".join(f"{k} {v}" for k, v in ev if v)
            + f" · {pres.get('majority')} TO WIN"
        )
        if pres.get("contingent"):
            c = pres["contingent"]
            console.print(f"  contingent election: {c.get('mode')} → {c.get('outcome')}")
    for chamber in ("house", "senate"):
        c = res.get(chamber)
        if c:
            comp = ", ".join(f"{p} {n}" for p, n in sorted(c["composition"].items(), key=lambda kv: -kv[1]))
            ctl = c.get("controlling_party") or "no majority"
            console.print(
                f"[bold]{chamber.capitalize()}[/]: {comp} · control: {ctl} ({c['majority']} FOR CONTROL)"
            )
    if res.get("governors"):
        govs = ", ".join(f"{pv} {p or '-'}" for pv, p in res["governors"].items())
        console.print(f"[bold]Governors[/]: {govs}")
    if res.get("turnout_pct") is not None:
        console.print(f"turnout {fmt_pct(res['turnout_pct'])} · flips {res.get('flips', 0)}")


def _print_summary(summary: dict[str, Any]) -> None:
    c = summary.get("contents", {})
    counts = ", ".join(f"{k} {v}" for k, v in (c.get("race_counts") or {}).items())
    scen = summary.get("scenario") or {}
    console.print(
        f"{data_badge('SIMULATED')} election [bold]{summary['id']}[/] · {summary['name']} · "
        f"{summary['election_type']} {summary['election_date']} · status [bold]{summary['status']}[/] · "
        f"seed {summary['seed']} · scenario {scen.get('slug', '-')}"
    )
    if counts:
        console.print(f"races: {counts}")


# =========================================================================== election
@election_app.command("create")
@friendly
def create(
    year: int | None = typer.Option(None, "--year", help="Election year (picks that year's scenario)."),
    scenario: str | None = typer.Option(None, "--scenario", help="Scenario slug or YAML file."),
    seed: int | None = typer.Option(None, "--seed", help="Election seed (default: the scenario's)."),
    simulate_now: bool = typer.Option(False, "--simulate", help="Also simulate the (hidden) result."),
    lenient: bool = typer.Option(
        False, "--lenient", help="Ignore scenario references to places missing from the geography."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Create a SCHEDULED election from a scenario (``--year`` alone picks that year's scenario)."""
    from app.services.elections import create_election, election_summary, simulate_election

    if scenario is None and year is None:
        raise ScenarioError("give --year and/or --scenario")
    slug = scenario or _scenario_for_year(int(year))  # type: ignore[arg-type]
    with session_scope() as s:
        strict = not (lenient or _synthetic_geography(s))
        with spinner(f"creating election from {slug} …"):
            el = create_election(s, slug, seed=seed, year=year, strict=strict)
        eid = el.id
    if simulate_now:
        with session_scope() as s, spinner("simulating …"):
            simulate_election(s, eid)
    with session_scope() as s:
        summary = election_summary(s, eid)
    if as_json:
        print_json(summary)
    else:
        _print_summary(summary)


@election_app.command("list")
@friendly
def list_cmd(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """List every election (chronological)."""
    from app.services.elections import list_elections

    with session_scope() as s:
        rows = list_elections(s)
    if as_json:
        print_json(rows)
        return
    body = [
        (
            r["id"],
            r["year"],
            r["election_type"],
            r["election_date"],
            r["status"],
            r["seed"],
            (r.get("scenario") or {}).get("slug", ""),
            sum((r["contents"].get("race_counts") or {}).values()),
        )
        for r in rows
    ]
    console.print(
        table(
            f"{data_badge('SIMULATED')} elections",
            [
                ("id", "right"),
                "year",
                "type",
                "date",
                "status",
                ("seed", "right"),
                "scenario",
                ("races", "right"),
            ],
            body,
        )
    )


@election_app.command("show")
@friendly
def show(
    election: str = typer.Option("latest", "--election", "-e", help=ELECTION_HELP),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Show one election (results only once it is FINAL)."""
    from app.services.elections import election_summary

    with session_scope() as s:
        summary = election_summary(s, resolve_election(s, election))
    if as_json:
        print_json(summary)
        return
    _print_summary(summary)
    if summary.get("reported"):
        _print_results(summary)
    else:
        console.print("[dim]results hidden until the election is final (election night or `finalize`)[/]")


# =========================================================================== lifecycle
@friendly
def simulate(
    election: str = typer.Option(..., "--election", "-e", help=ELECTION_HELP),
    seed: int | None = typer.Option(None, "--seed", help="Replace the election seed."),
) -> None:
    """Simulate an election's complete (hidden) result and its election-night timeline."""
    from app.services.elections import simulate_election
    from app.services.runtime import timeline_meta

    with session_scope() as s:
        eid = resolve_election(s, election)
        with spinner(f"simulating election {eid} …"):
            el = simulate_election(s, eid, seed=seed)
        meta = timeline_meta(s, eid)
        info = (el.id, el.year, int(el.seed), el.status)
    console.print(
        f"{data_badge('SIMULATED')} election {info[0]} ({info[1]}) simulated with seed {info[2]} · "
        f"status {info[3]} · night of {meta.get('events', '?')} reporting events "
        f"(polls close {str(meta.get('reference_close', ''))[11:16]}, last report {meta.get('end_clock', '?')})"
    )
    console.print("[dim]the result stays hidden until the election night (`election-night`) or `finalize`[/]")


@friendly
def finalize(
    election: str = typer.Option(..., "--election", "-e", help=ELECTION_HELP),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Finalize an election without an election night (tabulation, recounts, Electoral College)."""
    from app.services.elections import election_summary, finalize_election

    with session_scope() as s:
        eid = resolve_election(s, election)
        with spinner(f"finalizing election {eid} …"):
            finalize_election(s, eid)
    with session_scope() as s:
        summary = election_summary(s, eid)
    if as_json:
        print_json(summary)
        return
    _print_summary(summary)
    _print_results(summary)


@friendly
def forecast(
    election: str = typer.Option(..., "--election", "-e", help=ELECTION_HELP),
    simulations: int = typer.Option(10000, "--simulations", "-n", min=1, help="Monte Carlo draws."),
    seed: int = typer.Option(42, "--seed", help="Seed of the forecast."),
    workers: int = typer.Option(0, "--workers", help="Worker processes (0 = automatic, 1 = in-process)."),
    as_json: bool = typer.Option(False, "--json", help="Print the stored forecast as JSON."),
) -> None:
    """Run and store a Monte Carlo forecast (SIMULATED model estimates, not predictions)."""
    try:
        from app.services.forecast_runner import run_forecast_for_election
    except ImportError as exc:
        raise NotFoundError(f"the forecast service is not installed ({exc})") from exc
    from app.forecasting.service import load_forecast

    with session_scope() as s:
        eid = resolve_election(s, election)
        with spinner(f"forecasting election {eid} ({simulations:,} simulations) …"):
            run = run_forecast_for_election(s, eid, int(simulations), int(seed), workers=int(workers))
        run_id = int(run.id)
    with session_scope() as s:
        data = load_forecast(s, run_id)
    if as_json:
        print_json({k: v for k, v in data.items() if k not in ("distributions", "race_summaries")})
        return
    _print_forecast(data)


def _print_forecast(data: dict[str, Any]) -> None:
    result = data.get("result") or {}
    console.print(
        f"{data_badge('SIMULATED')} forecast run {data['run_id']} · election {data['election_id']} · "
        f"{fmt_int(data.get('n_simulations'))} simulations · seed {data.get('seed')} · "
        f"{data.get('duration_s') or 0:.1f}s"
    )
    pres = result.get("president")
    if pres:
        rows = [
            (
                t["label"],
                t.get("party_code") or "",
                fmt_pct(100 * (t.get("prob_majority") or 0)),
                f"{t['ev']['median']:.0f} ({t['ev']['p05']:.0f}–{t['ev']['p95']:.0f})",
                fmt_pct(100 * (t["pv_share"]["mean"] or 0)),
            )
            for t in sorted(pres["tickets"], key=lambda t: -(t.get("prob_majority") or 0))
        ]
        console.print(
            table(
                f"President — {pres['majority']} TO WIN of {pres['total_ev']}",
                [
                    "ticket",
                    "party",
                    ("P(majority)", "right"),
                    ("EV median (90%)", "right"),
                    ("PV mean", "right"),
                ],
                rows,
            )
        )
        console.print(f"P(contingent election) {fmt_pct(100 * (pres.get('prob_contingent') or 0))}")
    for chamber in ("house", "senate"):
        c = result.get(chamber)
        if not c:
            continue
        parties = sorted(c["parties"].values(), key=lambda p: -(p["seats"]["mean"] or 0))[:6]
        rows = [
            (
                p["key"],
                f"{p['seats']['median']:.0f} ({p['seats']['p05']:.0f}–{p['seats']['p95']:.0f})",
                fmt_pct(100 * (p.get("prob_majority") or 0)),
            )
            for p in parties
        ]
        console.print(
            table(
                f"{chamber.capitalize()} — {c.get('majority')} FOR CONTROL",
                ["party", ("seats median (90%)", "right"), ("P(majority)", "right")],
                rows,
            )
        )
    console.print(f"[dim]{data.get('disclaimer', '')}[/]")


# =========================================================================== polls / history
@friendly
def polls(
    election: str = typer.Option(..., "--election", "-e", help=ELECTION_HELP),
    poll_type: str | None = typer.Option(None, "--type", help="Poll type (e.g. president, generic_house)."),
    geo: str = typer.Option("NL", "--geo", help="Geography of the averages (NL, a province or a district)."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """FICTIONAL polls of an election and their averages (as of the day before the election)."""
    from datetime import timedelta

    from app.models import Election
    from app.polling.aggregate import aggregate_polls
    from app.polling.service import load_pollster_configs, polls_frames

    with session_scope() as s:
        eid = resolve_election(s, election)
        el = s.get(Election, eid)
        assert el is not None
        frame, results = polls_frames(s, eid, poll_type)
        pollsters = load_pollster_configs(s) if len(frame) else []
        election_date: date = el.election_date
    if len(frame) == 0:
        console.print(f"no polls for election {eid}" + (f" of type {poll_type}" if poll_type else ""))
        return
    as_of = election_date - timedelta(days=1)
    averages = aggregate_polls(frame, results, None, as_of, pollsters=pollsters)
    wanted = {k: v for k, v in averages.items() if v.geo_code == geo.upper()}
    counts = frame.groupby(["poll_type", "geo_code"]).size().rename("n").reset_index()
    if as_json:
        print_json(
            {
                "election_id": eid,
                "as_of": as_of.isoformat(),
                "polls": len(frame),
                "counts": counts.to_dict(orient="records"),
                "averages": [a.to_dict(include_polls=False, include_trend=False) for a in wanted.values()],
                "data_category": "SIMULATED",
            }
        )
        return
    by_type = counts.groupby("poll_type")["n"].sum().to_dict()
    console.print(
        f"{data_badge('SIMULATED')} {len(frame)} FICTIONAL polls of election {eid}: "
        + ", ".join(f"{t} {n}" for t, n in sorted(by_type.items()))
    )
    if not wanted:
        console.print(f"no averages for geography {geo.upper()}")
    for (ptype, g), avg in sorted(wanted.items(), key=lambda kv: kv[0]):
        rows = [
            (k, f"{avg.mean[k]:.1f}", f"{avg.lower[k]:.1f}–{avg.upper[k]:.1f}")
            for k in sorted(avg.keys, key=lambda k: -avg.mean[k])
        ]
        console.print(
            table(
                f"{ptype} · {g} · {avg.n_polls} polls · as of {avg.as_of.isoformat()}",
                ["option", ("average %", "right"), ("interval", "right")],
                rows,
            )
        )


@friendly
def history(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """Every reported election: President (EV, popular vote), House and Senate control."""
    from app.core.constitution import RaceType
    from app.models import Election
    from app.services._common import REPORTED_STATUSES
    from app.services.elections import election_summary
    from app.services.results import results_frame

    out: list[dict[str, Any]] = []
    with session_scope() as s:
        ids = s.scalars(
            select(Election.id)
            .where(Election.status.in_(list(REPORTED_STATUSES)))
            .order_by(Election.election_date, Election.id)
        ).all()
        for eid in ids:
            summ = election_summary(s, int(eid))
            res = summ.get("results") or {}
            pres = res.get("president") or {}
            pv = None
            if pres.get("winner"):
                frame = results_frame(s, [int(eid)], levels=("national",), race_types=[RaceType.PRESIDENT])
                row = frame[frame["line_key"] == pres["winner"]]
                pv = None if row.empty else float(row["share"].iloc[0]) * 100.0
            ev = pres.get("electoral_votes") or {}
            out.append(
                {
                    "election_id": int(eid),
                    "year": summ["year"],
                    "type": summ["election_type"],
                    "president": pres.get("winner_name"),
                    "president_party": pres.get("winner_party"),
                    "electoral_votes": ev.get(pres.get("winner"), None) if pres else None,
                    "popular_vote_pct": pv,
                    "decided_by": pres.get("decided_by"),
                    "house_control": (res.get("house") or {}).get("controlling_party"),
                    "house_seats": (res.get("house") or {}).get("composition"),
                    "senate_control": (res.get("senate") or {}).get("controlling_party"),
                    "senate_seats": (res.get("senate") or {}).get("composition"),
                    "turnout_pct": res.get("turnout_pct"),
                }
            )
    if as_json:
        print_json(out)
        return
    if not out:
        console.print("no reported elections yet (run `python -m app demo`)")
        return

    def seats(comp: dict[str, int] | None, party: str | None) -> str:
        if not comp:
            return ""
        lead = party or max(comp, key=lambda p: comp[p])
        return f"{party or 'none'} ({lead} {comp.get(lead, 0)})"

    rows = [
        (
            r["year"],
            r["type"],
            f"{r['president']} ({r['president_party']})" if r["president"] else "",
            r["electoral_votes"] if r["electoral_votes"] is not None else "",
            fmt_pct(r["popular_vote_pct"]),
            seats(r["house_seats"], r["house_control"]),
            seats(r["senate_seats"], r["senate_control"]),
            fmt_pct(r["turnout_pct"]),
        )
        for r in out
    ]
    console.print(
        table(
            f"{data_badge('SIMULATED')} election history",
            [
                "year",
                "type",
                "President",
                ("EV", "right"),
                ("PV", "right"),
                "House control",
                "Senate control",
                ("turnout", "right"),
            ],
            rows,
        )
    )


# =========================================================================== export
@friendly
def export(
    election: str = typer.Option(..., "--election", "-e", help=ELECTION_HELP),
    dataset: str = typer.Option("national", "--dataset", "-d", help="Dataset name, or 'all'."),
    fmt: str = typer.Option("csv", "--format", "-f", help="csv, json or both."),
    out: Path = typer.Option(Path("data/exports"), "--out", "-o", help="Output directory."),
) -> None:
    """Export stable-schema CSV / JSON datasets (results, calls, timeline, polls, forecasts …)."""
    from app.cli.datasets import DATASETS, DEFAULT_ALL, build_dataset
    from app.export.writers import export_bundle

    formats = ["csv", "json"] if fmt == "both" else [fmt]
    if any(f not in ("csv", "json") for f in formats):
        raise NotFoundError(f"unknown format {fmt!r} (csv, json or both)")
    names = list(DEFAULT_ALL) if dataset == "all" else [d.strip() for d in dataset.split(",") if d.strip()]
    unknown = [n for n in names if n not in DATASETS]
    if unknown:
        raise NotFoundError(f"unknown dataset(s) {unknown}; available: {', '.join(DATASETS)}, all")
    frames = {}
    with session_scope() as s:
        eid = resolve_election(s, election)
        for name in names:
            try:
                with spinner(f"building {name} …"):
                    ds, df = build_dataset(s, eid, name)
            except NotFoundError as exc:
                if dataset != "all":
                    raise
                err_console.print(f"[yellow]skipped {name}:[/] {exc}")
                continue
            frames[ds.schema] = df
    if not frames:
        raise NotFoundError(f"nothing to export for election {eid}")
    target = out / f"election_{eid}"
    paths = export_bundle(frames, target, formats, metadata={"election_id": eid, "datasets": sorted(frames)})
    rows = [(p.name, fmt_int(p.stat().st_size)) for p in paths]
    console.print(table(f"exported to {target}", ["file", ("bytes", "right")], rows))
