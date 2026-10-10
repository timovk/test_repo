"""The world clock API: today, the agenda, next election day, watch / count (a background job)
and the news; plus the custom people endpoint."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services.read import clock as clock_read


def _wait(client: TestClient) -> dict:
    clock_read.wait_for_job(600)
    job = client.get("/api/clock/job").json()["job"]
    assert job is not None and job["status"] == "done", job
    return job


def test_rolling_the_clock(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    c = rw_client
    st = c.get("/api/clock").json()
    # unset clock: today is the earliest unfinished election's day (the sandbox's 2028 election)
    assert st["today"].startswith("2028-11") and st["elections_today"] and not st["can_advance"]
    assert st["next"] is not None and st["next"]["date"] > st["today"]
    blocked = c.post("/api/clock/next")
    assert blocked.status_code == 409 and "not finished yet" in blocked.json()["detail"]

    assert c.post("/api/clock/count").status_code == 202
    job = _wait(c)
    assert job["kind"] == "count" and job["result"]["counted"] == [api_world.hidden_id]
    st = c.get("/api/clock").json()
    assert st["can_advance"] and st["elections_today"][0]["status"] == "final"
    assert c.post("/api/clock/count").status_code == 409  # nothing left to count

    moved = c.post("/api/clock/next")
    assert moved.status_code == 200, moved.text
    body = moved.json()
    arrived = body["arrived"]
    assert body["today"] == arrived["today"] == st["next"]["date"]
    el = arrived["election"]
    assert el["local"] and el["status"] == "scheduled" and el["provinces"]
    assert isinstance(arrived["news"], list)
    w = c.post("/api/clock/watch").json()
    assert w["election_id"] == el["id"] and w["elections_today"][0]["status"] == "simulated"
    detail = c.get(f"/api/elections/{el['id']}").json()
    assert detail["night"]["needs_simulation"] is False

    agenda = c.get("/api/clock/agenda").json()
    whens = {d["when"] for d in agenda["days"]}
    assert "today" in whens and "upcoming" in whens
    today = next(d for d in agenda["days"] if d["when"] == "today")
    assert today["election_id"] == el["id"] and not today["planned"]
    news = c.get("/api/clock/news").json()
    assert news["count"] >= 1 and any(i["kind"] == "result" for i in news["items"])
    assert c.post("/api/clock/skip", json={"date": "2020-01-01"}).status_code == 409


def test_people_endpoint(client: TestClient) -> None:
    data = client.get("/api/people").json()
    assert data["error"] is None and isinstance(data["people"], list) and data["file"].endswith("people.yaml")
