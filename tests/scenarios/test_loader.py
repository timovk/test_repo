"""Scenario loading, includes, round-trips, listing, duplication and geography validation."""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest
import yaml

from app.core.errors import ScenarioError
from app.scenarios.loader import (
    dump_scenario,
    duplicate_scenario,
    list_scenarios,
    load_scenario,
    load_scenario_text,
    merge_includes,
    save_scenario,
    scenario_path,
    validate_scenario,
    validate_scenario_or_raise,
)
from app.scenarios.schema import ScenarioDocument
from app.simulation.names import BLOCKED_FULL_NAMES

SLUGS = ("founding-2024", "midterm-2026", "demo-2028")
REAL_POLLSTERS = ("peil", "ipsos", "i&o", "verian", "eenvandaag", "kantar", "maurice de hond", "motivaction")


@pytest.fixture(scope="module")
def docs() -> dict[str, ScenarioDocument]:
    return {s: load_scenario(s) for s in SLUGS}


def test_demo_scenarios_load(docs):
    f24, m26, g28 = docs["founding-2024"], docs["midterm-2026"], docs["demo-2028"]
    assert f24.scenario.election_type == "general" and f24.scenario.year == 2024
    assert m26.scenario.election_type == "midterm" and m26.scenario.year == 2026
    assert g28.scenario.election_type == "general" and g28.scenario.year == 2028
    for d in docs.values():
        assert [p.code for p in d.parties] == ["PA", "SAP", "VLP", "DM", "CVU", "NVB", "PLB", "RV"]
        assert d.scenario.fictional is True and all(p.fictional for p in d.parties)
    assert len({d.scenario.seed for d in docs.values()}) == 3
    assert len({d.scenario.political_geography_seed for d in docs.values()}) == 1
    # tickets: six parties, PLB and RV field none
    for d in (f24, g28):
        assert sorted(t.party for t in d.president.tickets) == sorted(
            ["PA", "VLP", "NVB", "CVU", "DM", "SAP"]
        )
    assert f24.senate.classes_up == [1, 2, 3]
    assert m26.senate.classes_up == [1]
    assert g28.senate.classes_up == [2]
    assert m26.municipal.enabled and not m26.governors.enabled and not m26.president.tickets
    assert f24.governors.enabled and g28.governors.enabled
    # party lineage: PLB renamed in 2027 (override + event)
    assert (
        g28.party("PLB").name == "Plattelands- en Regiopartij" and f24.party("PLB").name == "Plattelandsbond"
    )
    assert any(e.event == "renamed" and e.party == "PLB" for e in g28.party_events)
    # the 2028 incumbent ticket and the midterm president's party
    assert next(t for t in g28.president.tickets if t.party == "VLP").incumbent
    assert any(c.incumbent_office == "PRES" and c.party == "VLP" for c in m26.candidates)


def test_demo_scenarios_use_only_fictional_people_and_pollsters(docs):
    for d in docs.values():
        for c in d.candidates:
            full = unicodedata.normalize("NFKD", f"{c.first_name} {c.last_name}".lower())
            assert "".join(ch for ch in full if not unicodedata.combining(ch)) not in BLOCKED_FULL_NAMES
            assert c.bio and "Fictional" in c.bio
        for p in d.polling.pollsters:
            assert not any(r in p.name.lower() for r in REAL_POLLSTERS), p.name


@pytest.mark.parametrize("slug", SLUGS)
def test_round_trip(docs, slug):
    doc = docs[slug]
    text = dump_scenario(doc)
    assert "parties_file" not in text  # includes are merged
    again = load_scenario_text(text)
    assert again == doc
    assert dump_scenario(again) == text


def test_resolution_by_slug_filename_and_path(docs):
    assert scenario_path("demo-2028").name == "general_2028.yaml"  # via scenario.slug scan
    assert scenario_path("founding_2024").name == "founding_2024.yaml"  # via file name
    assert load_scenario("config/scenarios/midterm_2026.yaml") == docs["midterm-2026"]
    with pytest.raises(ScenarioError):
        load_scenario("no-such-scenario")


def test_list_scenarios():
    infos = {i.slug: i for i in list_scenarios()}
    for s in SLUGS:
        assert s in infos and infos[s].valid
    assert [i.year for i in list_scenarios()] == sorted(i.year for i in list_scenarios())


def test_list_reports_invalid_files(tmp_path):
    (tmp_path / "broken.yaml").write_text(
        "scenario: {slug: broken, name: B, year: 2030, seed: 1}\nparties: []\nfoo: 1\n"
    )
    infos = list_scenarios(tmp_path)
    assert len(infos) == 1 and not infos[0].valid and infos[0].error


