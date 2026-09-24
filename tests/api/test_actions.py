"""Write actions: create / simulate / finalize elections, manual polls, campaign what-ifs, and
the history and analytics that become available once two elections are reported."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_finalize_then_history_and_analytics(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    assert rw_client.get("/api/history/compare").status_code == 404
    r = rw_client.post(f"/api/elections/{e}/finalize")
    assert r.status_code == 200, r.text
    assert r.json()["election"]["status"] == "final" and r.json()["results"]["president"]["winner"]
    assert rw_client.post(f"/api/elections/{e}/finalize").status_code == 409
    assert rw_client.post(f"/api/elections/{e}/simulate").status_code == 409
    p = rw_client.get(f"/api/elections/{e}/president").json()
    assert p["results_source"] == "final" and p["provinces"][0]["flip_status"] in ("hold", "flip", "new")
    cmp = rw_client.get("/api/history/compare").json()
    assert cmp["a"]["id"] == api_world.final_id and cmp["b"]["id"] == e and cmp["level"] == "municipality"
    assert cmp["rows"] and cmp["national"] and cmp["flips_summary"]
    assert {"geo_code", "winner_a", "winner_b", "flip_status", "swing", "turnout_change_pp"} <= set(
        cmp["rows"][0]
    )
    house = rw_client.get("/api/history/compare?race=HOUSE&level=district").json()
    assert len(house["rows"]) == 150
    prov = rw_client.get(f"/api/history/compare?a={api_world.final_id}&b={e}&level=province").json()
    assert len(prov["rows"]) == 12
    assert rw_client.get("/api/history/compare?level=bogus").status_code == 422
    same = rw_client.get(f"/api/history/compare?a={e}&b={e}")
    assert same.status_code == 422 and same.json()["code"] == "invalid"
    summ = rw_client.get("/api/history/summary").json()
    assert summ["count"] == 2 and summ["elections"][1]["president"]["popular_vote"]["winner_pct"]
    div = rw_client.get("/api/history/divergence").json()
    assert div["count"] == 2
    series = rw_client.get("/api/history/province/NB").json()
    assert len(series["points"]) == 2 and "change_pp" in series["points"][1]
    code = cmp["rows"][0]["geo_code"]
    assert len(rw_client.get(f"/api/history/municipality/{code}").json()["points"]) == 2
    dist = rw_client.get("/api/history/district/NB-01").json()
    assert dist["points"][0]["status"] == "first"
    an = rw_client.get(f"/api/analytics/{e}").json()
    assert an["available"] is True
    assert an["elasticity"]["available"] is True and an["elasticity"]["provinces"]
    assert an["lean"]["provinces"] and an["ec_efficiency"] and an["tipping_point"]["province_code"]
    assert an["competitiveness"]["by_race_type"] and an["seat_vote"] and an["efficiency_gap"]
    closest = rw_client.get("/api/history/closest?n=3&race_type=HOUSE").json()
    assert closest["count"] == 3 and all(x["race_type"] == "HOUSE" for x in closest["races"])
    assert rw_client.get("/api/history/closest?race_type=NOPE").status_code == 422
    parties = rw_client.get("/api/parties").json()["parties"]
    assert any(len(p["record"]) == 2 for p in parties)
    avg = rw_client.get(f"/api/polls/{e}/average").json()
    assert avg["average"]["accuracy"]["error_pp"]
    camp = rw_client.get(f"/api/campaigns/{e}").json()
    assert camp["effects_revealed"] is True
    assert any(t["realized_effect"] is not None for c in camp["campaigns"] for t in c["targets"])


def test_create_and_simulate_election(rw_client: TestClient) -> None:
    r = rw_client.post("/api/elections", json={"scenario": "midterm-2026", "seed": 42})
    assert r.status_code == 201, r.text
    body = r.json()
    new = body["election"]["id"]
    assert body["election"]["status"] == "scheduled" and body["seed"] == 42
    assert rw_client.get(f"/api/elections/{new}/house").json()["results_source"] == "hidden"
    s = rw_client.post(f"/api/elections/{new}/simulate", json={"seed": 43})
    assert s.status_code == 200 and s.json()["election"]["status"] == "simulated"
    assert rw_client.post("/api/elections", json={"scenario": "no-such-scenario"}).status_code == 404
    assert rw_client.post("/api/elections", json={"seed": 1}).status_code == 422


def test_manual_poll(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    before = rw_client.get(f"/api/polls/{e}").json()["total"]
    body = {
        "pollster": "Peilpunt Test Research",
        "poll_type": "national_president",
        "geo_code": "NL",
        "start_date": "2028-10-20",
        "end_date": "2028-10-24",
        "sample_size": 1500,
        "results": {"PA": 30.0, "VLP": 28.0, "NVB": 15.0},
        "undecided_pct": 8.0,
    }
    r = rw_client.post(f"/api/polls/{e}", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["id"] and r.json()["data_category"] == "SIMULATED"
    assert rw_client.get(f"/api/polls/{e}").json()["total"] == before + 1
    late = {**body, "end_date": "2029-01-01"}
    assert rw_client.post(f"/api/polls/{e}", json=late).status_code == 422
    assert rw_client.post(f"/api/polls/{e}", json={**body, "poll_type": "horoscope"}).status_code == 422
    assert rw_client.post(f"/api/polls/{e}", json={**body, "results": {"PA": 150}}).status_code == 422
    assert rw_client.post(f"/api/polls/{api_world.final_id}", json=body).status_code == 409


def test_campaign_plan_preview(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    stored = rw_client.get(f"/api/campaigns/{e}").json()
    r = rw_client.post(f"/api/campaigns/{e}/plan", json={"seed": 9, "budgets": {"PA": 150.0}})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["preview"] is True and plan["stored"] is False and plan["seed"] == 9
    pa = next(c for c in plan["campaigns"] if c["party"] == "PA")
    assert pa["budget"] == 150.0 and pa["targets"] and pa["by_action"]
    assert all(t["realized_effect"] is None for t in pa["targets"])
    assert rw_client.get(f"/api/campaigns/{e}").json() == stored
    assert rw_client.post(f"/api/campaigns/{e}/plan", json={"budgets": {"ZZZ": 1}}).status_code == 422


def test_candidates(client: TestClient) -> None:
    lst = client.get("/api/candidates?search=an&limit=5").json()
    assert lst["total"] >= len(lst["candidates"]) > 0 and lst["limit"] == 5
    cand = lst["candidates"][0]
    assert {"id", "name", "party", "portrait_key", "offices", "races"} <= set(cand)
    prof = client.get(f"/api/candidates/{cand['id']}").json()
    assert prof["id"] == cand["id"] and "candidacies" in prof and "offices" in prof
    serving = client.get("/api/candidates?office=PRES").json()
    assert serving["total"] == 1 and serving["candidates"][0]["offices"] == ["PRES"]
    assert client.get("/api/candidates/999999").status_code == 404
    assert client.get("/api/candidates?limit=0").status_code == 422
