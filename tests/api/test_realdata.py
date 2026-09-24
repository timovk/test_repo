"""Read-only smoke test of the API against the real database ``data/nlfed.db`` (skipped when it
does not exist or holds no elections; ``NLFED_TEST_REAL_DB`` points the test at another copy).
Only GET endpoints that never write are called."""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.settings import PROJECT_ROOT

pytestmark = pytest.mark.realdata

DB = Path(os.environ.get("NLFED_TEST_REAL_DB") or PROJECT_ROOT / "data" / "nlfed.db")


def _usable() -> bool:
    if not DB.exists() or DB.stat().st_size < 1_000_000:
        return False
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            n = con.execute("select count(*) from election").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return n > 0


@pytest.fixture(scope="module")
def real_client() -> TestClient:
    if not _usable():
        pytest.skip("data/nlfed.db with elections not available")
    from app.api.main import create_app

    return TestClient(create_app(f"sqlite:///{DB}", configure_logs=False))


def test_real_api_smoke(real_client: TestClient) -> None:
    with real_client as c:
        h = c.get("/api/health").json()
        assert h["ready"] is True and h["geography"]["source"] == "store"
        meta = c.get("/api/meta").json()
        assert meta["constitution"]["electoral_votes"] == 174
        assert meta["active"]["vintage"]["municipalities"] == 342
        assert len(c.get("/api/provinces").json()["provinces"]) == 12
        assert c.get("/api/municipalities").json()["count"] == 342
        assert c.get("/api/districts").json()["count"] == 150
        assert c.get("/api/geo/provinces.geojson").status_code == 200
        elections = c.get("/api/elections").json()["elections"]
        reported = [e for e in elections if e["reported"]]
        for e in elections:
            t0 = time.perf_counter()
            r = c.get(f"/api/elections/{e['id']}")
            assert r.status_code == 200
            assert time.perf_counter() - t0 < 10.0
        if not reported:
            return
        eid = next((e["id"] for e in reversed(reported) if e["contents"]["president"]), reported[-1]["id"])
        pages = [
            f"/api/elections/{eid}/provinces",
            f"/api/elections/{eid}/municipalities",
            f"/api/elections/{eid}/house",
            f"/api/elections/{eid}/senate",
            f"/api/elections/{eid}/timeline",
            f"/api/elections/{eid}/provinces/NB",
            f"/api/elections/{eid}/municipalities/GM0855",
            "/api/history/summary",
            f"/api/analytics/{eid}",
        ]
        if any(e["id"] == eid and e["contents"]["president"] for e in elections):
            pages.append(f"/api/elections/{eid}/president")
        for page in pages:
            t0 = time.perf_counter()
            r = c.get(page)
            dt = time.perf_counter() - t0
            assert r.status_code == 200, (page, r.text[:300])
            assert dt < 30.0, (page, dt)
        rows = c.get(f"/api/elections/{eid}/municipalities").json()["municipalities"]
        assert len(rows) == 342
