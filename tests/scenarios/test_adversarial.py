"""Adversarial tests for scenario loading, validation and editing."""

from __future__ import annotations

import pytest

from app.core.errors import ScenarioError
from app.scenarios.editor import apply_edits
from app.scenarios.loader import list_scenarios, load_scenario, scenario_path, validate_scenario
from app.scenarios.schema import ScenarioDocument


@pytest.fixture(scope="module")
def doc() -> ScenarioDocument:
    return load_scenario("demo-2028")


def test_list_scenarios_survives_broken_files(tmp_path):
    (tmp_path / "a_bad_yaml.yaml").write_text("scenario: [unclosed\n", encoding="utf-8")
    (tmp_path / "b_bad_year.yaml").write_text(
        "scenario: {slug: y, name: Y, year: soon, seed: lots}\nparties: []\n", encoding="utf-8"
    )
    (tmp_path / "c_scalar.yaml").write_text("just a string\n", encoding="utf-8")
    (tmp_path / "d_meta_list.yaml").write_text("scenario: [1, 2]\n", encoding="utf-8")
    infos = list_scenarios(tmp_path)
    assert len(infos) == 4 and not any(i.valid for i in infos)
    assert all(i.error for i in infos)
    bad_year = next(i for i in infos if i.slug == "y")
    assert bad_year.year == 0 and bad_year.seed == 0


@pytest.mark.parametrize("slug", ["../parties/fictional", "..", "/etc/passwd", "a/b", "Demo-2028", ""])
def test_slug_resolution_cannot_escape_scenarios_dir(slug):
    with pytest.raises(ScenarioError):
        scenario_path(slug)


def test_validation_catches_new_references(synthetic, doc):
    data = doc.model_dump(mode="json")
    data["environment"]["president_party"] = "GHOST"
    data["calibration"]["national"]["PA"] = 0.0
    data["calibration"]["provinces"] = {"NB": {"CVU": 1.5}}
    data["campaigns"]["budgets"]["XYZ"] = 10
    data["campaigns"]["strategies"]["QQQ"] = "base"
    data["polling"]["pollsters"][0]["house_effects"] = {"NOPE": 1.0}
    data["senate"]["special_elections"] = ["SEN-NB-3", "SEN-XX-1", "NB-1"]
    problems = "\n".join(validate_scenario(ScenarioDocument.model_validate(data), synthetic.frame))
    for needle in (
        "president_party: unknown party 'GHOST'",
        "calibration.national.PA",
        "calibration.provinces.NB.CVU",
        "campaigns.budgets: unknown party 'XYZ'",
        "campaigns.strategies: unknown party 'QQQ'",
        "house_effects: unknown party 'NOPE'",
        "malformed seat code 'SEN-NB-3'",
        "unknown province in 'SEN-XX-1'",
    ):
        assert needle in problems, needle
    assert "'NB-1'" not in problems  # accepted short form


def test_validation_checks_demographics_against_the_frame(synthetic, doc):
    import dataclasses

    frame = synthetic.frame
    reduced = dataclasses.replace(
        frame,
        demo_names=frame.demo_names[:-1],
        unit_demo=frame.unit_demo[:, :-1],
        unit_demo_imputed=frame.unit_demo_imputed[:, :-1],
        _unit_demo_z=None,
    )
    data = doc.model_dump(mode="json")
    data["parties"][0]["demographics"] = {frame.demo_names[-1]: 0.1}
    problems = validate_scenario(ScenarioDocument.model_validate(data), reduced)
    assert any(f"unknown variable '{frame.demo_names[-1]}'" in p for p in problems)


@pytest.mark.parametrize(
    "edit",
    [
        {"op": "set_seed", "value": "abc"},
        {"op": "set_national_environment", "values": [1, 2]},
        {"op": "set_candidate_quality", "candidate": "lotte-van-der-ploeg", "value": None},
        {"op": "set_party_base_share", "party": "PA", "value": {"x": 1}},
        {"op": "set", "path": 5, "value": 1},
        {"op": "set", "path": "parties.99.base_share", "value": 0.1},
        {"op": "set", "path": "environment.turnout_base.deeper", "value": 0.1},
        {"op": "add_candidate", "candidate": "not-a-mapping"},
    ],
)
def test_malformed_edits_raise_scenario_errors(doc, edit):
    with pytest.raises(ScenarioError):
        apply_edits(doc, [edit])


def test_president_party_edit_round_trip(doc, synthetic):
    res = apply_edits(doc, [{"op": "set", "path": "environment.president_party", "value": "PA"}])
    assert res.document.environment.president_party == "PA"
    bad = apply_edits(
        doc, [{"op": "set", "path": "environment.president_party", "value": "ZZ"}], frame=synthetic.frame
    )
    assert any("president_party" in p for p in bad.problems)


def test_party_overrides_must_be_a_mapping():
    from app.scenarios.loader import merge_includes

    with pytest.raises(ScenarioError, match="party_overrides"):
        merge_includes({"scenario": {}, "party_overrides": ["PA"]})


def test_includes_cannot_escape_config_or_scenario_dir(tmp_path):
    from app.scenarios.loader import load_scenario_text

    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.yaml"
    secret.write_text(
        "parties:\n  - {code: XX, name: X, abbreviation: X, color: '#000000', base_share: 0.5}\n",
        encoding="utf-8",
    )
    inside = tmp_path / "scenarios"
    inside.mkdir()
    text = "scenario: {slug: s, name: S, year: 2030, seed: 1}\nparties_file: %s\n"
    for ref in (str(secret), "../outside/secret.yaml"):
        with pytest.raises(ScenarioError, match="outside"):
            load_scenario_text(text % ref, inside)
    # the shared party file under config/ still resolves
    doc = load_scenario_text(text % "parties/fictional.yaml", inside)
    assert len(doc.parties) == 8
