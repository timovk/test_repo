"""Election list / summary and every results page of a reported (FINAL) election."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def eid(api_world) -> int:  # type: ignore[no-untyped-def]
    return api_world.final_id


def test_list_and_aliases(client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    lst = client.get("/api/elections").json()
    assert lst["count"] == 2
    first, second = lst["elections"]
    assert first["status"] == "final" and first["headline"]["president"]["winner"]
    assert second["status"] == "simulated" and second["headline"] is None
    assert second["results_source"] == "hidden" and first["results_source"] == "final"
    assert client.get("/api/elections/latest").json()["election"]["id"] == api_world.hidden_id
    assert client.get("/api/elections/demo").json()["election"]["id"] == api_world.hidden_id
    d = client.get(f"/api/elections/{api_world.final_id}").json()
    assert d["results"]["president"]["winner"] and d["contents"]["president"] is True
    assert d["next_election_id"] == api_world.hidden_id
    assert d["constitution"]["presidential_majority"] == 88


def test_unknown_election(client: TestClient) -> None:
    for ref in ("999", "abc"):
        r = client.get(f"/api/elections/{ref}/president")
        assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_president_final(client: TestClient, eid: int) -> None:
    p = client.get(f"/api/elections/{eid}/president").json()
    assert p["results_source"] == "final" and p["data_category"] == "SIMULATED"
    assert (p["electoral_votes_total"], p["majority"], p["label"]) == (174, 88, "88 TO WIN")
    assert sum(t["electoral_votes"] for t in p["tickets"]) == 174
    assert sum(t["votes"] for t in p["tickets"]) == p["popular_vote"]["total_valid"]
    t0 = p["tickets"][0]
    assert {
        "key",
        "name",
        "party",
        "color",
        "president",
        "running_mate",
        "votes",
        "pct",
        "electoral_votes",
    } <= set(t0)
    assert t0["president"]["portrait_key"] and t0["running_mate"]["name"]
    w = p["winner"]
    assert w["key"] == t0["key"] and w["electoral_votes"] >= 88
    assert p["tipping_point"]["province_code"] in {x["code"] for x in p["provinces"]}
    assert p["divergence"]["diverged"] in (True, False)
    assert len(p["provinces"]) == 12 and all(x["winner"] for x in p["provinces"])
    assert p["electoral_vote_margin"] >= 0


def test_electoral_college(client: TestClient, eid: int) -> None:
    ec = client.get(f"/api/elections/{eid}/electoral-college").json()
    assert len(ec["path"]) == 12 and ec["path"][-1]["cumulative_ev"] <= 174
    assert sum(1 for x in ec["path"] if x["is_tipping_point"]) == 1
    assert abs(sum(t["efficiency_pp"] for t in ec["tickets"])) < 1e-3
    assert ec["closest"]["margin_pp"] <= ec["largest_victory"]["margin_pp"]


def test_provinces_results(client: TestClient, eid: int) -> None:
    p = client.get(f"/api/elections/{eid}/provinces").json()
    assert p["race"] == "PRES" and len(p["provinces"]) == 12
    row = p["provinces"][0]
    assert {"code", "ev", "winner", "margin_pp", "turnout_pct", "status", "flip_status", "pct"} <= set(row)
    g = client.get(f"/api/elections/{eid}/provinces?race=GOV").json()
    assert g["race"] == "GOV" and len(g["provinces"]) == 12
    assert client.get(f"/api/elections/{eid}/provinces?race=HOUSE").status_code == 422
    assert client.get(f"/api/elections/{eid}/provinces?race=NOPE").status_code == 422


def test_province_page(client: TestClient, eid: int) -> None:
    p = client.get(f"/api/elections/{eid}/provinces/NB?sort=margin_pp&order=asc").json()
    assert p["province"]["code"] == "NB" and p["headline"]["code"] == "PRES-NB"
    rows = p["municipalities"]["rows"]
    assert rows and p["municipalities"]["sort"] == "margin_pp"
    margins = [r["margin_pp"] for r in rows]
    assert margins == sorted(margins)
    assert {
        "population",
        "votes",
        "reporting_pct",
        "margin_pp",
        "shares",
        "outstanding_est",
        "swing_pp",
    } <= set(rows[0])
    assert p["races"]["house"] and len(p["races"]["senate"]) == 2 and p["races"]["governor"]
    assert p["outstanding"]["votes_est"] == 0
    assert client.get(f"/api/elections/{eid}/provinces/NB?sort=bogus").status_code == 422


def test_municipality_rows(client: TestClient, eid: int) -> None:
    n = client.get("/api/municipalities").json()["count"]
    m = client.get(f"/api/elections/{eid}/municipalities").json()
    assert m["count"] == n and m["keyed_by"] == "party"
    row = m["municipalities"][0]
    assert {"code", "leader", "leader_color", "margin_pp", "shares", "turnout_pct", "reporting_pct"} <= set(
        row
    )
    assert abs(sum(row["shares"].values()) - 100.0) < 0.05
    h = client.get(f"/api/elections/{eid}/municipalities?race=HOUSE&province=NB").json()
    assert h["race"] == "HOUSE" and all(r["province_code"] == "NB" for r in h["municipalities"])
    one = client.get(f"/api/elections/{eid}/municipalities?race=HOUSE-NB-01").json()
    assert one["count"] >= 1
    assert client.get(f"/api/elections/{eid}/municipalities?race=HOUSE-ZZ-99").status_code == 404


def test_municipality_page(client: TestClient, eid: int) -> None:
    code = client.get("/api/municipalities?province=UT").json()["municipalities"][0]["code"]
    p = client.get(f"/api/elections/{eid}/municipalities/{code}").json()
    codes = [r["code"] for r in p["races"]]
    assert codes[0] == "PRES" and any(c.startswith("HOUSE-") for c in codes) and "GOV-UT" in codes
    assert all(r["municipal"]["total_votes"] > 0 for r in p["races"])
    assert p["precinct_race"] == "PRES" and p["precincts"]
    u = p["precincts"][0]
    assert {"code", "name", "votes", "pct", "turnout_pct", "leader"} <= set(u)
    gov = client.get(f"/api/elections/{eid}/municipalities/{code}?race=GOV-UT").json()
    assert gov["precinct_race"] == "GOV-UT" and gov["precincts"]


def test_house(client: TestClient, eid: int) -> None:
    h = client.get(f"/api/elections/{eid}/house").json()
    assert (h["seats_total"], h["majority"], h["label"]) == (150, 76, "76 FOR CONTROL")
    assert len(h["districts"]) == 150 and h["called"] == 150
    assert sum(p["won"] for p in h["by_party"]) == 150
    assert abs(sum(p["vote_pct"] for p in h["by_party"]) - 100.0) < 0.05
    d = h["districts"][0]
    assert {
        "code",
        "race_code",
        "name",
        "status",
        "winner",
        "winner_party",
        "margin_pp",
        "incumbent",
        "open_seat",
        "flip_status",
        "top",
    } <= set(d)
    assert h["control"]["label"]
    det = client.get(f"/api/elections/{eid}/house/{d['code']}").json()
    assert det["race"]["code"] == d["race_code"] and det["district"]["code"] == d["code"]
    assert det["municipalities"] and det["history"]
    assert client.get(f"/api/elections/{eid}/house/ZZ-01").status_code == 404


def test_senate_governors_mayors(client: TestClient, eid: int) -> None:
    s = client.get(f"/api/elections/{eid}/senate").json()
    assert s["seats_total"] == 24 and len(s["seats"]) == 24 and s["label"] == "13 FOR CONTROL"
    assert s["up"] == 24 and sum(s["composition"]["projected"].values()) == 24
    seat = s["seats"][0]
    assert seat["up"] and seat["race"]["lines"] and seat["projected_winner"]
    g = client.get(f"/api/elections/{eid}/governors").json()
    assert g["races"] == 12 and all(x["winner"] for x in g["governors"])
    assert g["governors"][0]["lines"][0]["running_mate_name"]
    m = client.get(f"/api/elections/{eid}/mayors").json()
    assert m["races"] == 0 and m["mayors"] == []


def test_race_detail(client: TestClient, eid: int) -> None:
    r = client.get(f"/api/elections/{eid}/races/pres-nb").json()
    race = r["race"]
    assert race["code"] == "PRES-NB" and race["winner"] and race["electoral_votes"]
    assert r["municipalities"] and "recounts" in r and "calls" in r
    pres = client.get(f"/api/elections/{eid}/races/PRES").json()
    assert len(pres["provinces"]) == 12 and sum(pres["electoral_votes"].values()) == 174
    with_recount = client.get(f"/api/elections/{eid}/races/SEN-FL-1").json()
    assert isinstance(with_recount["recounts"], list)
    assert client.get(f"/api/elections/{eid}/races/NOPE-1").status_code == 404


def test_calls_and_timeline(client: TestClient, eid: int) -> None:
    c = client.get(f"/api/elections/{eid}/calls").json()
    assert c["count"] == len(c["calls"])
    t = client.get(f"/api/elections/{eid}/timeline").json()
    assert t["total_events"] == t["revealed_events"] > 0
    b = t["buckets"]
    assert b and b[-1]["cumulative_pct"] == pytest.approx(100.0, abs=1e-6)
    assert sum(x["events"] for x in b) == t["total_events"]
    ev = client.get(f"/api/elections/{eid}/timeline?detail=events&limit=5&offset=2").json()
    assert len(ev["events"]) == 5 and ev["events"][0]["seq"] == 3
    assert client.get(f"/api/elections/{eid}/timeline?detail=bogus").status_code == 422
