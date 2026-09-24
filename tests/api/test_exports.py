"""Stable-schema exports: CSV headers and JSON envelopes follow the export schema registry."""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.export.schemas import get_schema

REPORTED_DATASETS = [
    "national",
    "provinces",
    "municipalities",
    "district_lines",
    "house",
    "senate",
    "governors",
    "electoral_votes",
    "timeline",
    "calls",
    "polls",
    "polling_averages",
    "districts",
    "apportionment",
]


@pytest.mark.parametrize("dataset", REPORTED_DATASETS)
def test_csv_schema(client: TestClient, api_world, dataset: str) -> None:  # type: ignore[no-untyped-def]
    r = client.get(f"/api/export/{api_world.final_id}/{dataset}.csv")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == list(get_schema(dataset).names)
    if dataset not in ("calls",):
        assert len(rows) > 1


@pytest.mark.parametrize("dataset", ["provinces", "house", "electoral_votes", "polls"])
def test_json_envelope(client: TestClient, api_world, dataset: str) -> None:  # type: ignore[no-untyped-def]
    r = client.get(f"/api/export/{api_world.final_id}/{dataset}.json")
    assert r.status_code == 200, r.text
    env = r.json()
    sch = get_schema(dataset)
    assert env["schema"] == sch.name and env["schema_version"] == sch.version
    assert [c["name"] for c in env["columns"]] == list(sch.names)
    assert env["rows"] and all(len(row) == len(sch.names) for row in env["rows"])
    assert env["metadata"]["election_id"] == api_world.final_id


def test_units_export_filtered(client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    r = client.get(f"/api/export/{api_world.final_id}/units.csv?race=PRES-NB")
    rows = list(csv.reader(io.StringIO(r.text)))
    assert rows[0] == list(get_schema("units").names)
    assert len(rows) > 1 and {row[2] for row in rows[1:]} == {"PRES-NB"}


def test_electoral_votes_total(client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    env = client.get(f"/api/export/{api_world.final_id}/electoral_votes.json").json()
    idx = [c["name"] for c in env["columns"]].index("electoral_votes")
    assert sum(row[idx] for row in env["rows"]) == 174


def test_export_errors(client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.final_id
    assert client.get(f"/api/export/{e}/nonsense.csv").status_code == 404
    assert client.get(f"/api/export/{e}/provinces.xlsx").status_code == 422
    assert client.get(f"/api/export/{e}/provinces").status_code == 404
    assert client.get(f"/api/export/{e}/swing.csv").status_code == 404  # no earlier election
    assert client.get(f"/api/export/{e}/montecarlo_summary.csv").status_code == 404  # no forecast yet


def test_schemas_listing(client: TestClient) -> None:
    s = client.get("/api/export/schemas").json()
    names = {d["name"] for d in s["datasets"]}
    assert {"province_results", "house_results", "polls", "swing"} <= names
    prov = next(d for d in s["datasets"] if d["name"] == "province_results")
    assert prov["requires_reported_election"] is True and "provinces" in prov["aliases"]
