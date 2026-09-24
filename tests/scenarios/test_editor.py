"""Scenario editor helpers."""

from __future__ import annotations

import pytest

from app.core.errors import ScenarioError
from app.scenarios.editor import apply_edits, available_operations
from app.scenarios.loader import load_scenario


@pytest.fixture(scope="module")
def doc():
    return load_scenario("demo-2028")


def test_basic_operations(doc):
    res = apply_edits(
        doc,
        [
            {"op": "set_national_environment", "party": "PA", "value": 0.1},
            {"op": "set_national_environment", "values": {"DM": None, "RV": 0.02}},
            {"op": "set_province_environment", "province": "NB", "party": "CVU", "value": 0.2},
            {"op": "set_candidate_quality", "candidate": "lotte-van-der-ploeg", "value": 1.5},
            {"op": "set_party_base_share", "party": "SAP", "value": 0.1},
            {"op": "set_party_field", "party": "DM", "field": "color", "value": "#112233"},
            {"op": "set_ev_allocation", "value": "proportional"},
            {"op": "set_seed", "value": 123},
            {"op": "set_political_geography_seed", "value": 77},
            {"op": "set", "path": "environment.turnout_base", "value": 0.7},
            {"op": "set", "path": "parties.CVU.provinces.LI", "value": 0.3},
            {"op": "set", "path": "environment.national.NVB", "value": None},
        ],
    )
    d = res.document
    assert (
        d.environment.national["PA"] == 0.1
        and "DM" not in d.environment.national
        and d.environment.national["RV"] == 0.02
    )
    assert "NVB" not in d.environment.national
    assert d.environment.provinces["NB"]["CVU"] == 0.2
    assert d.candidate("lotte-van-der-ploeg").quality == 1.5
    assert d.party("SAP").base_share == 0.1 and d.party("DM").color == "#112233"
    assert d.electoral_college.allocation.value == "proportional"
    assert d.scenario.seed == 123 and d.scenario.political_geography_seed == 77
    assert d.environment.turnout_base == 0.7
    assert d.party("CVU").provinces["LI"] == 0.3
    assert len(res.changes) == 12 and res.ok
    # the input document is untouched
    assert doc.environment.turnout_base != 0.7 and doc.scenario.seed != 123


def test_ticket_operations(doc):
    res = apply_edits(doc, [{"op": "remove_ticket", "party": "DM"}])
    assert "DM" not in [t.party for t in res.document.president.tickets]
    res = apply_edits(
        res.document,
        [
            {
                "op": "add_ticket",
                "party": "DM",
                "president": "jasper-de-lange",
                "vice_president": "iris-nijland",
            }
        ],
    )
    assert "DM" in [t.party for t in res.document.president.tickets]
    with pytest.raises(ScenarioError, match="already has a ticket"):
        apply_edits(
            doc,
            [
                {
                    "op": "add_ticket",
                    "party": "PA",
                    "president": "lotte-van-der-ploeg",
                    "vice_president": "anil-jagesar",
                }
            ],
        )
    replaced = apply_edits(
        doc,
        [
            {
                "op": "add_ticket",
                "party": "PA",
                "president": "anil-jagesar",
                "vice_president": "lotte-van-der-ploeg",
                "replace": True,
            }
        ],
    )
    assert next(t for t in replaced.document.president.tickets if t.party == "PA").president == "anil-jagesar"
    wd = apply_edits(doc, [{"op": "withdraw_ticket", "party": "SAP"}])
    assert next(t for t in wd.document.president.tickets if t.party == "SAP").withdrawn
    with pytest.raises(ScenarioError):
        apply_edits(
            doc,
            [
                {
                    "op": "add_ticket",
                    "party": "PA",
                    "president": "ghost",
                    "vice_president": "anil-jagesar",
                    "replace": True,
                }
            ],
        )


def test_candidate_operations(doc):
    res = apply_edits(
        doc,
        [
            {
                "op": "add_candidate",
                "candidate": {
                    "key": "new-person",
                    "first_name": "Nieuw",
                    "last_name": "Persoon",
                    "party": "PLB",
                },
            }
        ],
    )
    assert res.document.candidate("new-person").party == "PLB"
    res2 = apply_edits(res.document, [{"op": "remove_candidate", "candidate": "new-person"}])
    with pytest.raises(KeyError):
        res2.document.candidate("new-person")
    with pytest.raises(ScenarioError, match="presidential ticket"):
        apply_edits(doc, [{"op": "remove_candidate", "candidate": "lotte-van-der-ploeg"}])
    with pytest.raises(ScenarioError, match="already exists"):
        apply_edits(
            doc,
            [
                {
                    "op": "add_candidate",
                    "candidate": {
                        "key": "lotte-van-der-ploeg",
                        "first_name": "A",
                        "last_name": "B",
                        "party": "PA",
                    },
                }
            ],
        )


def test_invalid_edits(doc):
    with pytest.raises(ScenarioError, match="unknown operation"):
        apply_edits(doc, [{"op": "delete_everything"}])
    with pytest.raises(ScenarioError, match="unknown party"):
        apply_edits(doc, [{"op": "set_party_base_share", "party": "ZZ", "value": 0.1}])
    with pytest.raises(ScenarioError, match="EV allocation"):
        apply_edits(doc, [{"op": "set_ev_allocation", "value": "lottery"}])
    with pytest.raises(ScenarioError, match="invalid"):
        apply_edits(doc, [{"op": "set", "path": "environment.turnout_base", "value": 1.5}])
    with pytest.raises(ScenarioError, match="fictional"):
        apply_edits(doc, [{"op": "set", "path": "scenario.fictional", "value": False}])
    with pytest.raises(ScenarioError, match="stable identifier"):
        apply_edits(doc, [{"op": "set_party_field", "party": "PA", "field": "code", "value": "PX"}])
    assert "set_seed" in available_operations()


def test_geography_problems_reported(doc, synthetic):
    res = apply_edits(
        doc,
        [{"op": "set_province_environment", "province": "XX", "party": "PA", "value": 0.1}],
        frame=synthetic.frame,
    )
    assert not res.ok and any("'XX'" in p for p in res.problems)
