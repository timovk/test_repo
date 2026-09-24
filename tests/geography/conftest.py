"""Fixtures for the geography pipeline tests.

``cbs_raw`` writes a synthetic *raw* data directory in the exact CBS/PDOK formats (GeoPackage with
``buurten``/``wijken``/``gemeenten`` layers and CBS missing-value codes, generalised GeoJSON layers,
StatLine OData JSON) derived from the synthetic toy country, with deliberate gaps:

* two neighbourhoods of ``GM0105`` have a missing population (official municipal total is 500 higher
  than the known sum → residual distributed);
* ``BU01000100`` has a suppressed 15–25 age share (→ wijk value);
* every neighbourhood of wijk ``WK010300`` lacks the 65+ share and so does the wijk (→ gemeente);
* ``BU01000600`` lacks two of its five age bands and ``BU01000700`` one of its three origin shares
  (→ compositional fill: published parts kept, remainder split by the wijk's composition);
* ``GM0102`` has no supplement rows at all (→ province mean for education/income/housing);
* ``BU01000300`` lacks its CBS urbanity class but has an address density (→ derived, not imputed);
* ``BU01000400`` lacks both (→ wijk class, imputed);
* a water neighbourhood and a water municipality feature (must be ignored);
* an island neighbourhood ``BU01999900`` (GM0103) 3 km off the Groningen coast (→ water link).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import pyogrio
import pytest
from shapely.geometry import box

from app.core.config import clear_config_cache
from app.core.settings import get_settings, reset_settings_cache
from app.geography import store
from app.geography.cbs import CORE_OTHER_COLUMNS, CORE_PERCENT_COLUMNS
from app.geography.synthetic import SyntheticGeography

MISSING = -99997
YEAR = 2025
ISLAND_CODE = "BU01999900"


@pytest.fixture()
def tmp_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point ``settings.data_dir`` to a temporary directory with network access disabled."""
    data_dir = tmp_path / "data"
    (data_dir / "raw").mkdir(parents=True)
    monkeypatch.setenv("NLFED_DATA_DIR", str(data_dir))
    monkeypatch.setenv("NLFED_ALLOW_NETWORK", "false")
    reset_settings_cache()
    clear_config_cache()
    store.clear_cache()
    try:
        yield data_dir
    finally:
        monkeypatch.undo()
        reset_settings_cache()
        clear_config_cache()
        store.clear_cache()


def _unit_frame(synthetic: SyntheticGeography) -> gpd.GeoDataFrame:
    u = synthetic.units.copy()
    u["pct_age_0_15"] = 16.0 - 0.1 * u["pct_age_15_25"]
    u["pct_age_45_65"] = 100.0 - u[["pct_age_0_15", "pct_age_15_25", "pct_age_25_45", "pct_age_65_plus"]].sum(
        axis=1
    )
    u["pct_origin_nl"] = 100.0 - u["pct_origin_europe"] - u["pct_origin_non_europe"]
    u["avg_household_size"] = 2.1
    u["pct_education_mid"] = 100.0 - u["pct_education_high"] - u["pct_education_low"]
    # island neighbourhood of GM0103 (Groningen): 3 km north of the province square
    gr = synthetic.provinces.geometry.iloc[0].bounds
    island = u[u["code"] == "BU01000000"].copy()
    island["code"] = ISLAND_CODE
    island["name"] = "Eiland"
    island["wijk_code"] = "WK010399"
    island["municipality_code"] = "GM0103"
    island["population"] = 900
    island["geometry"] = [box(gr[0] + 10_000, gr[3] + 3_000, gr[0] + 12_000, gr[3] + 5_000)]
    island["area_km2"] = island["land_area_km2"] = 4.0
    return gpd.GeoDataFrame(pd.concat([u, island], ignore_index=True), geometry="geometry", crs=28992)


def _pw_mean(df: pd.DataFrame, by: str, cols: list[str]) -> pd.DataFrame:
    w = df["population"].astype(float).clip(lower=1.0)
    out = {c: (df[c] * w).groupby(df[by]).sum() / w.groupby(df[by]).sum() for c in cols}
    return pd.DataFrame(out)


def _cbs_layer(frame: pd.DataFrame, pct: pd.DataFrame, extra: dict[str, Any]) -> pd.DataFrame:
    out = pd.DataFrame(extra)
    for var, col in {**CORE_PERCENT_COLUMNS, **CORE_OTHER_COLUMNS}.items():
        out[col] = pct[var].to_numpy()
    return out


