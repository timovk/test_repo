"""System endpoints: health, meta, settings, provenance, validation, docs, errors, static UI."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.api.main import create_app


def test_health_ready(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    h = r.json()
    assert h["status"] == "ok" and h["ready"] is True
    assert h["database"]["schema"] is True
    assert h["geography"]["loaded"] is True and h["geography"]["source"] == "synthetic"
    assert h["elections"] == 2
    assert {"version", "night_service", "ui_built"} <= set(h)


def test_meta_constitution_and_elections(client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    m = client.get("/api/meta").json()
    c = m["constitution"]
    assert (c["electoral_votes"], c["presidential_majority"]) == (174, 88)
    assert (c["house_seats"], c["house_majority"], c["senate_seats"], c["senate_majority"]) == (
        150,
        76,
        24,
        13,
    )
    assert c["labels"] == {"president": "88 TO WIN", "house": "76 FOR CONTROL", "senate": "13 FOR CONTROL"}
    assert c["data_category"] == "FICTIONAL"
    ids = [e["id"] for e in m["elections"]]
    assert ids == [api_world.final_id, api_world.hidden_id]
    assert m["demo_election_id"] == api_world.hidden_id
    assert m["latest_election_id"] == api_world.hidden_id
    assert {d["key"] for d in m["data_categories"]} == {"REAL", "DERIVED", "FICTIONAL", "SIMULATED"}
    assert m["active"]["plan"]["total_districts"] == 150
    assert {p["code"] for p in m["parties"]} >= {"PA", "VLP"}
    assert m["night"]["speeds"]


def test_settings_round_trip(rw_client: TestClient) -> None:
    s = rw_client.get("/api/settings").json()
    assert s["theme"] == "dark" and s["party_colors"] == {}
    assert "speeds" in s["options"]
    r = rw_client.put("/api/settings", json={"theme": "light", "party_colors": {"PA": "#112233"}})
    assert r.status_code == 200, r.text
    assert r.json()["theme"] == "light" and r.json()["party_colors"] == {"PA": "#112233"}
    again = rw_client.get("/api/settings").json()
    assert again["theme"] == "light" and again["party_colors"]["PA"] == "#112233"


def test_settings_validation(rw_client: TestClient) -> None:
    bad = [
        {"party_colors": {"PA": "red"}},
        {"party_colors": {"NOPE": "#112233"}},
        {"playback_speed": 3.7},
        {"theme": "purple"},
        {"default_election_id": 999},
    ]
    for body in bad:
        r = rw_client.put("/api/settings", json=body)
        assert r.status_code == 422, (body, r.text)
        assert r.json()["code"] == "invalid"


def test_provenance(client: TestClient) -> None:
    p = client.get("/api/data/provenance").json()
    assert p["real"]["data_category"] == "REAL"
    assert p["derived"]["transforms"] and p["fictional"]["constructs"] and p["simulated"]["outputs"]
    assert p["fictional"]["counts"]["parties"] >= 8
    assert p["simulated"]["counts"]["elections"] == 2


def test_validation_report(client: TestClient) -> None:
    v = client.get("/api/data/validation").json()
    assert {"ok", "errors", "warnings", "checks"} <= set(v)
    assert any(c["name"] == "electoral votes total" for c in v["checks"])


def test_openapi_docs(client: TestClient) -> None:
    assert client.get("/api/docs").status_code == 200
    spec = client.get("/api/openapi.json").json()
    assert "/api/elections/{election_id}/president" in spec["paths"]
    assert "/api/night/{election_id}/stream" in spec["paths"]


def test_unknown_api_path_is_json_404(client: TestClient) -> None:
    r = client.get("/api/does/not/exist")
    assert r.status_code == 404
    assert r.json() == {"detail": "no API endpoint GET /api/does/not/exist", "code": "not_found"}
    r = client.post("/api/nothing", json={})
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_spa_fallback(client: TestClient) -> None:
    r = client.get("/some/client/route")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_placeholder_without_ui(tmp_path: Path, api_world) -> None:  # type: ignore[no-untyped-def]
    ui = tmp_path / "ui"
    ui.mkdir()
    app = create_app(f"sqlite:///{api_world.path}", ui_dir=ui, configure_logs=False)
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200 and "/api/docs" in r.text and "being built" in r.text
        assert c.get("/anything/else").status_code == 200
        assert c.get("/api/health").json()["ui_built"] is False


def test_unprepared_database_does_not_crash(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    app = create_app(f"sqlite:///{missing}", configure_logs=False)
    with TestClient(app) as c:
        h = c.get("/api/health").json()
        assert h["status"] == "ok" and h["ready"] is False
        assert h["database"]["exists"] is False
        r = c.get("/api/meta")
        assert r.status_code == 503 and r.json()["code"] == "not_prepared"
    assert not missing.exists()


def test_gzip(client: TestClient) -> None:
    r = client.get("/api/districts", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