def test_includes_and_overrides(tmp_path):
    parties = tmp_path / "parties.yaml"
    parties.write_text(
        yaml.safe_dump(
            {
                "parties": [
                    {"code": "AA", "name": "A", "abbreviation": "A", "color": "#000000", "base_share": 0.5},
                    {"code": "BB", "name": "B", "abbreviation": "B", "color": "#ffffff", "base_share": 0.5},
                ]
            }
        )
    )
    cands = tmp_path / "cands.yaml"
    cands.write_text(
        yaml.safe_dump({"candidates": [{"key": "x", "first_name": "X", "last_name": "Y", "party": "AA"}]})
    )
    raw = {
        "scenario": {"slug": "inc", "name": "Inc", "year": 2030, "seed": 3},
        "parties_file": str(parties),
        "candidates_file": [str(cands)],
        "parties": [
            {"code": "BB", "base_share": 0.4},
            {"code": "CC", "name": "C", "abbreviation": "C", "color": "#123456", "base_share": 0.1},
        ],
        "party_overrides": {"AA": {"name": "A renamed"}},
        "candidates": [{"key": "x", "quality": 1.0}],
    }
    merged = merge_includes(raw, tmp_path)
    doc = ScenarioDocument.model_validate(merged)
    assert [p.code for p in doc.parties] == ["AA", "BB", "CC"]
    assert doc.party("AA").name == "A renamed" and doc.party("BB").base_share == 0.4
    assert doc.candidate("x").quality == 1.0 and doc.candidate("x").party == "AA"
    with pytest.raises(ScenarioError, match="unknown party"):
        merge_includes({**raw, "party_overrides": {"ZZ": {"name": "?"}}}, tmp_path)
    with pytest.raises(ScenarioError, match="not found"):
        merge_includes({**raw, "parties_file": "nope/missing.yaml"}, tmp_path)


def test_invalid_document_raises_scenario_error():
    with pytest.raises(ScenarioError):
        load_scenario_text("scenario: {slug: x, name: X, year: 2030, seed: 1}\nparties: []\nunknown_key: 3\n")
    with pytest.raises(ScenarioError):
        load_scenario_text("scenario: [unclosed")


def test_duplicate_scenario(docs):
    src = docs["demo-2028"]
    dup = duplicate_scenario(src, "demo-2028-alt")
    assert dup.scenario.slug == "demo-2028-alt" and dup.scenario.seed != src.scenario.seed
    assert dup.scenario.political_geography_seed == src.scenario.political_geography_seed
    assert duplicate_scenario(src, "demo-2028-alt") == dup  # derived seed is deterministic
    assert duplicate_scenario(src, "x", 5).scenario.seed == 5
    assert src.scenario.slug == "demo-2028"  # original untouched


def test_save_scenario(tmp_path, docs):
    p = save_scenario(docs["founding-2024"], tmp_path / "out.yaml")
    assert load_scenario(p) == docs["founding-2024"]


def test_validate_against_synthetic_frame(synthetic, docs):
    problems = validate_scenario(docs["demo-2028"], synthetic.frame)
    # the demo uses REAL CBS municipality codes, which the toy country does not have
    assert any("GM0363" in p for p in problems)
    with pytest.raises(ScenarioError):
        validate_scenario_or_raise(docs["demo-2028"], synthetic.frame)


def test_validate_catches_bad_references(synthetic, docs):
    data = docs["founding-2024"].model_dump(mode="json")
    data["parties"][0]["provinces"] = {"XX": 0.1}
    data["parties"][0]["regions"] = {"atlantis": 0.1}
    data["environment"]["provinces"] = {"YY": {"PA": 0.1}}
    data["house"]["contest_rules"]["RV"]["regions"] = ["narnia"]
    data["senate"]["classes_up"] = [4]
    data["calibration"]["imported_baseline"] = "baselines/none.yaml"
    data["candidates"][0]["home_province"] = "LI"  # Wassenaar is in ZH (checked on real frames)
    doc = ScenarioDocument.model_validate(data)
    problems = "\n".join(validate_scenario(doc, synthetic.frame))
    for needle in ("'XX'", "atlantis", "'YY'", "narnia", "class 4", "baselines/none.yaml"):
        assert needle in problems


@pytest.mark.realdata
def test_real_frame_validation(real_frame, docs):
    for d in docs.values():
        assert validate_scenario(d, real_frame) == []
    data = docs["founding-2024"].model_dump(mode="json")
    data["candidates"][0]["home_province"] = "LI"
    problems = validate_scenario(ScenarioDocument.model_validate(data), real_frame)
    assert any("lies in ZH" in p for p in problems)


def test_scenario_files_are_marked_fictional():
    for f in Path(scenario_path("demo-2028")).parent.glob("*.yaml"):
        assert "FICTIONAL" in f.read_text(encoding="utf-8")[:400], f.name
