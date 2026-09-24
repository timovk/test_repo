"""``apportion`` and ``districts generate | validate | show`` — the FICTIONAL electoral geography
(150 House seats apportioned over the 12 REAL provinces, 150 single-member districts)."""

from __future__ import annotations

import typer
from sqlalchemy import select

from app.cli._common import (
    EXIT_FAILURE,
    console,
    data_badge,
    fmt_int,
    friendly,
    print_json,
    session_scope,
    spinner,
    table,
)

districts_app = typer.Typer(no_args_is_help=True)

_PLAN_CHECK_WORDS = ("district", "house", "unit", "apportion", "seats", "electoral votes", "province")


@friendly
def apportion(
    method: str | None = typer.Option(
        None, "--method", help="huntington_hill (default), webster, jefferson, adams, dean or hamilton."
    ),
    year: int | None = typer.Option(None, "--year", help="Geography vintage (default: the active one)."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """Apportion the 150 House seats over the provinces (EV = seats + 2; 174 EV, 88 to win)."""
    from app.core.config import get_constitution
    from app.models import ApportionmentSeat, Province
    from app.services.bootstrap import ensure_apportionment

    cons = get_constitution()
    with session_scope() as s:
        appt = ensure_apportionment(s, year, method)
        rows = s.execute(
            select(
                Province.code,
                Province.name,
                ApportionmentSeat.population,
                ApportionmentSeat.quota,
                ApportionmentSeat.seats,
                ApportionmentSeat.electoral_votes,
                ApportionmentSeat.persons_per_seat,
            )
            .join(Province, Province.id == ApportionmentSeat.province_id)
            .where(ApportionmentSeat.apportionment_id == appt.id)
            .order_by(ApportionmentSeat.seats.desc(), Province.code)
        ).all()
        info = (appt.id, appt.method, appt.total_seats, appt.total_electoral_votes)
    if as_json:
        print_json(
            {
                "apportionment_id": info[0],
                "method": info[1],
                "total_seats": info[2],
                "total_electoral_votes": info[3],
                "presidential_majority": cons.presidential_majority,
                "provinces": [
                    {
                        "code": r[0],
                        "name": r[1],
                        "population": r[2],
                        "quota": r[3],
                        "seats": r[4],
                        "electoral_votes": r[5],
                        "persons_per_seat": r[6],
                    }
                    for r in rows
                ],
                "data_category": "FICTIONAL",
            }
        )
        return
    body = [(r[0], r[1], fmt_int(r[2]), f"{r[3]:.3f}", r[4], r[5], fmt_int(round(r[6]))) for r in rows]
    body.append(("", "total", fmt_int(sum(r[2] for r in rows)), "", info[2], info[3], ""))
    console.print(
        table(
            f"{data_badge('FICTIONAL')} apportionment {info[0]} ({info[1]}) of {data_badge('REAL')} populations",
            [
                "",
                "province",
                ("population", "right"),
                ("quota", "right"),
                ("seats", "right"),
                ("EV", "right"),
                ("persons/seat", "right"),
            ],
            body,
        )
    )
    console.print(
        f"{info[2]} House seats · {info[3]} electoral votes · [bold]{cons.presidential_majority} TO WIN[/]"
    )


@districts_app.command("generate")
@friendly
def generate(
    year: int | None = typer.Option(2028, "--year", help="First election year the plan applies to."),
    seed: int = typer.Option(2028, "--seed", help="Seed of the district generator."),
    workers: int = typer.Option(0, "--workers", help="Worker processes (0 = automatic, 1 = serial)."),
) -> None:
    """Generate (or reuse) the 150-district House plan of the active geography and make it active."""
    from app.models import DistrictPlan
    from app.services.bootstrap import ensure_district_plan, ensure_offices

    with session_scope() as s:
        before = s.scalar(select(DistrictPlan.id).order_by(DistrictPlan.id.desc()).limit(1)) or 0
        with spinner(f"generating House districts (seed {seed}) …"):
            plan = ensure_district_plan(s, year, seed=seed, workers=workers)
            ensure_offices(s)
        info = {
            "plan_id": plan.id,
            "reused": plan.id <= before,
            "districts": plan.total_districts,
            "seed": int(plan.seed),
            "max_dev": plan.max_abs_deviation_pct,
            "mean_dev": plan.mean_abs_deviation_pct,
            "splits": plan.split_municipalities,
            "noncontiguous": plan.noncontiguous_districts,
            "seconds": plan.generation_seconds,
        }
    console.print(
        f"{data_badge('FICTIONAL')} House plan {info['plan_id']}"
        f"{' (existing plan reused)' if info['reused'] else ''}: {info['districts']} districts, seed {info['seed']}, "
        f"max |deviation| {info['max_dev']:.2f}%, mean {info['mean_dev']:.2f}%, "
        f"{info['splits']} split municipalities, {info['noncontiguous']} non-contiguous districts"
    )


@districts_app.command("validate")
@friendly
def validate_districts(as_json: bool = typer.Option(False, "--json", help="Print JSON.")) -> None:
    """Validate the apportionment, the active House plan and the Senate seats."""
    from app.services.validation import validate_system

    with session_scope() as s:
        rep = validate_system(s, elections=False)
    checks = [c for c in rep.checks if any(w in c.name.lower() for w in _PLAN_CHECK_WORDS)]
    ok = all(c.ok or c.severity != "error" for c in checks)
    if as_json:
        print_json({"ok": ok, "checks": [vars(c) for c in checks]})
    else:
        rows = [("[green]ok[/]" if c.ok else f"[red]{c.severity}[/]", c.name, c.detail) for c in checks]
        console.print(table("House plan and apportionment checks", ["", "check", "detail"], rows))
    if not ok:
        raise typer.Exit(EXIT_FAILURE)


@districts_app.command("show")
@friendly
def show(
    province: str | None = typer.Option(None, "--province", help="Only this province (e.g. NB)."),
    plan_id: int | None = typer.Option(None, "--plan", help="Plan id (default: the active plan)."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON."),
) -> None:
    """List the districts of the House plan with population, deviation and compactness."""
    from app.core.errors import NotFoundError
    from app.districts.service import active_plan
    from app.models import DistrictPlan, HouseDistrict, Province

    with session_scope() as s:
        plan = s.get(DistrictPlan, plan_id) if plan_id is not None else active_plan(s)
        if plan is None:
            raise NotFoundError("no House plan (run `districts generate` or `demo`)")
        q = (
            select(HouseDistrict, Province.code)
            .join(Province, Province.id == HouseDistrict.province_id)
            .where(HouseDistrict.plan_id == plan.id)
            .order_by(Province.sort_order, HouseDistrict.number)
        )
        if province:
            q = q.where(Province.code == province.upper())
        rows = s.execute(q).all()
        if province and not rows:
            raise NotFoundError(f"no districts in province {province}")
        data = [
            {
                "code": d.code,
                "province_code": pv,
                "name": d.name,
                "population": d.population,
                "deviation_pct": d.deviation_pct,
                "area_km2": d.area_km2,
                "polsby_popper": d.polsby_popper,
                "n_municipalities": d.n_municipalities,
                "n_split_municipalities": d.n_split_municipalities,
                "is_contiguous": d.is_contiguous,
            }
            for d, pv in rows
        ]
        head = (plan.id, int(plan.seed), plan.total_districts, plan.max_abs_deviation_pct)
    if as_json:
        print_json({"plan_id": head[0], "seed": head[1], "districts": data, "data_category": "FICTIONAL"})
        return
    body = [
        (
            d["code"],
            d["name"] or "",
            fmt_int(d["population"]),
            f"{d['deviation_pct']:+.2f}%",
            f"{d['area_km2']:.0f}",
            "" if d["polsby_popper"] is None else f"{d['polsby_popper']:.2f}",
            d["n_municipalities"],
            d["n_split_municipalities"],
            "yes" if d["is_contiguous"] else "[red]no[/]",
        )
        for d in data
    ]
    console.print(
        table(
            f"{data_badge('FICTIONAL')} House plan {head[0]} (seed {head[1]}, {head[2]} districts, "
            f"max |dev| {head[3]:.2f}%)",
            [
                "district",
                "name",
                ("population", "right"),
                ("deviation", "right"),
                ("km²", "right"),
                ("P-P", "right"),
                ("munis", "right"),
                ("split", "right"),
                "contiguous",
            ],
            body,
        )
    )
