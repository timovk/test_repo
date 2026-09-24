"""Invariants of the REAL processed CBS store (skipped when it has not been built)."""

from __future__ import annotations

import json

import numpy as np
import pyogrio
import pytest

from app.core.constitution import PROVINCE_COUNT
from app.geography import store
from app.geography.adjacency import component_counts
from app.geography.config import load_geography_config
from app.geography.frame import GeographyFrame

pytestmark = pytest.mark.realdata


def test_frame_invariants(real_frame: GeographyFrame) -> None:
    assert real_frame.validate() == []
    assert real_frame.n_provinces == PROVINCE_COUNT
    assert real_frame.province_codes == load_geography_config().province_codes
    assert real_frame.n_units > 14_000
    assert (real_frame.unit_province >= 0).all() and (real_frame.unit_muni >= 0).all()
    total = int(real_frame.unit_population.sum())
    assert 17_500_000 < total < 18_600_000
    assert 0.7 < real_frame.unit_eligible.sum() / total < 0.8
    assert not np.isnan(real_frame.unit_demo).any()
    assert (real_frame.province_population() > 300_000).all()


def test_municipalities_match_generalized_layer(real_frame: GeographyFrame) -> None:
    manifest = store.manifest()
    raw = manifest["sources"]["municipalities_generalized"]["path"]
    from app.core.settings import get_settings

    path = get_settings().project_root / raw if not raw.startswith("/") else raw
    layer = pyogrio.read_dataframe(path, read_geometry=False)
    assert real_frame.n_munis == len(layer) == manifest["counts"]["municipalities"]
    assert set(real_frame.muni_codes) == set(layer["statcode"])


def test_every_unit_assigned_and_hierarchy_consistent() -> None:
    units = store.load_units_attrs()
    munis = store.load_municipalities_gdf(
        columns=["code", "province_code", "population", "population_official"]
    )
    assert units["province_code"].notna().all() and units["municipality_code"].notna().all()
    mp = munis.set_index("code")["province_code"]
    assert (units["municipality_code"].map(mp) == units["province_code"]).all()
    sums = units.groupby("municipality_code")["population"].sum()
    assert (sums.reindex(munis["code"]).to_numpy() == munis["population"].to_numpy()).all()
    # CBS rounds neighbourhood figures: canonical sums stay within 0.1 % of the official totals
    rel = (munis["population"] - munis["population_official"]).abs().sum() / munis[
        "population_official"
    ].sum()
    assert rel < 1e-3


def test_province_graphs_connected() -> None:
    units = store.load_units_attrs(columns=["code", "province_code", "municipality_code"])
    adj = store.load_unit_adjacency()
    assert set(adj["kind"]) <= {"border", "water_link"}
    assert (adj["a"] < adj["b"]).all()
    assert set(component_counts(adj, units, "province_code").values()) == {1}
    munis = store.load_municipalities_gdf(columns=["code", "province_code"])
    madj = store.load_municipality_adjacency()
    assert set(component_counts(madj, munis, "province_code").values()) == {1}
    # the Wadden islands of Fryslân need explicit water links
    links = adj[adj["kind"] == "water_link"]
    assert len(links) >= 1 and (links["gap_m"] > 0).all()


def test_web_layers() -> None:
    cfg = load_geography_config()
    prov = json.loads(store.web_geojson_path(None, "provinces").read_text())
    assert len(prov["features"]) == PROVINCE_COUNT
    assert set(prov["features"][0]["properties"]) >= {"code", "name", "province_code"}
    for code in cfg.province_codes:
        assert store.web_geojson_path(None, "units", province=code).exists()
    munis = pyogrio.read_dataframe(store.web_geojson_path(None, "municipalities"))
    assert munis.crs.to_epsg() == 4326 and len(munis) == store.manifest()["counts"]["municipalities"]


def _schema_version() -> int:
    return int(store.manifest().get("schema_version", 1))


def test_compositions_consistent() -> None:
    """Age bands and migration background sum to 100 % up to CBS rounding (imputed rows exactly)."""
    if _schema_version() < 2:
        pytest.skip("store predates compositional imputation (schema 2): rebuild with build_geography")
    from app.geography.cbs import CORE_COMPOSITIONS

    units = store.load_units_attrs()
    # published parts are whole percentages (age) or multiples of 5 (origin) → bounded spread
    for name, (lo, hi) in {"age": (97.0, 103.0), "origin": (92.0, 108.0)}.items():
        parts = list(CORE_COMPOSITIONS[name])
        sums = units[parts].sum(axis=1)
        assert sums.between(lo, hi).all(), name
        imputed = units["imputed_fields"].str.split(",").apply(lambda f, p=parts: bool(set(f) & set(p)))
        partial = imputed & sums.round(6).ne(100.0)
        # an imputed row only deviates from 100 when its published parts alone already exceed it
        assert (sums[partial] > 100.0).all(), name


def test_web_layers_valid() -> None:
    if _schema_version() < 2:
        pytest.skip("store predates valid web geometry (schema 2): rebuild with build_geography")
    import shapely

    cfg = load_geography_config()
    for layer in ["provinces", "municipalities", *(f"units/{c}" for c in cfg.province_codes)]:
        web = pyogrio.read_dataframe(store.web_geojson_path(None, layer))
        geoms = np.asarray(web.geometry.values, dtype=object)
        assert shapely.is_valid(geoms).all() and not shapely.is_empty(geoms).any(), layer


def test_store_files_match_manifest_digests() -> None:
    import hashlib

    base = store.store_dir()
    for name, meta in store.manifest()["files"].items():
        path = base / name
        assert path.stat().st_size == meta["bytes"], name
        h = hashlib.sha256()
        with path.open("rb") as fh:
            while block := fh.read(1 << 22):
                h.update(block)
        assert h.hexdigest() == meta["sha256"], name


def test_load_real_store_into_db(db_session) -> None:  # type: ignore[no-untyped-def]
    import time

    import shapely
    from sqlalchemy import func, select

    from app.geography.loader_db import load_into_db
    from app.models import GeoUnit, Municipality, ProvinceStats

    t0 = time.perf_counter()
    vintage = load_into_db(db_session)
    elapsed = time.perf_counter() - t0
    manifest = store.manifest()
    assert vintage.unit_count == manifest["counts"]["units"]
    assert vintage.population_total == manifest["counts"]["population"]
    assert db_session.scalar(select(func.count()).select_from(GeoUnit)) == manifest["counts"]["units"]
    assert db_session.scalar(select(func.count()).select_from(ProvinceStats)) == PROVINCE_COUNT
    for model in (ProvinceStats, Municipality):
        for (wkb,) in db_session.execute(select(model.geometry_wkb)):
            assert shapely.from_wkb(wkb).is_valid
    assert elapsed < 30.0
    t0 = time.perf_counter()
    assert load_into_db(db_session).id == vintage.id  # idempotent and fast
    assert time.perf_counter() - t0 < 5.0
