"""``election-night`` in the terminal: headless and live broadcast runs on a fake clock, pausing at
``--until``, resuming, reset and the instant finish (persisted through the night service)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from rich.console import Console
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from typer.testing import CliRunner

from app.cli import night as night_cli
from app.cli._common import EXIT_CONFLICT
from app.cli.main import app
from app.models import Election, NightSession, RaceCall


class FakeClock:
    """Wall clock of the live loop: sleeping advances it instantly."""

    def __init__(self) -> None:
        self.t = 1000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)


@pytest.fixture()
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(night_cli, "_now", clock.now)
    monkeypatch.setattr(night_cli, "_sleep", clock.sleep)
    return clock


def invoke(runner: CliRunner, *args: str, code: int = 0) -> Any:
    result = runner.invoke(app, list(args))
    assert result.exit_code == code, f"{args}: exit {result.exit_code}\n{result.output}\n{result.exception!r}"
    return result


def db_state(url: str, election_id: int) -> tuple[str, NightSession | None, int]:
    engine = create_engine(url)
    try:
        with sessionmaker(bind=engine, expire_on_commit=False)() as s:
            status = s.scalar(select(Election.status).where(Election.id == election_id))
            ns = s.scalar(select(NightSession).where(NightSession.election_id == election_id))
            calls = s.scalar(
                select(func.count()).select_from(RaceCall).where(RaceCall.election_id == election_id)
            )
            return status, ns, int(calls or 0)
    finally:
        engine.dispose()


def test_headless_night_pauses_at_until_and_resumes(cli_db, runner: CliRunner, fake_clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    eid = cli_db.second
    res = invoke(runner, "election-night", "-e", "2028", "--headless", "--speed", "25", "--until", "22:00")
    assert "paused at 22:00" in res.output
    assert "President (88 TO WIN of 174)" in res.output
    status, ns, calls = db_state(cli_db.url, eid)
    assert status == "live" and ns.status == "paused" and ns.sim_time_s == pytest.approx(3600.0)
    assert ns.speed == 25.0 and ns.current_seq > 0 and calls >= 195
    # resume from the persisted night and stop again later
    res = invoke(runner, "election-night", "-e", "demo", "--headless", "--speed", "10", "--until", "22:30")
    assert "paused at 22:30" in res.output
    status, ns, calls2 = db_state(cli_db.url, eid)
    assert ns.sim_time_s == pytest.approx(5400.0) and ns.speed == 10.0 and calls2 > calls
    # reset back to polls closing, then finish at once
    res = invoke(runner, "election-night", "-e", "2028", "--reset", "--headless", "--until", "21:30")
    assert "reset to polls closing" in res.output and "paused at 21:30" in res.output
    res = invoke(runner, "election-night", "-e", "2028", "--instant", "--json")
    data = json.loads(res.stdout)
    assert data["election_status"] == "final" and data["clock"]["status"] == "finished"
    assert data["president"]["ev_total"] == 174 and data["president"]["ev_needed"] == 88
    assert data["clock"]["seq"] == data["clock"]["total_events"]
    status, ns, _ = db_state(cli_db.url, eid)
    assert status == "final" and ns.status == "finished"
    res = invoke(runner, "election-night", "-e", "2028", "--headless")
    assert "its night is complete" in res.output
    invoke(runner, "election-night", "-e", "2028", "--reset", code=EXIT_CONFLICT)


def test_live_broadcast_screen(cli_db, runner: CliRunner, fake_clock: FakeClock) -> None:  # type: ignore[no-untyped-def]
    res = invoke(runner, "election-night", "-e", "2028", "--speed", "25", "--until", "23:00")
    out = res.output
    for text in ("ELECTION NIGHT", "PRESIDENT", "88 TO WIN", "HOUSE", "76 FOR CONTROL", "SENATE", "CALLS"):
        assert text in out
    assert "paused at 23:00" in out
    invoke(runner, "election-night", "-e", "2028", "--speed", "3", "--until", "23:30", code=EXIT_CONFLICT)


def _state(decided: dict[str, int], leading: dict[str, int]) -> dict[str, Any]:
    tickets = [
        {
            "key": k,
            "label": k.upper(),
            "party": k.upper(),
            "color": "#ff0000" if k == "a" else "#0000ff",
            "ev_decided": decided.get(k, 0),
            "ev_leading": leading.get(k, 0),
            "ev_max_possible": 174,
        }
        for k in ("a", "b")
    ]
    return {
        "election_id": 1,
        "year": 2028,
        "name": "Test",
        "election_status": "live",
        "labels": {"president": "88 TO WIN", "house": "76 FOR CONTROL", "senate": "13 FOR CONTROL"},
        "clock": {
            "status": "running",
            "speed": 10.0,
            "seq": 5,
            "total_events": 10,
            "sim_time_s": 60.0,
            "clock": "21:01",
            "polls_close_local": "21:00",
            "base_rate": 60.0,
        },
        "snapshot": {
            "reporting": {
                "pct_expected_ballots": 12.5,
                "municipalities_reporting": 3,
                "municipalities_total": 12,
            },
            "president": {
                "ev_total": 174,
                "ev_needed": 88,
                "ev_decided_total": sum(decided.values()),
                "ev_uncalled": 174 - sum(decided.values()),
                "tickets": tickets,
                "winner": None,
                "contingent_likely": False,
            },
            "popular_vote": {
                "lines": [{"key": "a", "votes": 100, "pct": 55.0}, {"key": "b", "votes": 80, "pct": 45.0}]
            },
            "house": {
                "majority": 76,
                "uncalled": 140,
                "control": None,
                "by_party": [{"party": "A", "color": "#ff0000", "called": 10, "leading": 3}],
            },
            "senate": None,
            "governors": [],
            "recent_calls": [
                {
                    "race_key": "PRES-NB",
                    "seq": 5,
                    "status": "CALLED",
                    "key": "a",
                    "timestamp": "2028-11-07T21:01:00+01:00",
                    "reporting_pct": 40.0,
                    "win_probability": 0.9999,
                    "is_manual": False,
                    "retracted": False,
                }
            ],
        },
    }


def test_render_helpers() -> None:
    bar = night_cli.ev_bar(_state({"a": 40}, {"b": 20})["snapshot"]["president"], width=58)
    assert len(bar.plain) == 58
    assert bar.plain.count("┃") == 1 and bar.plain.index("┃") == round(58 * 88 / 174)
    assert bar.plain.count("█") == round(58 * 40 / 174) and bar.plain.count("▒") == round(58 * 20 / 174)
    state = _state({"a": 90}, {})
    console = Console(width=140, record=True)
    console.print(night_cli.render_broadcast(state, night_cli.line_labels(state)))
    text = console.export_text()
    assert "88 TO WIN" in text and "PRES-NB" in text and "CALLED" in text and "12.5%" in text
    lines = night_cli.summary_lines(state)
    assert lines[1].startswith("President (88 TO WIN of 174): A 90") and "House (76 FOR CONTROL)" in lines[2]
    assert night_cli._parse_until("00:30", "21:00") == pytest.approx(3.5 * 3600)
    assert night_cli._parse_until("21:00", "21:00") == 0.0
    assert night_cli._parse_until(None, "21:00") is None
