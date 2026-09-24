"""Builders from standard results frames to export schemas, end to end with the writers."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.export import builders as B
from app.export import schemas as S
from app.export import writers as W


def test_results_export_every_level(election_curr: pd.DataFrame) -> None:
    for level, name in (
        ("national", "national_results"),
        ("province", "province_results"),
        ("municipality", "municipality_results"),
        ("district", "district_results"),
    ):
        out = B.results_export(election_curr, level)
        assert S.validate(out, name) == [], level
        assert set(out["level"]) == {level}
    nat = B.results_export(election_curr, "national")
    assert nat["turnout"].notna().all()
    with pytest.raises(ValueError):
        B.results_export(election_curr, "galaxy")


def test_unit_results_with_municipality_mapping(election_curr: pd.DataFrame) -> None:
    units = B.results_export(
        election_curr, "unit", unit_municipality={"BU08550101": "GM0855", "BU08550102": "GM0855"}
    )
    assert set(units["municipality_code"]) == {"GM0855"}
    bare = B.results_export(election_curr, "unit")
    assert bare["municipality_code"].isna().all()
    assert S.validate(bare, "unit_results") == []


def test_house_results_flips_incumbents_and_independents(election_prev, election_curr) -> None:
    inc = pd.DataFrame(
        {
            "race_code": ["HOUSE-NB-01"],
            "incumbent_candidate": ["Cand h1a"],
            "incumbent_party": ["A"],
            "is_open_seat": [False],
        }
    )
    out = B.house_results_export(election_curr, prev=election_prev, incumbents=inc)
    assert S.validate(out, "house_results") == []
    h = out.set_index("district_code")
    assert h.loc["NB-01", "flip_status"] == "flip" and h.loc["NB-01", "winner_party"] == "B"
    assert not h.loc["NB-01", "incumbent_won"]
    assert h.loc["NB-02", "flip_status"] == "hold"
    assert pd.isna(h.loc["UT-01", "winner_party"])  # independent → null party code
    assert h.loc["UT-01", "margin_pp"] == pytest.approx(100.0)
    assert h.loc["NB-01", "district_name"] == "District NB-01"


def test_senate_and_governor_results(election_prev, election_curr) -> None:
    sen = B.senate_results_export(
        election_curr,
        prev=election_prev,
        senate_classes={"SEN-NB-1": 1, "SEN-UT-2": 3},
        special_races={"SEN-UT-2"},
    )
    assert S.validate(sen, "senate_results") == []
    s = sen.set_index("race_code")
    assert s.loc["SEN-NB-1", "seat_number"] == 1 and s.loc["SEN-UT-2", "seat_number"] == 2
    assert s.loc["SEN-UT-2", "senate_class"] == 3 and s.loc["SEN-UT-2", "is_special"]
    assert s.loc["SEN-NB-1", "province_name"] == "Noord-Brabant"
    bare = B.senate_results_export(election_curr)
    assert bare["senate_class"].isna().all() and bare["is_special"].isna().all()
    gov = B.governor_results_export(election_curr)
    assert S.validate(gov, "governor_results") == []
    g = gov.iloc[0]
    assert pd.isna(g["winner_party"]) and np.isnan(g["margin_pp"])  # no votes cast: no winner
    assert g["flip_status"] == "new"  # no previous holder known
    held = B.governor_results_export(
        election_curr, incumbents=pd.DataFrame({"race_code": ["GOV-GR"], "incumbent_party": ["A"]})
    )
    assert held["flip_status"].iloc[0] == "undecided"


def test_electoral_votes_export(election_curr: pd.DataFrame, canonical_ev) -> None:
    ev = B.electoral_votes_export(election_curr, canonical_ev)
    assert S.validate(ev, "electoral_votes") == []
    assert ev["electoral_votes"].sum() == sum(canonical_ev.values())
    e = ev.set_index("province_code")
    assert e.loc["ZE", "decided_by"] == "lot" and e.loc["ZE", "winner_party"] == "A"
    assert e.loc["GR", "decided_by"] == "popular_vote"
    custom = B.electoral_votes_export(election_curr, canonical_ev, decided_by={"ZE": "recount"}).set_index(
        "province_code"
    )
    assert custom.loc["ZE", "decided_by"] == "recount"
    with pytest.raises(ValueError):
        B.electoral_votes_export(election_curr, {"GR": 7})


def test_swing_export(election_prev, election_curr) -> None:
    sw = B.swing_export(election_prev, election_curr, "province", race_type="PRESIDENT")
    assert S.validate(sw, "swing") == []
    assert set(sw["race_family"]) == {"PRESIDENT"} and len(sw) == 12 * 2


def test_bundle_from_builders(tmp_path: Path, election_prev, election_curr, canonical_ev) -> None:
    frames = B.build_all_results(election_curr)
    assert set(frames) == {
        "national_results",
        "province_results",
        "municipality_results",
        "unit_results",
        "district_results",
    }
    frames |= {
        "house": B.house_results_export(election_curr, prev=election_prev),
        "senate": B.senate_results_export(election_curr),
        "governors": B.governor_results_export(election_curr),
        "electoral_votes": B.electoral_votes_export(election_curr, canonical_ev),
        "swing": B.swing_export(election_prev, election_curr, "municipality", race_type="PRESIDENT"),
    }
    paths = W.export_bundle(
        frames, tmp_path, metadata={"election_id": 2, "seed": 1}, generated_at="2032-11-04T00:00:00+00:00"
    )
    manifest = json.loads(paths[-1].read_text())
    assert len(manifest["files"]) == 2 * len(frames)
    for p in paths[:-1]:
        if p.suffix == ".json":
            df, head = W.read_json(p)
            assert head["data_category"] == "SIMULATED"
            assert S.validate(df, head["schema"]) == []
    only = B.build_all_results(election_curr, levels=["national"])
    assert list(only) == ["national_results"]


def test_synthetic_unit_export_is_fast_and_valid(synthetic, tmp_path: Path) -> None:
    """Unit-level export for the whole synthetic country (vectorised builder + writer)."""
    import time

    from app.analytics.results import build_results_frame
    from app.core.rng import make_rng

    geo = synthetic.frame
    rng = make_rng(5, "export-test")
    votes = rng.integers(0, 500, size=(geo.n_units, 3))
    pc = np.asarray(geo.province_codes, dtype=object)[geo.unit_province]
    df = pd.DataFrame(
        {
            "election_id": 1,
            "year": 2028,
            "race_code": np.repeat([f"PRES-{p}" for p in pc], 3),
            "race_type": "PRESIDENT_PROVINCE",
            "level": "unit",
            "geo_code": np.repeat(geo.unit_codes, 3),
            "province_code": np.repeat(pc, 3),
            "line_key": np.tile(["t-a", "t-b", "t-c"], geo.n_units),
            "party_code": np.tile(["A", "B", "C"], geo.n_units),
            "votes": votes.ravel(),
        }
    )
    frame = build_results_frame(df)
    mapping = dict(zip(geo.unit_codes, np.asarray(geo.muni_codes)[geo.unit_muni], strict=True))
    t0 = time.perf_counter()
    out = B.results_export(frame, "unit", unit_municipality=mapping)
    paths = W.export_bundle({"unit_results": out}, tmp_path, generated_at="2028-01-01T00:00:00+00:00")
    assert time.perf_counter() - t0 < 20.0
    back = W.read_csv(paths[0], "unit_results")
    assert len(back) == 3 * geo.n_units and back["municipality_code"].notna().all()
