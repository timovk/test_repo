"""Election-night endpoints (skipped when the night service is not installed)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("app.services.night")


def _start(c: TestClient, e: int, steps: int = 30) -> dict:
    r = c.post(f"/api/night/{e}/control", json={"action": "start"})
    assert r.status_code == 200, r.text
    st = r.json()
    for _ in range(steps):
        st = c.post(f"/api/night/{e}/control", json={"action": "step"}).json()
    return st


def test_state_before_start(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    st = rw_client.get(f"/api/night/{api_world.hidden_id}/state").json()
    assert st["clock"]["status"] == "ready" and st["snapshot"]["seq"] == 0
    assert st["data_category"] == "SIMULATED"
    # a night that has not started keeps the election hidden
    p = rw_client.get(f"/api/elections/{api_world.hidden_id}/president").json()
    assert p["results_source"] == "hidden"


def test_live_night_flow(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    st = _start(rw_client, e)
    assert st["election_status"] == "live" and st["clock"]["seq"] > 0
    p = rw_client.get(f"/api/elections/{e}/president").json()
    assert p["results_source"] == "live" and p["election"]["live"] is True
    counted = sum(t["votes"] for t in p["tickets"])
    assert counted == p["popular_vote"]["total_valid"] > 0
    assert {"ev_leading", "ev_max_possible"} <= set(p["tickets"][0])
    h = rw_client.get(f"/api/elections/{e}/house").json()
    assert h["results_source"] == "live" and len(h["districts"]) == 150
    prov = rw_client.get(f"/api/elections/{e}/provinces/NH").json()
    assert prov["results_source"] == "live" and prov["municipalities"]["rows"]
    assert prov["outstanding"]["votes_est"] >= 0
    m = rw_client.get(f"/api/elections/{e}/municipalities").json()
    assert m["results_source"] == "live" and any(x["reporting_pct"] for x in m["municipalities"])
    house_map = rw_client.get(f"/api/elections/{e}/municipalities?race=HOUSE").json()
    assert house_map["results_source"] == "live" and house_map["race"] == "HOUSE"
    assert house_map["count"] == m["count"] and any(x["votes"] for x in house_map["municipalities"])
    # House fragments pooled per municipality: counted votes equal the sum of the district races
    districts = rw_client.get(f"/api/elections/{e}/house").json()["districts"]
    fragments = rw_client.get(f"/api/night/{e}/municipalities?race={districts[0]['race_code']}").json()
    by_code = {x["code"]: x for x in house_map["municipalities"]}
    assert all(by_code[f["code"]]["units_total"] >= f["units_total"] for f in fragments["municipalities"])
    pres = rw_client.get(f"/api/elections/{e}/races/PRES").json()
    assert pres["race"]["total_votes"] == counted and pres["race"]["reporting_pct"] is not None
    race = rw_client.get(f"/api/elections/{e}/races/PRES-NH").json()
    assert race["race"]["status"] and "decision" in race and "lead_changes" in race
    rows = rw_client.get(f"/api/night/{e}/municipalities?race=PRES").json()
    assert rows["count"] == len(rows["municipalities"]) > 0
    assert rw_client.get(f"/api/night/{e}/races/PRES-NH").json()["key"] == "PRES-NH"
    code = rows["municipalities"][0]["code"]
    assert rw_client.get(f"/api/night/{e}/municipalities/{code}").json()["code"] == code
    tl = rw_client.get(f"/api/elections/{e}/timeline").json()
    assert 0 < tl["revealed_events"] <= tl["total_events"]
    # the live call log is every call made so far (persisted by the night service)
    log = rw_client.get(f"/api/elections/{e}/calls?evidence=true").json()
    seq = rw_client.get(f"/api/night/{e}/state").json()["clock"]["seq"]
    assert log["results_source"] == "live" and log["count"] == len(log["calls"]) > 0
    assert all(c["seq"] <= seq and c["race_code"] and "evidence" in c for c in log["calls"])
    assert [c["seq"] for c in log["calls"]] == sorted(c["seq"] for c in log["calls"])
    # results exports stay closed while the night is live
    assert rw_client.get(f"/api/export/{e}/provinces.csv").status_code == 409
    assert rw_client.post(f"/api/elections/{e}/finalize").status_code == 409


def test_override_and_finish(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    _start(rw_client, e, steps=5)
    lines = rw_client.get(f"/api/elections/{e}/races/GOV-NB").json()["race"]["lines"]
    r = rw_client.post(
        f"/api/night/{e}/calls/GOV-NB/override",
        json={"status": "LEAN", "line_key": lines[0]["key"], "reason": "test producer call"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["race_key"] == "GOV-NB"
    bad = rw_client.post(f"/api/night/{e}/calls/GOV-NB/override", json={"status": "LEAN", "reason": ""})
    assert bad.status_code == 422
    assert (
        rw_client.post(
            f"/api/night/{e}/calls/NOPE/override", json={"status": "CLEAR", "reason": "x"}
        ).status_code
        == 404
    )
    fin = rw_client.post(f"/api/night/{e}/control", json={"action": "finish"}).json()
    assert fin["election_status"] == "final"
    p = rw_client.get(f"/api/elections/{e}/president").json()
    assert p["results_source"] == "final" and sum(t["electoral_votes"] for t in p["tickets"]) == 174
    calls = rw_client.get(f"/api/elections/{e}/calls?race_type=GOVERNOR").json()
    assert calls["count"] > 0 and any(c["is_manual"] for c in calls["calls"])


def test_control_validation(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    r = rw_client.post(f"/api/night/{e}/control", json={"action": "explode"})
    assert r.status_code == 422 and r.json()["code"] == "invalid"
    r = rw_client.post(f"/api/night/{e}/control", json={"action": "pause"})
    assert r.status_code == 409
    assert rw_client.get("/api/night/999/state").status_code == 404


def test_sse_stream(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    _start(rw_client, e, steps=2)
    with rw_client.stream("GET", f"/api/night/{e}/stream?max_events=1") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        data = [ln for ln in r.iter_lines() if ln.startswith("data: ")]
    assert len(data) == 1
    payload = json.loads(data[0][len("data: ") :])
    assert payload["election_id"] == e and payload["snapshot"]["seq"] > 0
    assert payload["clock"]["status"] in ("running", "paused")
