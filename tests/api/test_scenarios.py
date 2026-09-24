"""Scenario editor endpoints: list / get / validate / create / update / duplicate / export /
delete, with the file and the database row written together."""

from __future__ import annotations

import sqlite3

import yaml
from fastapi.testclient import TestClient


def test_list_and_get(client: TestClient) -> None:
    lst = client.get("/api/scenarios").json()
    builtin = {s["slug"]: s for s in lst["scenarios"] if s["source"] == "builtin"}
    assert {"founding-2024", "demo-2028"} <= set(builtin)
    assert builtin["demo-2028"]["read_only"] is True and builtin["founding-2024"]["elections"]
    assert "set_seed" in lst["operations"]
    g = client.get("/api/scenarios/demo-2028").json()
    assert g["slug"] == "demo-2028" and g["yaml"].strip() and g["document"]["scenario"]["slug"] == "demo-2028"
    assert g["validation"]["geography_checked"] is True
    assert client.get("/api/scenarios/does-not-exist").status_code == 404
    assert client.get("/api/scenarios/..%2Fetc").status_code in (404, 422)


def test_validate(client: TestClient) -> None:
    ok = client.post(
        "/api/scenarios/validate", json={"base": "demo-2028", "edits": [{"op": "set_seed", "value": 5}]}
    )
    body = ok.json()
    assert ok.status_code == 200 and body["schema_ok"] is True and body["changes"]
    assert body["document"]["scenario"]["seed"] == 5
    bad = client.post("/api/scenarios/validate", json={"yaml": "scenario: {slug: 'X Y'}\n"}).json()
    assert bad["ok"] is False and bad["schema_ok"] is False and bad["errors"]
    op = client.post(
        "/api/scenarios/validate", json={"base": "demo-2028", "edits": [{"op": "no_such_op"}]}
    ).json()
    assert op["schema_ok"] is False


def test_create_update_duplicate_export_delete(rw_client: TestClient) -> None:
    doc = rw_client.get("/api/scenarios/demo-2028").json()["document"]
    doc["scenario"]["slug"] = "my-2028"
    doc["scenario"]["name"] = "My 2028"
    r = rw_client.post("/api/scenarios", json={"document": doc})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["source"] == "user" and created["read_only"] is False
    path = rw_client.scenario_dir / "my-2028.yaml"  # type: ignore[attr-defined]
    assert path.exists()
    assert rw_client.post("/api/scenarios", json={"document": doc}).status_code == 409
    up = rw_client.put("/api/scenarios/my-2028", json={"edits": [{"op": "set_seed", "value": 77}]})
    assert up.status_code == 200, up.text
    assert up.json()["seed"] == 77 and up.json()["changes"]
    assert yaml.safe_load(path.read_text())["scenario"]["seed"] == 77
    con = sqlite3.connect(rw_client.db_path)  # type: ignore[attr-defined]
    try:
        row = con.execute("select seed, document_hash from scenario where slug = 'my-2028'").fetchone()
    finally:
        con.close()
    assert row[0] == 77 and row[1] == up.json()["hash"]
    assert (
        rw_client.put(
            "/api/scenarios/demo-2028", json={"edits": [{"op": "set_seed", "value": 1}]}
        ).status_code
        == 409
    )
    dup = rw_client.post("/api/scenarios/my-2028/duplicate", json={"new_slug": "my-copy", "seed": 3})
    assert dup.status_code == 201 and dup.json()["seed"] == 3
    exp = rw_client.get("/api/scenarios/my-copy/export")
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
    assert yaml.safe_load(exp.text)["scenario"]["slug"] == "my-copy"
    assert rw_client.delete("/api/scenarios/my-copy").status_code == 200
    assert rw_client.get("/api/scenarios/my-copy").status_code == 404
    assert rw_client.delete("/api/scenarios/demo-2028").status_code == 409
    listing = {s["slug"]: s["source"] for s in rw_client.get("/api/scenarios").json()["scenarios"]}
    assert listing["my-2028"] == "user" and "my-copy" not in listing


def test_import_yaml_with_slug_override(rw_client: TestClient) -> None:
    text = rw_client.get("/api/scenarios/founding-2024/export").text
    r = rw_client.post("/api/scenarios", json={"yaml": text, "slug": "imported-2024"})
    assert r.status_code == 201, r.text
    assert r.json()["slug"] == "imported-2024"
    bad = rw_client.post("/api/scenarios", json={"yaml": "not: [valid"})
    assert bad.status_code == 422 and bad.json()["code"] == "invalid"
    both = rw_client.post("/api/scenarios", json={})
    assert both.status_code == 422
