"""The hidden-until-reported rule: a SIMULATED election's stored result never leaks."""

from __future__ import annotations

import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

_INT = re.compile(r"(?<![\d.])\d{6,}(?![\d.])")


@pytest.fixture(scope="module")
def hidden_numbers(api_world) -> set[str]:  # type: ignore[no-untyped-def]
    """Stored vote totals / ballots of the hidden election at province and national level (large
    numbers, so a coincidental match with REAL statistics is practically impossible)."""
    con = sqlite3.connect(api_world.path)
    try:
        rid = [r for (r,) in con.execute("select id from race where election_id = ?", (api_world.hidden_id,))]
        marks = ",".join("?" * len(rid))
        votes = con.execute(
            f"select votes from election_result where race_id in ({marks}) and level in ('national','province') "
            "and votes >= 100000",
            rid,
        ).fetchall()
        turn = con.execute(
            f"select ballots_cast, valid_votes from turnout_result where race_id in ({marks}) and level = 'national'",
            rid,
        ).fetchall()
    finally:
        con.close()
    out = {str(v) for (v,) in votes} | {str(x) for row in turn for x in row if x >= 100000}
    assert len(out) > 20
    return out


HIDDEN_PAGES = [
    "/api/elections",
    "/api/elections/{e}",
    "/api/elections/{e}/president",
    "/api/elections/{e}/electoral-college",
    "/api/elections/{e}/provinces",
    "/api/elections/{e}/provinces?race=GOV",
    "/api/elections/{e}/provinces/NB",
    "/api/elections/{e}/municipalities",
    "/api/elections/{e}/municipalities?race=HOUSE",
    "/api/elections/{e}/house",
    "/api/elections/{e}/house/NB-01",
    "/api/elections/{e}/senate",
    "/api/elections/{e}/governors",
    "/api/elections/{e}/races/PRES",
    "/api/elections/{e}/races/PRES-NB",
    "/api/elections/{e}/races/GOV-NB",
    "/api/elections/{e}/calls",
    "/api/elections/{e}/timeline",
    "/api/elections/{e}/timeline?detail=events",
    "/api/analytics/{e}",
    "/api/history/summary",
    "/api/history/closest",
    "/api/history/divergence",
    "/api/parties",
    "/api/polls/{e}",
    "/api/polls/{e}/average",
    "/api/campaigns/{e}",
]


@pytest.mark.parametrize("page", HIDDEN_PAGES)
def test_no_hidden_numbers_leak(client: TestClient, api_world, hidden_numbers: set[str], page: str) -> None:  # type: ignore[no-untyped-def]
    r = client.get(page.format(e=api_world.hidden_id))
    assert r.status_code == 200, r.text
    leaked = set(_INT.findall(r.text)) & hidden_numbers
    assert not leaked, f"{page} leaks stored results: {sorted(leaked)[:5]}"


def test_hidden_shapes(client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    e = api_world.hidden_id
    p = client.get(f"/api/elections/{e}/president").json()
    assert p["results_source"] == "hidden" and p["notice"]
    assert p["status"] == "SCHEDULED" and p["winner"] is None and p["popular_vote"] is None
    assert all(t["votes"] is None and t["electoral_votes"] is None for t in p["tickets"])
    # before the night: 0 EV allocated, all 174 available, 88 to win
    assert (p["ev_decided_total"], p["ev_uncalled"], p["majority"], p["label"]) == (0, 174, 88, "88 TO WIN")
    ec = client.get(f"/api/elections/{e}/electoral-college").json()
    assert (ec["ev_decided_total"], ec["ev_uncalled"], ec["majority"]) == (0, 174, 88)
    assert all(t["president"]["name"] for t in p["tickets"])
    assert all(x["pct"] is None and x["winner"] is None and x["ev"] for x in p["provinces"])
    h = client.get(f"/api/elections/{e}/house").json()
    assert all(x["won"] is None and x["net_change"] is None for x in h["by_party"])
    assert sum(h["previous_composition"].values()) + h["previous_vacant"] == 150
    assert all(
        d["winner"] is None and d["margin_pp"] is None and d["top"][0]["votes"] is None
        for d in h["districts"]
    )
    s = client.get(f"/api/elections/{e}/senate").json()
    assert s["composition"]["projected"] is None and s["control"] is None
    assert s["up"] == 8 and sum(s["composition"]["current"].values()) == 24
    m = client.get(f"/api/elections/{e}/municipalities").json()
    assert all(x["shares"] is None and x["votes"] is None for x in m["municipalities"])
    race = client.get(f"/api/elections/{e}/races/PRES-NB").json()
    assert race["race"]["winner"] is None and race["calls"] == [] and race["municipalities"] is None
    assert all(ln["votes"] is None for ln in race["race"]["lines"])
    t = client.get(f"/api/elections/{e}/timeline").json()
    assert t["revealed_events"] == 0 and t["buckets"] == [] and t["total_events"] > 0
    a = client.get(f"/api/analytics/{e}").json()
    assert a["available"] is False
    c = client.get(f"/api/campaigns/{e}").json()
    assert c["effects_revealed"] is False and c["hidden_fields"] == ["realized_effect", "turnout_effect"]
    targets = [t for camp in c["campaigns"] for t in camp["targets"]]
    assert targets and all(t["realized_effect"] is None and t["turnout_effect"] is None for t in targets)
    assert all(t["expected_effect"] is not None for t in targets)
    page = client.get(f"/api/elections/{e}/municipalities/{m['municipalities'][0]['code']}").json()
    assert page["precincts"] == [] and page["precincts_available"] is False
    assert all(r["municipal"]["total_votes"] is None for r in page["races"])


def test_hidden_candidate_results(client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    p = client.get(f"/api/elections/{api_world.hidden_id}/president").json()
    cid = p["tickets"][0]["president"]["id"]
    prof = client.get(f"/api/candidates/{cid}").json()
    hidden = [c for c in prof["candidacies"] if c["election"]["id"] == api_world.hidden_id]
    assert hidden and all(c["result"] is None for c in hidden)


@pytest.mark.parametrize(
    "dataset",
    ["national", "provinces", "municipalities", "house", "senate", "electoral_votes", "timeline", "calls"],
)
def test_hidden_exports_refused(client: TestClient, api_world, dataset: str) -> None:  # type: ignore[no-untyped-def]
    r = client.get(f"/api/export/{api_world.hidden_id}/{dataset}.csv")
    assert r.status_code == 409 and r.json()["code"] == "conflict"


def test_hidden_polls_are_available(client: TestClient, api_world) -> None:  # type: ignore[no-untyped-def]
    polls = client.get(f"/api/polls/{api_world.hidden_id}").json()
    assert polls["total"] > 0 and polls["polls"][0]["results"]
    avg = client.get(f"/api/polls/{api_world.hidden_id}/average").json()
    assert avg["average"]["accuracy"] is None and avg["average"]["mean"]
    r = client.get(f"/api/export/{api_world.hidden_id}/polls.csv")
    assert r.status_code == 200
