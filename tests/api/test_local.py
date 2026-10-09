"""The local-election API on a copy of the synthetic sandbox: calendar, creation, the race list,
race pages of measures and vote-for-N races, the strict date order (earlier / finish-earlier)
and the local election list."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _first_days(client: TestClient, n: int = 2) -> list[dict]:
    cal = client.get("/api/local/calendar?start=2024-11-06&end=2025-12-31").json()
    assert cal["count"] > 0 and cal["days"][0]["date"] > "2024-11-06"
    assert {"province_code", "date", "races", "counts", "contests", "election_id"} <= set(cal["days"][0])
    assert len(cal["province_days"]) == 12
    return cal["days"][:n]


def test_local_calendar_create_and_results(rw_client: TestClient) -> None:
    day = _first_days(rw_client, 1)[0]
    r = rw_client.post(
        "/api/local/elections", json={"province_code": day["province_code"], "date": day["date"]}
    )
    assert r.status_code == 201, r.text
    detail = r.json()
    eid = detail["election"]["id"]
    assert detail["election"]["local"] is True and detail["election"]["province_code"] == day["province_code"]
    assert detail["contents"]["local"] is True
    assert detail["night"]["needs_simulation"] is True  # SCHEDULED: no night before simulating
    again = rw_client.post(
        "/api/local/elections", json={"province_code": day["province_code"], "date": day["date"]}
    )
    assert again.status_code == 409
    bad = rw_client.post(
        "/api/local/elections", json={"province_code": day["province_code"], "date": "2025-07-01"}
    )
    assert bad.status_code == 409

    races = rw_client.get(f"/api/elections/{eid}/races").json()
    assert races["results_source"] == "hidden" and races["count"] == day["races"]
    assert all(r["leader"] is None and r["passed"] is None for r in races["races"])
    measures = rw_client.get(f"/api/elections/{eid}/races?type=BALLOT_MEASURE").json()["races"]
    assert all(r["question"] and r["threshold"] in (0.5, 0.6, 0.6667) for r in measures)

    rw_client.post(f"/api/elections/{eid}/simulate")
    fin = rw_client.post(f"/api/elections/{eid}/finalize")
    assert fin.status_code == 200, fin.text
    races = rw_client.get(f"/api/elections/{eid}/races?sort=type").json()
    assert races["results_source"] == "final"
    for row in races["races"]:
        assert row["status"] in ("FINAL", "RECOUNT")
        if row["question"]:
            assert isinstance(row["passed"], bool) and row["yes_pct"] is not None
        elif row["vote_for"] > 1:
            assert len(row["winners"]) == row["vote_for"]
    for row in races["races"]:
        assert {"kind", "target_name", "replacement_race", "water_board_name"} <= set(row)
        if row["type"] == "WATER_BOARD":
            assert row["water_board_name"] and row["water_board"]
    q = races["races"][0]["name"].split()[0]
    assert rw_client.get(f"/api/elections/{eid}/races?q={q}").json()["count"] >= 1
    if measures:
        page = rw_client.get(f"/api/elections/{eid}/races/{measures[0]['code']}").json()["race"]
        c = page["contest"]
        assert c["kind"] == "measure" and c["title"] and "passed" in c and c["vote_for"] == 1
        assert [ln["key"] for ln in page["lines"]] == ["YES", "NO"]
    boards = [r for r in races["races"] if r["vote_for"] > 1]
    if boards:
        page = rw_client.get(f"/api/elections/{eid}/races/{boards[0]['code']}").json()["race"]
        assert (
            len(page["winners"]) == page["contest"]["vote_for"] == sum(ln["winner"] for ln in page["lines"])
        )
    listed = rw_client.get("/api/local/elections").json()
    assert (
        listed["count"] == 1
        and listed["elections"][0]["id"] == eid
        and listed["elections"][0]["races"] == day["races"]
    )
    meta = rw_client.get("/api/meta").json()
    brief = next(e for e in meta["elections"] if e["id"] == eid)
    assert brief["local"] and brief["province_code"] == day["province_code"]


def test_strict_order_api(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    days = _first_days(rw_client, 3)
    ids = []
    for d in days:
        r = rw_client.post(
            "/api/local/elections",
            json={"province_code": d["province_code"], "date": d["date"], "simulate": True},
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["election"]["id"])
    last = ids[-1]
    later_day = days[-1]["date"] > days[0]["date"]
    info = rw_client.get(f"/api/elections/{last}/earlier").json()
    if later_day:
        assert info["count"] >= 1 and info["first"]["name"]
        blocked = rw_client.post(f"/api/elections/{last}/finalize")
        assert blocked.status_code == 409 and "must be finished first" in blocked.json()["detail"]
        done = rw_client.post(f"/api/elections/{last}/finish-earlier").json()
        assert done["count"] == 0 and len(done["finished"]) >= 1
    assert rw_client.post(f"/api/elections/{last}/finalize").status_code == 200
    # the 2028 general now waits for every local election before it
    gen = rw_client.get(f"/api/elections/{api_world.hidden_id}/earlier").json()
    assert gen["count"] > 0 and gen["not_created_count"] > 0
