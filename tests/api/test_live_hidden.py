"""The hidden-until-reported rule while a night is LIVE: early in the night no endpoint may reveal
the stored final result (province and national totals), only what has been counted so far."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("app.services.night")

_INT = re.compile(r"(?<![\d.])\d{6,}(?![\d.])")


def _final_totals(db: Path, election_id: int) -> set[str]:
    """Stored final vote totals / ballots / valid votes (≥ 100,000) of the election at province and
    national level; eligible voters are inputs (DERIVED), not results, and are excluded."""
    con = sqlite3.connect(db)
    try:
        rid = [r for (r,) in con.execute("select id from race where election_id = ?", (election_id,))]
        marks = ",".join("?" * len(rid))
        votes = con.execute(
            f"select votes from election_result where race_id in ({marks}) "
            "and level in ('national','province') and votes >= 100000",
            rid,
        ).fetchall()
        turnout = con.execute(
            f"select ballots_cast, valid_votes, eligible_voters from turnout_result where race_id in ({marks}) "
            "and level in ('national','province')",
            rid,
        ).fetchall()
    finally:
        con.close()
    eligible = {str(e) for (_, _, e) in turnout}
    out = {str(v) for (v,) in votes} | {str(x) for row in turnout for x in row[:2] if x >= 100000}
    return out - eligible


LIVE_PAGES = [
    "/api/elections",
    "/api/elections/{e}",
    "/api/elections/{e}/president",
    "/api/elections/{e}/electoral-college",
    "/api/elections/{e}/provinces",
    "/api/elections/{e}/provinces?race=GOV",
    "/api/elections/{e}/provinces/NB",
    "/api/elections/{e}/provinces/ZH",
    "/api/elections/{e}/municipalities",
    "/api/elections/{e}/municipalities?race=GOV",
    "/api/elections/{e}/house",
    "/api/elections/{e}/house/NB-01",
    "/api/elections/{e}/senate",
    "/api/elections/{e}/governors",
    "/api/elections/{e}/races/PRES",
    "/api/elections/{e}/races/PRES-NB",
    "/api/elections/{e}/races/GOV-ZH",
    "/api/elections/{e}/calls?evidence=true",
    "/api/elections/{e}/timeline",
    "/api/elections/{e}/timeline?detail=events",
    "/api/analytics/{e}",
    "/api/history/summary",
    "/api/history/closest",
    "/api/history/landslides",
    "/api/history/divergence",
    "/api/polls/{e}",
    "/api/polls/{e}/average",
    "/api/campaigns/{e}",
    "/api/night/{e}/state?detail=full",
    "/api/night/{e}/municipalities?race=PRES",
    "/api/night/{e}/races/PRES",
    "/api/night/{e}/races/PRES-NB",
    "/api/night/{e}/races/GOV-ZH",
    "/api/candidates?limit=200",
    "/api/parties",
]


def test_live_night_reveals_only_counted_votes(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    hidden = _final_totals(rw_client.db_path, e)  # type: ignore[attr-defined]
    assert len(hidden) > 20
    assert rw_client.post(f"/api/night/{e}/control", json={"action": "start"}).status_code == 200
    st = rw_client.post(f"/api/night/{e}/control", json={"action": "pause"}).json()
    for _ in range(4):
        st = rw_client.post(f"/api/night/{e}/control", json={"action": "step"}).json()
    assert st["election_status"] == "live" and 0 < st["clock"]["seq"] < st["clock"]["total_events"]
    # early in the night no province-wide race has been counted completely
    assert not any(p["status"] in ("FINAL", "RECOUNT") for p in st["snapshot"]["provinces"])
    for page in LIVE_PAGES:
        r = rw_client.get(page.format(e=e))
        assert r.status_code == 200, (page, r.text[:300])
        leaked = set(_INT.findall(r.text)) & hidden
        assert not leaked, f"{page} leaks stored results during the live night: {sorted(leaked)[:5]}"
    for ticket in rw_client.get(f"/api/elections/{e}/president").json()["tickets"]:
        prof = rw_client.get(f"/api/candidates/{ticket['president']['id']}")
        assert prof.status_code == 200 and not set(_INT.findall(prof.text)) & hidden
        assert all(c["result"] is None for c in prof.json()["candidacies"] if c["election"]["id"] == e)
    muni = rw_client.get(f"/api/night/{e}/municipalities?race=PRES").json()["municipalities"][0]["code"]
    r = rw_client.get(f"/api/night/{e}/municipalities/{muni}")
    assert r.status_code == 200 and not set(_INT.findall(r.text)) & hidden
    # results exports stay closed until the election is final; poll accuracy stays hidden
    for dataset in ("national", "provinces", "municipalities", "units", "calls", "timeline", "swing"):
        assert rw_client.get(f"/api/export/{e}/{dataset}.csv").status_code == 409, dataset
    assert rw_client.get(f"/api/polls/{e}/average").json()["average"]["accuracy"] is None
    camp = rw_client.get(f"/api/campaigns/{e}").json()
    assert camp["effects_revealed"] is False
