"""Readers and cleaners for the raw CBS / PDOK source files (REAL data).

* :func:`read_buurten` — land neighbourhoods of the CBS *Wijk- en Buurtkaart* GeoPackage;
* :func:`read_layer_attributes` — the ``wijken`` / ``gemeenten`` layers (attributes only);
* :func:`read_supplement` — the CBS StatLine *Kerncijfers wijken en buurten* OData dump
  (education, income, owner-occupied housing) as one row per region code;
* :func:`read_generalized` — PDOK *gebiedsindelingen* GeoJSON (provinces / municipalities).

CBS encodes suppressed or unavailable values as large negative numbers (``-99997``,
``-99999999`` …); :func:`clean_missing` turns every value ``<= -99990`` into ``NaN``.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio

from app.core.errors import DataNotPreparedError
from app.core.logging import get_logger

log = get_logger(__name__)

#: Every value at or below this threshold is a CBS missing-value code.
MISSING_THRESHOLD: float = -99990.0

#: Land / water flag of the Wijk- en Buurtkaart (``water`` column).
WATER_LAND = "NEE"

#: Core demographic indicators of the Wijk- en Buurtkaart → canonical variable names (0–100 %).
CORE_PERCENT_COLUMNS: dict[str, str] = {
    "pct_age_0_15": "percentage_personen_0_tot_15_jaar",
    "pct_age_15_25": "percentage_personen_15_tot_25_jaar",
    "pct_age_25_45": "percentage_personen_25_tot_45_jaar",
    "pct_age_45_65": "percentage_personen_45_tot_65_jaar",
    "pct_age_65_plus": "percentage_personen_65_jaar_en_ouder",
    "pct_single_households": "percentage_eenpersoonshuishoudens",
    "pct_households_with_children": "percentage_huishoudens_met_kinderen",
    "pct_origin_nl": "percentage_met_herkomstland_nederland",
    "pct_origin_europe": "percentage_met_herkomstland_uit_europa_excl_nl",
    "pct_origin_non_europe": "percentage_met_herkomstland_buiten_europa",
}
#: Non-percentage core indicators.
CORE_OTHER_COLUMNS: dict[str, str] = {
    "avg_household_size": "gemiddelde_huishoudsgrootte",
}
CORE_VARIABLES: tuple[str, ...] = (*CORE_PERCENT_COLUMNS, *CORE_OTHER_COLUMNS)

#: Core indicators that are parts of one composition (they sum to 100 % up to CBS rounding);
#: gaps are filled jointly so the parts stay consistent (see ``demographics.fill_composition``).
CORE_COMPOSITIONS: dict[str, tuple[str, ...]] = {
    "age": ("pct_age_0_15", "pct_age_15_25", "pct_age_25_45", "pct_age_45_65", "pct_age_65_plus"),
    "origin": ("pct_origin_nl", "pct_origin_europe", "pct_origin_non_europe"),
}

#: Supplement (StatLine 85318NED) fields → canonical names.
SUPPLEMENT_FIELDS: dict[str, str] = {
    "Codering_3": "code",
    "SoortRegio_2": "region_type",
    "AantalInwoners_5": "population_supplement",
    "Koopwoningen_40": "pct_owner_occupied",
    "OpleidingsniveauLaag_64": "education_low_count",
    "OpleidingsniveauMiddelbaar_65": "education_mid_count",
    "OpleidingsniveauHoog_66": "education_high_count",
    "GemiddeldInkomenPerInwoner_72": "income_per_capita_keur",
}
SUPPLEMENT_VARIABLES: tuple[str, ...] = (
    "pct_education_low",
    "pct_education_mid",
    "pct_education_high",
    "income_per_capita_keur",
    "pct_owner_occupied",
)

#: CBS *stedelijkheid* classes from the surrounding address density (addresses per km²):
#: ≥ 2500 → 1 (very strongly urban), 1500–2500 → 2, 1000–1500 → 3, 500–1000 → 4, < 500 → 5.
URBANITY_THRESHOLDS: tuple[float, ...] = (500.0, 1000.0, 1500.0, 2500.0)

_BUURT_COLUMNS: dict[str, str] = {
    "buurtcode": "code",
    "buurtnaam": "name",
    "wijkcode": "wijk_code",
    "gemeentecode": "municipality_code",
    "gemeentenaam": "municipality_name",
    "water": "water",
    "aantal_inwoners": "population_raw",
    "stedelijkheid_adressen_per_km2": "urbanity_class_raw",
    "omgevingsadressendichtheid": "address_density",
    "bevolkingsdichtheid_inwoners_per_km2": "density_cbs",
    "oppervlakte_totaal_in_ha": "area_ha",
    "oppervlakte_land_in_ha": "land_area_ha",
}
_LAYER_KEYS = {"wijken": ("wijkcode", "wijknaam"), "gemeenten": ("gemeentecode", "gemeentenaam")}


def clean_missing(values: pd.Series | np.ndarray) -> np.ndarray:
    """Float array with every CBS missing-value code (``<= -99990``) and NaN replaced by NaN."""
    arr = pd.to_numeric(pd.Series(np.asarray(values)), errors="coerce").to_numpy(dtype=float)
    arr[arr <= MISSING_THRESHOLD] = np.nan
    return arr


def urbanity_class_from_address_density(address_density: np.ndarray) -> np.ndarray:
    """CBS urbanity class (1 = very urban … 5 = not urban) from address density; NaN stays NaN."""
    ad = np.asarray(address_density, dtype=float)
    cls = 5.0 - np.digitize(ad, URBANITY_THRESHOLDS).astype(float)
    cls[np.isnan(ad)] = np.nan
    return cls


def _is_land(water: pd.Series) -> pd.Series:
    """Mask of land features (``water == 'NEE'``, tolerant of case and padding)."""
    return water.astype(str).str.strip().str.upper() == WATER_LAND


def _require(path: Path) -> Path:
    if not path.exists():
        raise DataNotPreparedError(
            f"Raw source {path.name} not found in {path.parent}. Run `python -m app geography download`."
        )
    return path


def read_buurten(gpkg: Path) -> gpd.GeoDataFrame:
    """Land neighbourhoods (``water == 'NEE'``) with cleaned attributes, sorted by code.

    Columns: code, name, wijk_code, municipality_code, municipality_name, population_raw (NaN =
    missing), urbanity_class_raw, address_density, density_cbs, area_ha, land_area_ha, the
    :data:`CORE_VARIABLES`, geometry (EPSG:28992).
    """
    _require(gpkg)
    wanted = list(_BUURT_COLUMNS) + list(CORE_PERCENT_COLUMNS.values()) + list(CORE_OTHER_COLUMNS.values())
    gdf = pyogrio.read_dataframe(gpkg, layer="buurten", columns=wanted)
    gdf = gdf[_is_land(gdf["water"])].copy()
    gdf = gdf.rename(columns=_BUURT_COLUMNS)
    for var, col in {**CORE_PERCENT_COLUMNS, **CORE_OTHER_COLUMNS}.items():
        gdf[var] = clean_missing(gdf.pop(col))
    for col in (
        "population_raw",
        "urbanity_class_raw",
        "address_density",
        "density_cbs",
        "area_ha",
        "land_area_ha",
    ):
        gdf[col] = clean_missing(gdf[col])
    for col in ("code", "name", "wijk_code", "municipality_code", "municipality_name"):
        gdf[col] = gdf[col].astype(str).str.strip()
    gdf = gdf.drop(columns=["water"]).sort_values("code", kind="mergesort").reset_index(drop=True)
    if gdf.crs is None or gdf.crs.to_epsg() != 28992:
        gdf = gdf.set_crs(28992, allow_override=True) if gdf.crs is None else gdf.to_crs(28992)
    return gdf


def read_layer_attributes(gpkg: Path, layer: str) -> pd.DataFrame:
    """Attributes (no geometry) of the ``wijken`` or ``gemeenten`` layer, land features only.

    Columns: code, name, population_raw, urbanity_class_raw, address_density, area_ha,
    land_area_ha and the :data:`CORE_VARIABLES`.
    """
    _require(gpkg)
    code_col, name_col = _LAYER_KEYS[layer]
    cols = [
        code_col,
        name_col,
        "water",
        "aantal_inwoners",
        "stedelijkheid_adressen_per_km2",
        "omgevingsadressendichtheid",
        "oppervlakte_totaal_in_ha",
        "oppervlakte_land_in_ha",
        *CORE_PERCENT_COLUMNS.values(),
        *CORE_OTHER_COLUMNS.values(),
    ]
    df = pyogrio.read_dataframe(gpkg, layer=layer, columns=cols, read_geometry=False)
    df = df[_is_land(df["water"])].copy()
    out = pd.DataFrame(
        {
            "code": df[code_col].astype(str).str.strip(),
            "name": df[name_col].astype(str).str.strip(),
            "population_raw": clean_missing(df["aantal_inwoners"]),
            "urbanity_class_raw": clean_missing(df["stedelijkheid_adressen_per_km2"]),
            "address_density": clean_missing(df["omgevingsadressendichtheid"]),
            "area_ha": clean_missing(df["oppervlakte_totaal_in_ha"]),
            "land_area_ha": clean_missing(df["oppervlakte_land_in_ha"]),
        }
    )
    for var, col in {**CORE_PERCENT_COLUMNS, **CORE_OTHER_COLUMNS}.items():
        out[var] = clean_missing(df[col])
    return out.drop_duplicates("code").sort_values("code").reset_index(drop=True)


def parse_supplement(rows: list[dict]) -> pd.DataFrame:
    """Normalise StatLine 85318NED rows: strip codes, derive education shares (0–100 %).

    Returns one row per region code with columns code, region_type, level ('BU'/'WK'/'GM'/'NL'),
    population_supplement and :data:`SUPPLEMENT_VARIABLES`.
    """
    df = pd.DataFrame(rows)
    missing = [f for f in SUPPLEMENT_FIELDS if f not in df.columns]
    if missing:
        raise ValueError(f"supplement is missing fields: {', '.join(missing)}")
    df = df[list(SUPPLEMENT_FIELDS)].rename(columns=SUPPLEMENT_FIELDS)
    df["code"] = df["code"].astype(str).str.strip()
    df["region_type"] = df["region_type"].astype(str).str.strip()
    df["level"] = df["code"].str[:2]
    for col in (
        "population_supplement",
        "pct_owner_occupied",
        "education_low_count",
        "education_mid_count",
        "education_high_count",
        "income_per_capita_keur",
    ):
        df[col] = clean_missing(df[col])
    total = df[["education_low_count", "education_mid_count", "education_high_count"]].sum(
        axis=1, min_count=3
    )
    total = total.where(total > 0)
    df["pct_education_low"] = 100.0 * df["education_low_count"] / total
    df["pct_education_mid"] = 100.0 * df["education_mid_count"] / total
    df["pct_education_high"] = 100.0 * df["education_high_count"] / total
    df = df.drop_duplicates("code").sort_values("code").reset_index(drop=True)
    return df[["code", "region_type", "level", "population_supplement", *SUPPLEMENT_VARIABLES]]


def read_supplement(path: Path) -> pd.DataFrame:
    """Read the merged OData JSON written by :mod:`app.geography.download`."""
    _require(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["value"] if isinstance(payload, dict) else payload
    return parse_supplement(rows)


def read_generalized(path: Path, code_prefix: str) -> gpd.GeoDataFrame:
    """PDOK *gebiedsindelingen* layer as ``cbs_code, name, geometry`` (EPSG:28992)."""
    _require(path)
    gdf = pyogrio.read_dataframe(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(28992)
    elif gdf.crs.to_epsg() != 28992:
        gdf = gdf.to_crs(28992)
    out = gpd.GeoDataFrame(
        {
            "cbs_code": gdf["statcode"].astype(str).str.strip(),
            "name": gdf["statnaam"].astype(str).str.strip(),
        },
        geometry=gdf.geometry.values,
        crs=gdf.crs,
    )
    out = out[out["cbs_code"].str.startswith(code_prefix)]
    return out.sort_values("cbs_code").reset_index(drop=True)
