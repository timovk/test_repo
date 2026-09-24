"""Historical-results import and model-based analysis helpers (spec §13)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.core.config import config_path
from app.core.errors import ConfigError
from app.simulation.baselines import (
    PROVENANCE_LABEL,
    elasticity,
    elasticity_ranking,
    import_historical_csv,
    import_historical_results,
    load_imported_baseline,
    municipality_vs_rest_of_province,
    province_competitiveness,
    province_lean,
    urban_coalition_index,
)

EXAMPLE = Path(config_path("baselines/example/mapping.yaml"))


def test_import_example(frame):
    imp = import_historical_results(EXAMPLE.parent / "results.csv", EXAMPLE, frame)
    assert imp.provenance == PROVENANCE_LABEL
    assert imp.synthetic is True
    assert imp.data_category.value == "DERIVED"
    assert imp.municipality_codes == ["GM0101", "GM0102", "GM1101", "GM1201"]
    assert imp.unresolved_municipalities == ["GM9999"]
    assert imp.unmapped_parties == ["Example Pensioners List"]
    assert np.allclose(imp.shares.sum(axis=1), 1.0)
    df = imp.to_frame()
    total = 21000 + 9000 + 12000 + 8000 + 4000 + 7000 + 1000
    assert df.loc["GM0101", "SAP"] == pytest.approx(0.4 * 9000 / total)
    assert df.loc["GM0101", "RV"] == pytest.approx(0.2 * 4000 / total)
    targets = imp.to_calibration_targets()
    assert set(targets["GM1201"]) == set(imp.parties)
    assert import_historical_csv is import_historical_results
    same = load_imported_baseline(EXAMPLE, frame)
    assert np.allclose(same.shares, imp.shares)


def test_import_errors(tmp_path, frame):
    bad = tmp_path / "bad.csv"
    bad.write_text("gemeente,partij,stemmen\nGM0101,A,1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="missing columns"):
        import_historical_results(bad, EXAMPLE, frame)
    strict = tmp_path / "strict.yaml"
    strict.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace("unmapped: drop", "unmapped: error"), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="without mapping"):
        import_historical_results(EXAMPLE.parent / "results.csv", strict, frame)
    with pytest.raises(ConfigError):
        import_historical_results(tmp_path / "nope.csv", EXAMPLE, frame)


def test_province_lean(demo_model):
    df = province_lean(demo_model)
    assert len(df) == 12 * len(demo_model.party_codes)
    assert df.attrs["data_category"] == "SIMULATED"
    # eligible-weighted leans average out: every province lean sums to 0 over parties
    for _, g in df.groupby("province_code"):
        assert g["lean_pp"].sum() == pytest.approx(0.0, abs=1e-6)
        assert g["province_share"].sum() == pytest.approx(1.0)


def test_municipality_vs_rest(demo_model, frame):
    code = frame.muni_codes[0]
    df = municipality_vs_rest_of_province(demo_model, code)
    assert df["municipality_share"].sum() == pytest.approx(1.0)
    assert df["rest_share"].sum() == pytest.approx(1.0)
    # the province share lies between the municipality and the rest
    lo = np.minimum(df["municipality_share"], df["rest_share"]) - 1e-9
    hi = np.maximum(df["municipality_share"], df["rest_share"]) + 1e-9
    assert ((df["province_share"] >= lo) & (df["province_share"] <= hi)).all()
    with pytest.raises(KeyError):
        municipality_vs_rest_of_province(demo_model, "Atlantis")


def test_urban_coalition_index(demo_model):
    df = urban_coalition_index(demo_model).set_index("party")
    assert df.loc["PA", "index"] > df.loc["PLB", "index"]
    assert df.loc["PA", "density_correlation"] > 0 > df.loc["PLB", "density_correlation"]


def test_elasticity_helpers(demo_model, frame):
    e = elasticity(demo_model, "municipality")
    assert len(e) == frame.n_munis
    w = frame.to_munis(demo_model.eligible)
    assert (e * w).sum() / w.sum() == pytest.approx(1.0, abs=1e-9)
    rank = elasticity_ranking(demo_model, top=10)
    assert len(rank) == 10 and rank["elasticity"].is_monotonic_decreasing
    assert len(elasticity(demo_model, "province")) == 12


def test_province_competitiveness(demo_model, demo_doc):
    ticket_parties = [t.party for t in demo_doc.president.tickets]
    df = province_competitiveness(demo_model, ticket_parties)
    assert len(df) == 12 and df["margin_pp"].is_monotonic_increasing
    assert set(df["leader"]) <= set(ticket_parties)
    assert (df["leader_share"] >= df["runner_up_share"]).all()
