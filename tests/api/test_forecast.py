"""Forecast runner (service) and forecast endpoints (background job on a small run)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services.forecast_runner import run_forecast_for_election


def _wait(c: TestClient, job_id: str, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = c.get(f"/api/forecast/jobs/{job_id}").json()
        if st["status"] in ("completed", "failed"):
            return st
        time.sleep(0.1)
    raise AssertionError(f"forecast job {job_id} did not finish")


def test_forecast_job_and_views(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    assert rw_client.get(f"/api/forecast/{e}/latest").status_code == 404
    r = rw_client.post(f"/api/forecast/{e}/run", json={"simulations": 300, "seed": 11, "workers": 1})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["status"] in ("queued", "running", "completed") and job["status_url"].endswith(job["job_id"])
    st = _wait(rw_client, job["job_id"])
    assert st["status"] == "completed", st
    assert st["run_id"] and st["progress"] == 1.0
    latest = rw_client.get(f"/api/forecast/{e}/latest").json()
    assert latest["data_category"] == "SIMULATED" and latest["disclaimer"]
    assert latest["run"]["id"] == st["run_id"] and latest["run"]["seed"] == 11
    assert (
        latest["run"]["n_simulations"] == 300 and latest["run"]["polls"]["poll_type"] == "national_president"
    )
    pres = latest["president"]
    assert pres["total_ev"] == 174 and pres["majority"] == 88
    probs = sum(t["prob_win"] for t in pres["tickets"]) + pres["prob_contingent"]
    assert abs(probs - 1.0) < 1e-6
    assert len(pres["provinces"]) == 12 and pres["combinations"]
    assert len(pres["tickets"][0]["ev_histogram"]) == 175
    assert latest["house"]["seats_total"] == 150 and latest["house"]["races"]
    assert latest["senate"]["seats_total"] == 24
    assert "races" not in latest
    full = rw_client.get(f"/api/forecast/runs/{st['run_id']}?include=races").json()
    assert full["races"]
    runs = rw_client.get(f"/api/forecast/{e}/runs").json()["runs"]
    assert [x["run_id"] for x in runs] == [st["run_id"]]
    assert any(j["job_id"] == job["job_id"] for j in rw_client.get("/api/forecast/jobs").json()["jobs"])
    exp = rw_client.get(f"/api/export/{e}/montecarlo_summary.csv")
    assert exp.status_code == 200 and exp.text.splitlines()[0].startswith("run_id,election_id,seed")


def test_forecast_validation(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    assert rw_client.post(f"/api/forecast/{e}/run", json={"simulations": 0}).status_code == 422
    assert rw_client.post(f"/api/forecast/{e}/run", json={"simulations": 10**9}).status_code == 422
    assert rw_client.get("/api/forecast/jobs/nope").status_code == 404
    assert rw_client.get("/api/forecast/runs/424242").status_code == 404


def test_run_forecast_for_election_is_deterministic(rw_client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine(f"sqlite:///{rw_client.db_path}")  # type: ignore[attr-defined]
    s = sessionmaker(bind=engine)()
    try:
        a = run_forecast_for_election(s, api_world.hidden_id, 200, 5, workers=1)
        b = run_forecast_for_election(s, api_world.hidden_id, 200, 5, workers=1, use_polls=True)
        c = run_forecast_for_election(s, api_world.hidden_id, 200, 5, workers=1, use_polls=False)
        s.commit()
        import json

        ra, rb, rc = (json.loads(x.summary_json) for x in (a, b, c))
        assert a.kind == "forecast" and a.seed == 5 and a.n_simulations == 200
        assert ra["president"] == rb["president"]
        assert ra["metadata"]["polls"]["used"] is True and rc["metadata"]["polls"]["used"] is False
        assert ra["president"]["tickets"] != rc["president"]["tickets"]
    finally:
        s.close()
        engine.dispose()
