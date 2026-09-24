"""The command-line interface against a copy of the synthetic sandbox world: listings, history,
validation, election lifecycle, exports, districts, polls, scenarios, database commands, demo /
run wiring and friendly errors."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from app.cli import _common
from app.cli.main import app
from app.core.errors import DataNotPreparedError, ElectionError, NotFoundError, ScenarioError
from app.models import Election
from app.services.demo import DemoStep, DemoSummary

CliDB = Any  # tests/cli/conftest.py::CliDB (path, url, founding, second)


def run(runner: CliRunner, *args: str, code: int = 0) -> Any:
    result = runner.invoke(app, list(args))
    assert result.exit_code == code, f"{args}: exit {result.exit_code}\n{result.output}\n{result.exception!r}"
    return result


def as_json(result: Any) -> Any:
    return json.loads(result.stdout)


def status_of(db: CliDB, election_id: int) -> str:
    engine = create_engine(db.url)
    try:
        with sessionmaker(bind=engine)() as s:
            return s.scalar(select(Election.status).where(Election.id == election_id))
    finally:
        engine.dispose()


# =========================================================================== basics
def test_help_and_version(runner: CliRunner) -> None:
    out = run(runner, "--help").output
    for cmd in (
        "init",
        "demo",
        "run",
        "validate",
        "geography",
        "apportion",
        "districts",
        "election",
        "simulate",
        "finalize",
        "forecast",
        "election-night",
        "export",
        "scenario",
        "polls",
        "history",
        "db",
    ):
        assert cmd in out
    assert "nlfed 1.0.0" in run(runner, "--version").output
    assert "--instant" in run(runner, "election-night", "--help").output


def test_exit_codes() -> None:
    assert _common.exit_code_for(DataNotPreparedError())[0] == _common.EXIT_NOT_PREPARED
    assert _common.exit_code_for(NotFoundError("x"))[0] == _common.EXIT_NOT_FOUND
    assert _common.exit_code_for(ScenarioError("x"))[0] == _common.EXIT_INVALID
    assert _common.exit_code_for(ElectionError("x"))[0] == _common.EXIT_CONFLICT
    assert _common.exit_code_for(RuntimeError("x"))[0] == _common.EXIT_FAILURE


# =========================================================================== read commands
def test_listings_history_and_validation(cli_db: CliDB, runner: CliRunner) -> None:
    elections = as_json(run(runner, "election", "list", "--json"))
    assert [(e["id"], e["year"], e["status"]) for e in elections] == [
        (cli_db.founding, 2024, "final"),
        (cli_db.second, 2028, "simulated"),
    ]
    out = run(runner, "election", "list").output
    assert "SIMULATED" in out and "demo-2028" in out
    history = as_json(run(runner, "history", "--json"))
    assert [h["year"] for h in history] == [2024]
    assert history[0]["decided_by"] in ("electoral_college", "contingent")
    assert "election history" in run(runner, "history").output
    shown = run(runner, "election", "show", "-e", "2024").output
    assert "President" in shown and "88 TO WIN" in shown and "76 FOR CONTROL" in shown
    hidden = run(runner, "election", "show", "-e", "latest").output
    assert "results hidden" in hidden
    summary = as_json(run(runner, "election", "show", "-e", str(cli_db.second), "--json"))
    assert summary["status"] == "simulated" and "results" not in summary
    report = as_json(run(runner, "validate", "--json"))
    assert report["ok"] is True and not report["errors"]
    assert "valid" in run(runner, "validate").output
    assert as_json(run(runner, "validate", "-e", "2028", "--json"))["ok"] is True


def test_districts_apportionment_and_polls(cli_db: CliDB, runner: CliRunner) -> None:
    appt = as_json(run(runner, "apportion", "--json"))
    assert appt["total_seats"] == 150 and appt["total_electoral_votes"] == 174
    assert appt["presidential_majority"] == 88 and len(appt["provinces"]) == 12
    assert all(p["electoral_votes"] == p["seats"] + 2 for p in appt["provinces"])
    assert "88 TO WIN" in run(runner, "apportion").output
    plan = as_json(run(runner, "districts", "show", "--province", "NB", "--json"))
    assert plan["districts"] and {d["province_code"] for d in plan["districts"]} == {"NB"}
    assert len(as_json(run(runner, "districts", "show", "--json"))["districts"]) == 150
    assert "House plan" in run(runner, "districts", "show").output
    checks = as_json(run(runner, "districts", "validate", "--json"))
    assert checks["ok"] and any("district" in c["name"] for c in checks["checks"])
    run(runner, "districts", "show", "--province", "XX", code=_common.EXIT_NOT_FOUND)
    polls = as_json(run(runner, "polls", "-e", "2028", "--json"))
    assert polls["polls"] > 0 and polls["averages"] and polls["data_category"] == "SIMULATED"
    out = run(runner, "polls", "-e", "2028", "--type", "national_president").output
    assert "FICTIONAL polls" in out and "national_president" in out


# =========================================================================== lifecycle
def test_create_and_simulate(cli_db: CliDB, runner: CliRunner) -> None:
    created = as_json(run(runner, "election", "create", "--year", "2026", "--json"))
    assert created["year"] == 2026 and created["status"] == "scheduled"
    assert created["scenario"]["slug"] == "midterm-2026"
    out = run(runner, "simulate", "--election", "2026", "--seed", "7").output
    assert "simulated with seed 7" in out and "hidden" in out
    assert status_of(cli_db, created["id"]) == "simulated"
    run(runner, "election", "create", code=_common.EXIT_INVALID)  # neither --year nor --scenario
    run(runner, "election", "create", "--year", "2029", code=_common.EXIT_INVALID)  # no scenario


def test_finalize_and_export(cli_db: CliDB, runner: CliRunner, tmp_path: Path) -> None:
    run(runner, "export", "-e", "2028", "-d", "national", "-o", str(tmp_path), code=_common.EXIT_CONFLICT)
    final = as_json(run(runner, "finalize", "-e", "2028", "--json"))
    assert final["status"] == "final" and final["results"]["president"]["majority"] == 88
    run(runner, "finalize", "-e", "2028", code=_common.EXIT_CONFLICT)  # already final
    out = run(runner, "export", "-e", "2028", "-d", "national,house,calls", "-f", "both", "-o", str(tmp_path))
    assert "exported 3 dataset(s)" in out.output
    folder = tmp_path / f"election_{cli_db.second}"
    names = {p.name for p in folder.iterdir()}
    assert {"national_results.csv", "national_results.json", "house_results.csv", "race_calls.csv"} <= names
    manifest = json.loads((folder / "manifest.json").read_text())
    assert {f["schema"] for f in manifest["files"]} == {"national_results", "house_results", "race_calls"}
    house = (folder / "house_results.csv").read_text().splitlines()
    assert len(house) == 151 and house[0].startswith("election_id,year,race_code,district_code")
    run(runner, "export", "-e", "2028", "-d", "nonsense", "-o", str(tmp_path), code=_common.EXIT_NOT_FOUND)
    everything = run(runner, "export", "-e", "2028", "-d", "all", "-o", str(tmp_path / "all"))
    assert "skipped montecarlo_summary" in everything.output  # no forecast yet
    history = as_json(run(runner, "history", "--json"))
    assert [h["year"] for h in history] == [2024, 2028]


def test_forecast(cli_db: CliDB, runner: CliRunner) -> None:
    pytest.importorskip("app.services.forecast_runner")
    data = as_json(
        run(runner, "forecast", "-e", "2028", "-n", "300", "--seed", "3", "--workers", "1", "--json")
    )
    assert data["n_simulations"] == 300 and data["seed"] == 3 and data["data_category"] == "SIMULATED"
    pres = data["result"]["president"]
    assert pres["majority"] == 88 and pres["total_ev"] == 174
    out = run(runner, "forecast", "-e", "2028", "-n", "200", "--workers", "1", "--no-polls").output
    assert "P(contingent election)" in out and "Not a prediction" in out
    run(runner, "forecast", "-e", "2028", "-n", "0", code=2)  # usage error


# =========================================================================== errors
def test_friendly_errors(cli_db: CliDB, runner: CliRunner) -> None:
    res = run(runner, "simulate", "-e", "1999", code=_common.EXIT_NOT_FOUND)
    assert "Not found" in res.output and "Traceback" not in res.output
    run(runner, "simulate", "-e", "banana", code=_common.EXIT_NOT_FOUND)
    run(runner, "simulate", "-e", "2024", code=_common.EXIT_CONFLICT)  # final elections are history
    res = run(runner, "election-night", "-e", "2024", "--reset", code=_common.EXIT_CONFLICT)
    assert "immutable" in res.output


def test_database_commands(runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    monkeypatch.setenv("NLFED_DATABASE_URL", "sqlite:///" + str(tmp_path / "unused.db"))
    monkeypatch.setenv("COLUMNS", "300")
    try:
        out = run(runner, "--database-url", url, "db", "upgrade").output
        assert "at revision" in out
        rev = run(runner, "--database-url", url, "db", "current").output
        assert "fresh.db" in rev and "no schema" not in rev
        res = run(runner, "--database-url", url, "apportion", code=_common.EXIT_NOT_PREPARED)
        assert "Data not prepared" in res.output
        out = run(runner, "--database-url", url, "init").output
        assert "database ready" in out
    finally:
        from app.core.settings import reset_settings_cache
        from app.db.session import reset_engines
        from app.services.night import reset_night_manager

        monkeypatch.undo()  # restores NLFED_DATABASE_URL, which --database-url overwrote
        reset_settings_cache()
        reset_engines()
        reset_night_manager()


# =========================================================================== scenarios
@pytest.fixture()
def scenario_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.scenarios import loader

    src = loader.scenarios_dir()
    dst = tmp_path / "scenarios"
    dst.mkdir()
    for f in src.glob("*.yaml"):
        shutil.copyfile(f, dst / f.name)
    monkeypatch.setattr(loader, "scenarios_dir", lambda: dst)
    return dst


def test_scenario_commands(
    runner: CliRunner, scenario_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = as_json(run(runner, "scenario", "list", "--json"))
    assert {s["slug"] for s in listing} >= {"founding-2024", "midterm-2026", "demo-2028"}
    assert all(s["valid"] for s in listing)
    shown = run(runner, "scenario", "show", "demo-2028").output
    assert "FICTIONAL" in shown and "presidential tickets" in shown
    assert "scenario:" in run(runner, "scenario", "show", "demo-2028", "--yaml").output
    out_file = tmp_path / "export.yaml"
    run(runner, "scenario", "export", "demo-2028", "--out", str(out_file))
    assert out_file.read_text().startswith("# Scenario 'demo-2028'")
    run(runner, "scenario", "duplicate", "demo-2028", "demo-2028-alt", "--seed", "99")
    # user scenarios live where the API's scenario editor keeps them (editable there)
    assert (scenario_dir / "user" / "demo-2028-alt.yaml").exists()
    from app.services.read.scenarios import locate

    assert locate("demo-2028-alt") == ("user", scenario_dir / "user" / "demo-2028-alt.yaml")
    run(runner, "scenario", "duplicate", "demo-2028", "demo-2028-alt", code=_common.EXIT_INVALID)
    run(runner, "scenario", "import", str(out_file), code=_common.EXIT_INVALID)  # slug exists
    run(runner, "scenario", "import", str(out_file), "--slug", "imported-2028")
    listing = {s["slug"]: s for s in as_json(run(runner, "scenario", "list", "--json"))}
    assert listing["demo-2028-alt"]["seed"] == 99 and "imported-2028" in listing
    assert listing["demo-2028-alt"]["source"] == "user" and listing["demo-2028"]["source"] == "builtin"
    monkeypatch.setattr("app.geography.store.is_prepared", lambda year=None: False)
    res = as_json(run(runner, "scenario", "validate", "demo-2028-alt", "--json"))
    assert res == {
        "slug": "demo-2028-alt",
        "schema_valid": True,
        "geography_checked": False,
        "problems": [],
        "ok": True,
    }
    run(runner, "scenario", "show", "no-such-scenario", code=_common.EXIT_INVALID)


def test_next_cycle_from_a_duplicated_scenario(runner: CliRunner, cli_db: CliDB, scenario_dir: Path) -> None:
    run(runner, "scenario", "duplicate", "midterm-2026", "midterm-2030", "--seed", "20300001")
    created = as_json(
        run(
            runner,
            "election",
            "create",
            "--year",
            "2030",
            "--scenario",
            "midterm-2030",
            "--simulate",
            "--json",
        )
    )
    assert created["year"] == 2030 and created["status"] == "simulated"
    assert created["scenario"]["slug"].startswith("midterm-2030")
    # an election dated before a FINAL one could never be certified: refused (conflict)
    run(runner, "election", "create", "--scenario", "founding-2024", code=_common.EXIT_CONFLICT)


@pytest.mark.realdata
def test_scenario_validation_against_real_geography(real_frame, runner: CliRunner) -> None:  # type: ignore[no-untyped-def]
    for slug in ("founding-2024", "midterm-2026", "demo-2028"):
        res = as_json(run(runner, "scenario", "validate", slug, "--json"))
        assert res["geography_checked"] and res["ok"], res["problems"]


# =========================================================================== demo / run wiring
def test_demo_command_renders_the_summary(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_build_demo(**kwargs: Any) -> DemoSummary:
        seen.update(kwargs)
        kwargs["progress"]("system", "done", "12 provinces")
        return DemoSummary(
            seed=kwargs["seed"],
            database_url="sqlite:///demo.db",
            synthetic=kwargs["synthetic"],
            elections={"founding-2024": 1, "midterm-2026": 2, "demo-2028": 3},
            demo_election_id=3,
            validation_ok=True,
            steps=[DemoStep("system", "done", 1.5, "12 provinces")],
            total_seconds=1.5,
        )

    monkeypatch.setattr("app.services.demo.build_demo", fake_build_demo)
    out = run(runner, "demo", "--seed", "11", "--forecast-simulations", "0", "--force").output
    assert "demo ready" in out and "demo election 3" in out
    assert seen["seed"] == 11 and seen["force"] is True and seen["forecast_simulations"] == 0
    data = as_json(run(runner, "demo", "--synthetic", "--json"))
    assert data["demo_election_id"] == 3 and data["synthetic"] is True


def test_run_command_starts_uvicorn(runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append((a, k)))
    run(runner, "run", "--host", "0.0.0.0", "--port", "9999", "--reload")
    ((args, kwargs),) = calls
    assert args == ("app.api.main:create_app",)
    assert kwargs["factory"] is True and kwargs["port"] == 9999 and kwargs["host"] == "0.0.0.0"
    assert kwargs["reload"] is True
