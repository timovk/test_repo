"""Structural model: calibration, turnout, elasticity, lean field, transfers, robustness."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from app.core.errors import ScenarioError
from app.simulation.config import load_model_config, model_config_from_dict
from app.simulation.structural import StructuralModel, routing_matrix


def test_national_calibration_matches_base_shares(small_model):
    nat = small_model.national_shares(environment=False)
    base = {p.code: p.base_share for p in small_model.scenario.parties}
    total = sum(base.values())
    for code, target in base.items():
        assert abs(nat[code] - target / total) < 0.005  # within 0.5 pp (in practice ~1e-7)
    assert small_model.calibration.converged
    assert small_model.calibration.max_abs_error < 1e-4


def test_demo_scenario_calibration(demo_model, demo_doc):
    nat = demo_model.national_shares(environment=False)
    for code, target in demo_doc.calibration.national.items():
        assert abs(nat[code] - target) < 0.005
    assert abs(sum(nat.values()) - 1.0) < 1e-9


def test_turnout_calibration(small_model):
    t = float(small_model.expected_turnout_by("national", environment=False).iloc[0])
    assert abs(t - small_model.scenario.environment.turnout_base) < 0.002
    unit_t = small_model.unit_expected_turnout(environment=False)
    cfg = small_model.config.turnout
    assert (unit_t >= cfg.min_probability - 1e-12).all() and (unit_t <= cfg.max_probability + 1e-12).all()
    # demographics matter: turnout varies between units
    assert unit_t.std() > 0.01


def test_turnout_propensity_mix(small_model):
    """Parties with a higher turnout propensity have larger vote than preference shares."""
    st = small_model.party_state(environment=False)
    w = small_model.eligible
    pref = (st.preference * w[:, None]).sum(0) / w.sum()
    votes = (st.vote_share * (w * st.turnout)[:, None]).sum(0) / (w * st.turnout).sum()
    j = small_model.party_index["RIGHT"]  # turnout_propensity +0.3
    assert votes[j] > pref[j]


def test_province_and_municipality_targets(frame, make_doc, no_regions):
    doc = make_doc(
        calibration={
            "provinces": {
                "NB": {"LEFT": 0.2, "RIGHT": 0.4, "CENTRE": 0.2, "RURAL": 0.1, "MINOR": 0.1},
                "GR": {"RURAL": 0.3},
            },
            "municipalities": {"GM1108": {"LEFT": 0.5}, "GM0801": {"RIGHT": 0.45, "LEFT": 0.2}},
        }
    )
    m = StructuralModel.build(frame, doc, regions=no_regions)
    prov = m.province_shares(environment=False)
    assert prov.loc["NB", "RIGHT"] == pytest.approx(0.4, abs=0.002)
    assert prov.loc["NB", "LEFT"] == pytest.approx(0.2, abs=0.002)
    assert prov.loc["GR", "RURAL"] == pytest.approx(0.3, abs=0.002)
    muni = m.municipality_shares(environment=False)
    assert muni.loc["GM1108", "LEFT"] == pytest.approx(0.5, abs=0.002)
    assert muni.loc["GM0801", "RIGHT"] == pytest.approx(0.45, abs=0.002)
    assert muni.loc["GM0801", "LEFT"] == pytest.approx(0.2, abs=0.002)
    # national targets are still met through the unpinned remainder of the country
    nat = m.national_shares(environment=False)
    assert nat["LEFT"] == pytest.approx(0.30, abs=0.005)
    assert m.calibration.converged
    assert "NB" in m.calibration.province_targets and "GM1108" in m.calibration.municipality_targets


def test_national_override(frame, make_doc, no_regions):
    doc = make_doc(calibration={"national": {"MINOR": 0.2}})
    m = StructuralModel.build(frame, doc, regions=no_regions)
    nat = m.national_shares(environment=False)
    assert nat["MINOR"] == pytest.approx(0.2, abs=0.002)
    # unlisted parties keep their base_share proportions in the remaining 80 %
    assert nat["LEFT"] / nat["RIGHT"] == pytest.approx(1.0, abs=0.02)
    assert nat["LEFT"] == pytest.approx(0.8 * 0.30 / 0.92, abs=0.005)


def test_imported_baseline_blending(frame, make_doc, no_regions):
    doc = make_doc(
        calibration={"imported_baseline": "baselines/example/mapping.yaml", "imported_baseline_weight": 1.0}
    )
    # the example maps onto the demo parties; build a doc with those parties instead
    from app.scenarios.loader import load_scenario

    demo = load_scenario("founding-2024")
    data = demo.model_dump(mode="json")
    data["calibration"] = doc.calibration.model_dump(mode="json")
    doc = type(demo).model_validate(data)
    m = StructuralModel.build(frame, doc)
    muni = m.municipality_shares(environment=False)
    # GM0101: Example Green 21000 + 0.6·9000 Labour → PA share (21000+5400)/62900 of mapped votes
    mapped_total = 21000 + 9000 + 12000 + 8000 + 4000 + 7000 + 1000
    assert muni.loc["GM0101", "PA"] == pytest.approx((21000 + 5400) / mapped_total, abs=0.003)
    assert m.calibration.imported_provenance.startswith("DERIVED from real election results")


def test_unknown_references_are_errors(frame, make_doc, no_regions):
    data_bad_demo = make_doc().model_dump(mode="json")
    data_bad_demo["parties"][0]["demographics"] = {"shoe_size": 1.0}
    with pytest.raises(ScenarioError, match="shoe_size"):
        StructuralModel.build(frame, type(make_doc()).model_validate(data_bad_demo), regions=no_regions)
    data = make_doc().model_dump(mode="json")
    data["parties"][0]["regions"] = {"atlantis": 0.2}
    with pytest.raises(ScenarioError, match="atlantis"):
        StructuralModel.build(frame, type(make_doc()).model_validate(data), regions=no_regions)
    data = make_doc().model_dump(mode="json")
    data["parties"][0]["provinces"] = {"XX": 0.2}
    with pytest.raises(ScenarioError, match="XX"):
        StructuralModel.build(frame, type(make_doc()).model_validate(data), regions=no_regions)


def test_unknown_municipality_warns_or_fails_when_strict(frame, make_doc, no_regions):
    data = make_doc().model_dump(mode="json")
    data["parties"][1]["municipalities"] = {"GM9999": 0.3}
    doc = type(make_doc()).model_validate(data)
    m = StructuralModel.build(frame, doc, regions=no_regions)
    assert any("GM9999" in w for w in m.warnings)
    with pytest.raises(ScenarioError, match="GM9999"):
        StructuralModel.build(frame, doc, regions=no_regions, strict=True)


def test_regions_shift_support(frame, make_doc, synth_regions, no_regions):
    data = make_doc().model_dump(mode="json")
    data["parties"][4]["regions"] = {"south": 1.0}
    doc = type(make_doc()).model_validate(data)
    with_r = StructuralModel.build(frame, doc, regions=synth_regions)
    prov = with_r.province_shares(environment=False)
    south = prov.loc[["NB", "LI"], "MINOR"].mean()
    north = prov.loc[["GR", "FR", "DR"], "MINOR"].mean()
    assert south > 1.8 * north


def test_missing_and_imputed_demographics(frame, small_doc, no_regions):
    demo = frame.unit_demo.copy()
    rng = np.random.default_rng(0)
    mask = rng.random(demo.shape) < 0.1
    demo[mask] = np.nan
    imputed = np.zeros_like(demo, dtype=bool)
    imputed[:, 3] = True
    f2 = dataclasses.replace(frame, unit_demo=demo, unit_demo_imputed=imputed, _unit_demo_z=None)
    m = StructuralModel.build(f2, small_doc, regions=no_regions)
    assert np.isfinite(m.U0).all() and np.isfinite(m.tau).all()
    assert any("missing demographic" in w for w in m.warnings)
    nat = m.national_shares(environment=False)
    assert nat["LEFT"] == pytest.approx(0.30, abs=0.005)
    # imputed column is shrunk halfway to the mean (lean.imputed_shrinkage = 0.5)
    ok = np.isfinite(frame.unit_demo[:, 3])
    assert np.abs(m.demo_z[:, 3]).max() <= np.abs(frame.unit_demo_z[ok, 3]).max() * 0.5 + 1e-9


def test_elasticity_properties(frame, make_doc, small_model, no_regions):
    e = small_model.elasticity
    w = small_model.eligible
    assert (e * w).sum() / w.sum() == pytest.approx(1.0, abs=1e-9)
    ec = small_model.config.elasticity
    assert e.min() >= ec.min * 0.99 and e.max() <= ec.max * 1.01
    assert e.std() > 0.02
    flat = StructuralModel.build(
        frame, make_doc(environment={"turnout_base": 0.75, "elasticity_strength": 0.0}), regions=no_regions
    )
    assert np.allclose(flat.elasticity, 1.0)


def test_persistent_lean_is_seeded_by_political_geography_seed(frame, make_doc, no_regions):
    a = StructuralModel.build(frame, make_doc(), regions=no_regions)
    data = make_doc().model_dump(mode="json")
    data["scenario"]["seed"] = 999  # the election seed does not change the persistent character
    b = StructuralModel.build(frame, type(a.scenario).model_validate(data), regions=no_regions)
    assert np.array_equal(a.components["lean_muni"], b.components["lean_muni"])
    assert np.array_equal(a.U0, b.U0)
    data["scenario"]["political_geography_seed"] = 6
    c = StructuralModel.build(frame, type(a.scenario).model_validate(data), regions=no_regions)
    assert not np.allclose(a.components["lean_muni"], c.components["lean_muni"])


def test_lean_field_is_spatially_correlated(small_model, frame):
    lean = small_model.components["lean_muni"]
    xy = frame.muni_xy
    d = np.sqrt(((xy[:, None, :] - xy[None, :, :]) ** 2).sum(-1))
    np.fill_diagonal(d, np.inf)
    nn = d.argmin(axis=1)
    rng = np.random.default_rng(1)
    rand = rng.permutation(len(xy))
    near = np.mean([np.corrcoef(lean[:, j], lean[nn, j])[0, 1] for j in range(lean.shape[1])])
    far = np.mean([np.corrcoef(lean[:, j], lean[rand, j])[0, 1] for j in range(lean.shape[1])])
    assert near > far + 0.2


def test_absent_party_transfer_by_affinity(small_model):
    full = small_model.national_shares(environment=False)
    sub = small_model.national_shares(["LEFT", "RIGHT", "CENTRE", "RURAL"], environment=False)
    assert sum(sub.values()) == pytest.approx(1.0)
    gains = {k: sub[k] / full[k] for k in sub}
    # MINOR (economic −0.9, eurosceptic) is closest to LEFT, not to RIGHT
    assert gains["LEFT"] > gains["RIGHT"]
    _, valid = small_model.expected_valid_weights(None, ["LEFT", "RIGHT"], environment=False)
    _, valid_all = small_model.expected_valid_weights(None, None, environment=False)
    assert valid.sum() < valid_all.sum()  # a share of absent-party supporters abstains
    ac = small_model.config.affinity
    absent = 1 - full["LEFT"] - full["RIGHT"]
    assert 1 - valid.sum() / valid_all.sum() == pytest.approx(absent * ac.absent_abstain_share, rel=0.05)


def test_routing_matrix_conserves_mass():
    ideo = np.array([[-0.5, 0, 0], [0.5, 0, 0], [0, 0.5, 0]])
    R, a = routing_matrix(
        ideo,
        np.array([0, 1, 1, -1]),
        np.array([[-0.5, 0, 0], [0.5, 0, 0], [0.5, 0, 0], [0, 0, 0]]),
        np.array([False, False, True, False]),
        dim_weights=np.ones(3),
        temperature=0.3,
        abstain_share=0.1,
        withdrawn_residual=0.05,
        independent_extra_distance=0.4,
    )
    assert np.allclose(R.sum(axis=1) + a, 1.0)
    assert R[1, 2] == pytest.approx(0.05 / 2)  # withdrawn line of party 1 keeps its residual
    assert a[2] == pytest.approx(0.1)  # party 2 is absent: 10 % abstain


def test_affinity_matrix_rows(small_model):
    A = small_model.affinity_matrix()
    assert np.allclose(A.to_numpy().sum(axis=1), 1.0)
    assert A.loc["MINOR"].idxmax() == "LEFT"


def test_environment_shifts_expectation(small_model):
    base = small_model.national_shares(environment=False)
    env = small_model.national_shares(environment=True)
    assert env["LEFT"] > base["LEFT"]  # environment.national LEFT +0.02


def test_model_config_file_and_validation():
    cfg = load_model_config()
    assert cfg.lean.spatial_length_km > 0
    assert cfg.turnout.demographics["pct_origin_non_europe"] < 0
    with pytest.raises(Exception, match="unknown demographic"):
        model_config_from_dict({"turnout": {"demographics": {"height": 1}}})
    with pytest.raises(Exception, match="viability_gap_full"):
        model_config_from_dict({"strategic": {"viability_gap_start": 0.3, "viability_gap_full": 0.2}})


def test_summary_is_json_serialisable(small_model):
    import json

    s = json.dumps(small_model.summary())
    assert "calibration" in s and small_model.fingerprint