def write_cbs_raw(raw_dir: Path, synthetic: SyntheticGeography) -> dict[str, Any]:
    """Write CBS-format raw files for the synthetic country; returns facts used by the tests."""
    u = _unit_frame(synthetic)
    core = list(CORE_PERCENT_COLUMNS) + list(CORE_OTHER_COLUMNS)
    facts: dict[str, Any] = {}

    # ------------------------------------------------------------------ buurten (with gaps)
    b = u.copy()
    pop = b["population"].astype(float)
    missing_pop = b.index[b["municipality_code"] == "GM0105"][:2]
    facts["missing_population_units"] = b.loc[missing_pop, "code"].tolist()
    facts["missing_population_true"] = int(pop[missing_pop].sum())
    pop_out = pop.copy()
    pop_out[missing_pop] = MISSING
    urb = b["urbanity_class"].astype(float).copy()
    addr = b["address_density"].astype(float).copy()
    urb[b["code"] == "BU01000300"] = MISSING
    urb[b["code"] == "BU01000400"] = MISSING
    addr[b["code"] == "BU01000400"] = MISSING
    buurt_pct = b[core].copy()
    buurt_pct.loc[b["code"] == "BU01000100", "pct_age_15_25"] = MISSING
    buurt_pct.loc[b["wijk_code"] == "WK010300", "pct_age_65_plus"] = MISSING
    buurt_pct.loc[b["code"] == "BU01000600", ["pct_age_45_65", "pct_age_65_plus"]] = MISSING
    buurt_pct.loc[b["code"] == "BU01000700", "pct_origin_europe"] = MISSING
    buurten = _cbs_layer(
        b,
        buurt_pct,
        {
            "buurtcode": b["code"],
            "buurtnaam": b["name"],
            "wijkcode": b["wijk_code"],
            "gemeentecode": b["municipality_code"],
            "gemeentenaam": "Gemeente " + b["municipality_code"],
            "water": "NEE",
            "aantal_inwoners": pop_out.round().astype(int),
            "stedelijkheid_adressen_per_km2": urb.round().astype(int),
            "omgevingsadressendichtheid": addr.round().astype(int),
            "bevolkingsdichtheid_inwoners_per_km2": b["density"].round().astype(int),
            "oppervlakte_totaal_in_ha": (b["area_km2"] * 100).round().astype(int),
            "oppervlakte_land_in_ha": (b["land_area_km2"] * 100).round().astype(int),
        },
    )
    water_row = buurten.iloc[[0]].copy()
    water_row["buurtcode"] = "BU01009900"
    water_row["water"] = "JA"
    for col in buurten.columns:
        if col not in ("buurtcode", "buurtnaam", "wijkcode", "gemeentecode", "gemeentenaam", "water"):
            water_row[col] = MISSING
    buurten = pd.concat([buurten, water_row], ignore_index=True)
    water_geom = box(0, 0, 1000, 1000)
    buurt_gdf = gpd.GeoDataFrame(buurten, geometry=[*u.geometry.tolist(), water_geom], crs=28992)

    # ------------------------------------------------------------------ wijken / gemeenten
    wijk_pct = _pw_mean(u, "wijk_code", core)
    wijk_pct.loc["WK010300", "pct_age_65_plus"] = MISSING
    wijk_pop = u.groupby("wijk_code")["population"].sum()
    wijk_urb = u.groupby("wijk_code")["urbanity_class"].median().round()
    wijken = _cbs_layer(
        u,
        wijk_pct,
        {
            "wijkcode": wijk_pct.index,
            "wijknaam": "Wijk " + wijk_pct.index,
            "water": "NEE",
            "aantal_inwoners": wijk_pop.reindex(wijk_pct.index).to_numpy(),
            "stedelijkheid_adressen_per_km2": wijk_urb.reindex(wijk_pct.index).astype(int).to_numpy(),
            "omgevingsadressendichtheid": 1000,
            "oppervlakte_totaal_in_ha": 1600,
            "oppervlakte_land_in_ha": 1600,
        },
    )
    wijk_geom = u.dissolve(by="wijk_code").reindex(wijk_pct.index).geometry.to_numpy()
    wijk_gdf = gpd.GeoDataFrame(wijken, geometry=list(wijk_geom), crs=28992)

    gm_pct = _pw_mean(u, "municipality_code", core)
    gm_pop = u.groupby("municipality_code")["population"].sum().astype(int)
    known = pop_out.where(pop_out >= 0, 0).groupby(u["municipality_code"]).sum()
    gm_pop.loc["GM0105"] = int(known.loc["GM0105"]) + 500
    facts["residual_GM0105"] = 500
    gemeenten = _cbs_layer(
        u,
        gm_pct,
        {
            "gemeentecode": gm_pct.index,
            "gemeentenaam": "Gemeente " + gm_pct.index,
            "water": "NEE",
            "aantal_inwoners": gm_pop.reindex(gm_pct.index).to_numpy(),
            "stedelijkheid_adressen_per_km2": 3,
            "omgevingsadressendichtheid": 1200,
            "oppervlakte_totaal_in_ha": 10000,
            "oppervlakte_land_in_ha": 10000,
        },
    )
    muni_geom = u.dissolve(by="municipality_code").reindex(gm_pct.index).geometry.to_numpy()
    water_gm = gemeenten.iloc[[0]].copy()
    water_gm["water"] = "JA"
    for col in gemeenten.columns:
        if col not in ("gemeentecode", "gemeentenaam", "water"):
            water_gm[col] = MISSING
    gemeenten = pd.concat([gemeenten, water_gm], ignore_index=True)
    gm_gdf = gpd.GeoDataFrame(gemeenten, geometry=[*muni_geom, water_geom], crs=28992)

    gpkg = raw_dir / f"wijkenbuurten_{YEAR}.gpkg"
    for layer, gdf in (("buurten", buurt_gdf), ("wijken", wijk_gdf), ("gemeenten", gm_gdf)):
        pyogrio.write_dataframe(gdf, gpkg, layer=layer, driver="GPKG", promote_to_multi=True)

    # ------------------------------------------------------------------ generalised layers
    prov = synthetic.provinces
    prov_gdf = gpd.GeoDataFrame(
        {"statcode": prov["cbs_code"], "statnaam": prov["name"]}, geometry=prov.geometry.values, crs=28992
    ).to_crs(4326)
    pyogrio.write_dataframe(
        prov_gdf, raw_dir / f"provincies_gegeneraliseerd_{YEAR}.geojson", driver="GeoJSON"
    )
    mg = gpd.GeoDataFrame(
        {"statcode": gm_pct.index, "statnaam": "Gemeente " + gm_pct.index},
        geometry=list(muni_geom),
        crs=28992,
    )
    pyogrio.write_dataframe(mg, raw_dir / f"gemeenten_gegeneraliseerd_{YEAR}.geojson", driver="GeoJSON")
    facts["municipalities"] = len(gm_pct)

    # ------------------------------------------------------------------ StatLine supplement
    rows: list[dict[str, Any]] = []

    def add(code: str, kind: str, df: pd.DataFrame) -> None:
        w = df["population"].astype(float).clip(lower=1.0)
        n = float(df["population"].sum())
        mean = {
            c: float((df[c] * w).sum() / w.sum())
            for c in (
                "pct_education_low",
                "pct_education_high",
                "income_per_capita_keur",
                "pct_owner_occupied",
            )
        }
        base = max(n, 10.0)
        rows.append(
            {
                "Codering_3": f"{code:<10}",
                "SoortRegio_2": f"{kind:<10}",
                "AantalInwoners_5": int(n),
                "Koopwoningen_40": round(mean["pct_owner_occupied"]),
                "OpleidingsniveauLaag_64": round(base * mean["pct_education_low"] / 100),
                "OpleidingsniveauMiddelbaar_65": round(
                    base * (100 - mean["pct_education_low"] - mean["pct_education_high"]) / 100
                ),
                "OpleidingsniveauHoog_66": round(base * mean["pct_education_high"] / 100),
                "GemiddeldInkomenPerInwoner_72": round(mean["income_per_capita_keur"], 1),
            }
        )

    sup_units = u[(u["municipality_code"] != "GM0102") & (u["code"] != ISLAND_CODE)]
    for code, df in sup_units.groupby("code"):
        add(str(code), "Buurt", df)
    for code, df in sup_units.groupby("wijk_code"):
        add(str(code), "Wijk", df)
    for code, df in sup_units.groupby("municipality_code"):
        add(str(code), "Gemeente", df)
    add("NL00", "Land", u)
    for r in rows:  # suppressed income of one neighbourhood (→ wijk value, flagged)
        if r["Codering_3"].strip() == "BU01000500":
            r["GemiddeldInkomenPerInwoner_72"] = None
    (raw_dir / "kerncijfers_wijken_buurten_2022.json").write_text(
        json.dumps({"value": rows}), encoding="utf-8"
    )
    facts["n_units"] = len(u)
    facts["island_code"] = ISLAND_CODE
    facts["year"] = YEAR
    facts["population_total"] = int(pop.sum()) - facts["missing_population_true"] + 500
    return facts


@pytest.fixture()
def cbs_raw(tmp_data_dir: Path, synthetic: SyntheticGeography) -> dict[str, Any]:
    """Synthetic raw files in CBS formats inside a temporary data dir (network disabled)."""
    facts = write_cbs_raw(get_settings().raw_dir, synthetic)
    facts["data_dir"] = tmp_data_dir
    return facts
