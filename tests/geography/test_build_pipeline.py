"""End-to-end build of the processed store from synthetic raw files in CBS formats (offline)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pandas as pd
import pyogrio
import pytest

from app.core.errors import DataNotPreparedError
from app.geography import store
from app.geography.build import ALL_DEMOGRAPHICS, build_geography, store_files
from app.geography.cbs import read_buurten, read_layer_attributes, read_supplement
from app.geography.config import load_geography_config
from app.geography.download import download_all
from app.geography.frame import DEMOGRAPHIC_VARIABLES


def _flags(units: pd.DataFrame, code: str) -> set[str]:
    value = units.loc[units["code"] == code, "imputed_fields"].iloc[0]
    return set(filter(None, str(value).split(",")))


@pytest.fixture()
def built(cbs_raw: dict[str, Any]) -> dict[str, Any]:
    report = build_geography(cbs_raw["year"])
    return {"facts": cbs_raw, "report": report}


def test_readers_clean_missing_codes_and_water(cbs_raw: dict[str, Any]) -> None:
    raw = cbs_raw["data_dir"] / "raw"
    b = read_buurten(raw / "wijkenbuurten_2025.gpkg")
    assert "BU01009900" not in set(b["code"])  # water neighbourhood dropped
    assert len(b) == cbs_raw["n_units"]
    assert b["population_raw"].isna().sum() == 2
    assert np.isnan(b.loc[b["code"] == "BU01000100", "pct_age_15_25"]).all()
    assert (b[["pct_age_25_45", "pct_single_households"]].min() > -1).all()
    g = read_layer_attributes(raw / "wijkenbuurten_2025.gpkg", "gemeenten")
    assert len(g) == cbs_raw["municipalities"]  # water feature dropped
    s = read_supplement(raw / "kerncijfers_wijken_buurten_2022.json")
    assert s["code"].str.len().max() <= 10 and not s["code"].str.endswith(" ").any()
    edu = s[["pct_education_low", "pct_education_mid", "pct_education_high"]].sum(axis=1)
    assert np.allclose(edu.dropna(), 100.0)


def test_download_offline_reuses_files_and_writes_provenance(cbs_raw: dict[str, Any]) -> None:
    records = download_all(cbs_raw["year"])
    assert {r.key for r in records} == set(load_geography_config().sources)
    assert not any(r.downloaded for r in records)
    payload = json.loads((cbs_raw["data_dir"] / "raw" / "sources_2025.json").read_text())
    for rec in payload["sources"].values():
        assert len(rec["sha256"]) == 64 and rec["bytes"] > 0 and rec["license"] == "CC BY 4.0"
        assert rec["url"].startswith("https://") and rec["retrieved_at"]


def test_build_store_contract(built: dict[str, Any]) -> None:
    facts, report = built["facts"], built["report"]
    cfg = load_geography_config()
    base = report.path
    for name in [*store_files(cfg), "manifest.json"]:
        assert (base / name).exists(), name
    assert report.counts["provinces"] == 12
    assert report.counts["municipalities"] == facts["municipalities"]
    assert report.counts["units"] == facts["n_units"]
    assert report.counts["population"] == facts["population_total"]

    units = store.load_units_gdf(2025)
    assert units.crs.to_epsg() == 28992
    for col in (
        "code",
        "wijk_code",
        "municipality_code",
        "province_code",
        "population",
        "eligible_voters_est",
        "density",
        "urbanity_class",
        "centroid_lon",
        "centroid_lat",
        *DEMOGRAPHIC_VARIABLES,
        "imputed_fields",
    ):
        assert col in units.columns, col
    assert not units[list(ALL_DEMOGRAPHICS)].isna().any().any()
    assert (units["eligible_voters_est"] <= units["population"]).all()
    munis = store.load_municipalities_gdf(2025)
    assert (
        munis.set_index("code")["population"] == units.groupby("municipality_code")["population"].sum()
    ).all()
    provinces = store.load_provinces_gdf(2025)
    assert list(provinces["code"]) == cfg.province_codes

    frame = store.load_frame(2025)
    assert frame.validate() == []
    assert frame.n_units == facts["n_units"] and frame.province_codes == cfg.province_codes
    assert store.load_frame(2025) is frame  # cached

    manifest = store.manifest(2025)
    assert manifest["counts"]["units"] == facts["n_units"]
    assert set(manifest["sources"]) == set(cfg.sources)
    assert "timings" in manifest and "library_versions" in manifest


def test_build_imputation_flags(built: dict[str, Any]) -> None:
    facts = built["facts"]
    units = store.load_units_attrs(2025)
    # population residual distributed over the two missing neighbourhoods (exactly, integers)
    miss = units[units["code"].isin(facts["missing_population_units"])]
    assert int(miss["population"].sum()) == facts["residual_GM0105"]
    assert all("population" in _flags(units, c) for c in facts["missing_population_units"])
    # suppressed buurt value → wijk value (flagged)
    assert "pct_age_15_25" in _flags(units, "BU01000100")
    # whole wijk missing → gemeente value (flagged)
    wk = units[units["wijk_code"] == "WK010300"]
    assert all("pct_age_65_plus" in _flags(units, c) for c in wk["code"])
    # municipality without supplement rows → province means (flagged)
    gm = units[units["municipality_code"] == "GM0102"]
    assert all(
        {"pct_education_high", "income_per_capita_keur", "pct_owner_occupied"} <= _flags(units, c)
        for c in gm["code"]
    )
    assert "income_per_capita_keur" in _flags(units, "BU01000500")
    # urbanity: derived from address density (not imputed) vs taken from the wijk (imputed)
    assert "urbanity" not in _flags(units, "BU01000300")
    assert "urbanity" in _flags(units, "BU01000400")
    assert units["urbanity_class"].between(1, 5).all()
    # an untouched neighbourhood carries no flags
    assert _flags(units, "BU05050500") == set()
    frame = store.load_frame(2025)
    col = frame.demo_names.index("pct_age_15_25")
    assert frame.unit_demo_imputed[frame.unit_index("BU01000100"), col]


def test_build_island_gets_water_link_and_web_layers(built: dict[str, Any]) -> None:
    facts = built["facts"]
    adj = store.load_unit_adjacency(2025)
    island = facts["island_code"]
    links = adj[(adj["a"] == island) | (adj["b"] == island)]
    assert len(links) == 1 and links["kind"].iloc[0] == "water_link"
    assert links["gap_m"].iloc[0] == pytest.approx(3000.0, abs=1.0)
    assert (adj["a"] < adj["b"]).all()
    manifest = store.manifest(2025)
    assert manifest["counts"]["water_links"] >= 1
    madj = store.load_municipality_adjacency(2025)
    assert set(madj.columns) >= {"a", "b", "shared_border_m", "kind"}

    web = pyogrio.read_dataframe(store.web_geojson_path(2025, "units", province="GR"))
    assert web.crs.to_epsg() == 4326
    assert set(web.columns) >= {"code", "name", "province_code", "population"}
    assert island in set(web["code"])
    raw = json.loads(store.web_geojson_path(2025, "municipalities").read_text())
    coords = np.array(raw["features"][0]["geometry"]["coordinates"][0][0], dtype=float)
    assert np.allclose(coords, np.round(coords, 5))
    with pytest.raises(ValueError):
        store.web_geojson_path(2025, "units")


def test_build_idempotent_and_deterministic(built: dict[str, Any]) -> None:
    first = built["report"]
    again = build_geography(2025)
    assert again.skipped
    forced = build_geography(2025, force=True)
    assert not forced.skipped
    assert forced.manifest["fingerprint"] == first.manifest["fingerprint"]
    for name, digest in first.manifest["files"].items():
        if name.endswith(".parquet") or name.endswith(".geojson"):
            assert forced.manifest["files"][name]["sha256"] == digest["sha256"], name


def test_store_missing_raises(tmp_data_dir: object) -> None:
    assert not store.is_prepared(2025)
    with pytest.raises(DataNotPreparedError):
        store.load_frame(2025)
    with pytest.raises(DataNotPreparedError):
        store.manifest(2025)
    with pytest.raises(DataNotPreparedError):
        build_geography(2025)  # raw sources missing and network disabled


def test_build_compositions_stay_consistent(built: dict[str, Any]) -> None:
    """Partially suppressed age bands / origin shares are filled jointly (sums stay ≈ 100 %)."""
    from app.geography.cbs import CORE_COMPOSITIONS

    units = store.load_units_attrs(2025)
    for parts in CORE_COMPOSITIONS.values():
        sums = units[list(parts)].sum(axis=1)
        assert sums.between(99.5, 100.5).all(), units.loc[~sums.between(99.5, 100.5), "code"].tolist()
    age = list(CORE_COMPOSITIONS["age"])
    row = units.set_index("code").loc["BU01000600"]
    flags = _flags(units, "BU01000600")
    assert {"pct_age_45_65", "pct_age_65_plus"} <= flags
    assert not {"pct_age_0_15", "pct_age_15_25", "pct_age_25_45"} & flags
    raw = built["facts"]["data_dir"] / "raw" / "wijkenbuurten_2025.gpkg"
    wijk = read_layer_attributes(raw, "wijken").set_index("code").loc[row["wijk_code"]]
    assert row["pct_age_45_65"] / row["pct_age_65_plus"] == pytest.approx(
        wijk["pct_age_45_65"] / wijk["pct_age_65_plus"]
    )
    assert row[age].sum() == pytest.approx(100.0)
    assert _flags(units, "BU01000700") & set(CORE_COMPOSITIONS["origin"]) == {"pct_origin_europe"}


def test_build_web_layers_are_valid(built: dict[str, Any]) -> None:
    import shapely

    cfg = load_geography_config()
    for layer in ["provinces", "municipalities", *(f"units/{c}" for c in cfg.province_codes)]:
        web = pyogrio.read_dataframe(store.web_geojson_path(2025, layer))
        geoms = np.asarray(web.geometry.values, dtype=object)
        assert shapely.is_valid(geoms).all() and not shapely.is_empty(geoms).any(), layer


def test_store_web_path_rejects_unknown_layers(built: dict[str, Any]) -> None:
    assert store.web_geojson_path(2025, "units/GR.geojson") == store.web_geojson_path(2025, "units", "GR")
    for bad in ("units/../manifest", "units/XX", "units/../../raw/x", "districts"):
        with pytest.raises(ValueError):
            store.web_geojson_path(2025, bad)
    with pytest.raises(ValueError):
        store.web_geojson_path(2025, "units", province="../web/provinces")


def test_frame_cache_is_keyed_by_store_directory(
    built: dict[str, Any], tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two data directories whose manifests share an mtime must not share a cached frame."""
    import os
    import shutil

    from app.core.settings import get_settings, reset_settings_cache

    first = store.load_frame(2025)
    src = get_settings().processed_geo_dir(2025)
    other_data = tmp_path / "other"
    dst = other_data / "processed" / src.name
    shutil.copytree(src, dst)
    attrs = pd.read_parquet(dst / "units_attrs.parquet")
    attrs.loc[attrs["code"] == "BU01000000", "population"] += 11
    attrs.to_parquet(dst / "units_attrs.parquet", index=False)
    st = (src / "manifest.json").stat()
    os.utime(dst / "manifest.json", ns=(st.st_atime_ns, st.st_mtime_ns))  # identical manifest mtime
    monkeypatch.setenv("NLFED_DATA_DIR", str(other_data))
    reset_settings_cache()
    second = store.load_frame(2025)
    assert second is not first
    i = second.unit_index("BU01000000")
    assert second.unit_population[i] == first.unit_population[i] + 11
    # manifest copies are independent of the cache
    m = store.manifest(2025)
    m["counts"]["units"] = -1
    assert store.manifest(2025)["counts"]["units"] != -1
