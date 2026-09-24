"""Geography, apportionment, districts, Senate seats and GeoJSON endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_provinces(client: TestClient) -> None:
    p = client.get("/api/provinces").json()
    assert p["data_category"] == "REAL" and p["provenance"]["house_seats"] == "FICTIONAL"
    assert len(p["provinces"]) == 12
    assert p["totals"]["house_seats"] == 150 and p["totals"]["electoral_votes"] == 174
    row = p["provinces"][0]
    assert {"code", "name", "population", "house_seats", "electoral_votes", "governor", "centroid"} <= set(
        row
    )
    assert all(r["electoral_votes"] == r["house_seats"] + 2 for r in p["provinces"])


def test_province_detail(client: TestClient) -> None:
    d = client.get("/api/provinces/nb").json()
    assert d["code"] == "NB"
    assert d["districts"] and all(x["code"].startswith("NB-") for x in d["districts"])
    assert len(d["senate_seats"]) == 2 and d["demographics"]
    assert d["governor"]["office"] == "GOV-NB"
    assert client.get("/api/provinces/XX").status_code == 404


def test_municipalities(client: TestClient) -> None:
    allm = client.get("/api/municipalities").json()
    nb = client.get("/api/municipalities?province=NB").json()
    assert allm["count"] == len(allm["municipalities"]) > nb["count"] > 0
    m = nb["municipalities"][0]
    assert {
        "code",
        "name",
        "province_code",
        "population",
        "demographics",
        "imputed_fields",
        "unit_count",
    } <= set(m)
    assert set(m["demographics"]) >= {"pct_age_65_plus", "income_per_capita_keur"}
    assert client.get("/api/municipalities?province=ZZ").status_code == 404


def test_municipality_detail(client: TestClient) -> None:
    code = client.get("/api/municipalities").json()["municipalities"][0]["code"]
    d = client.get(f"/api/municipalities/{code}").json()
    assert d["code"] == code and d["units"] and d["districts"]
    assert abs(sum(x["share_of_municipality"] for x in d["districts"]) - 1.0) < 1e-3
    assert all(u["district_code"] for u in d["units"])
    assert client.get("/api/municipalities/GM9999").status_code == 404


def test_apportionment(client: TestClient) -> None:
    a = client.get("/api/apportionment").json()
    assert a["total_seats"] == 150 and a["total_electoral_votes"] == 174
    assert sum(p["seats"] for p in a["provinces"]) == 150
    assert len(a["priority_list"]) == 150 - 12
    assert a["method_comparison"] and "huntington_hill" in a["method_comparison"][0]


def test_districts_and_plan(client: TestClient) -> None:
    d = client.get("/api/districts").json()
    assert d["count"] == 150 and d["data_category"] == "FICTIONAL"
    row = d["districts"][0]
    assert {"code", "name", "population", "deviation_pct", "holder", "polsby_popper"} <= set(row)
    assert client.get("/api/districts?province=GR").json()["count"] < 150
    plan = client.get("/api/districts/plan").json()
    assert plan["validation"]["total_districts"] == 150 and plan["deviation"]["max_abs_pct"] is not None
    assert len(plan["provinces"]) == 12
    det = client.get(f"/api/districts/{row['code']}").json()
    assert det["municipalities"] and det["history"] and det["neighbours"]
    assert client.get("/api/districts/ZZ-99").status_code == 404


def test_senate_seats(client: TestClient) -> None:
    s = client.get("/api/senate/seats").json()
    assert len(s["seats"]) == 24 and s["label"] == "13 FOR CONTROL"
    assert s["classes"] == {"1": 8, "2": 8, "3": 8}
    assert sum(s["composition"].values()) == 24
    assert all(x["next_election_year"] for x in s["seats"])


def test_geojson_layers(client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    for layer in ("provinces", "municipalities", "districts"):
        r = client.get(f"/api/geo/{layer}.geojson")
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("application/geo+json")
        assert "etag" in r.headers and "max-age" in r.headers["cache-control"]
        fc = r.json()
        assert fc["type"] == "FeatureCollection" and fc["features"]
        props = fc["features"][0]["properties"]
        assert {"code", "name", "province_code"} <= set(props)
        again = client.get(f"/api/geo/{layer}.geojson", headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304
    fc = client.get("/api/geo/districts.geojson").json()
    assert len(fc["features"]) == 150
    assert {"population", "deviation_pct"} <= set(fc["features"][0]["properties"])
    cached = list((api_world.data_dir / "processed").rglob("districts_*.geojson"))
    assert cached, "districts layer is cached as a file"


def test_geojson_errors(client: TestClient) -> None:
    assert client.get("/api/geo/rivers.geojson").status_code == 404
    r = client.get("/api/geo/units/NB.geojson")
    assert r.status_code == 503 and r.json()["code"] == "not_prepared"
