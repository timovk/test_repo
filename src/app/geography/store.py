"""Read access to the processed geography store ``data/processed/geo_<year>/``.

    from app.geography.store import is_prepared, load_frame
    if is_prepared():
        frame = load_frame()            # cached GeographyFrame (≈1 s, no geometry read)
        units = load_units_gdf(2025)    # full-resolution GeoDataFrame (EPSG:28992)

Every loader raises :class:`~app.core.errors.DataNotPreparedError` when the store (or the requested
file) is missing.
"""

from __future__ import annotations

import copy
import json
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd

from app.core.errors import DataNotPreparedError
from app.core.logging import Timer, get_logger
from app.core.settings import get_settings
from app.geography.config import load_geography_config
from app.geography.frame import DEMOGRAPHIC_VARIABLES, GeographyFrame
from app.geography.synthetic import UNIT_COLUMNS, frame_from_tables

log = get_logger(__name__)

WEB_LAYERS = ("provinces", "municipalities", "units")
_REQUIRED = (
    "manifest.json",
    "provinces.parquet",
    "municipalities.parquet",
    "units.parquet",
    "units_attrs.parquet",
    "unit_adjacency.parquet",
    "municipality_adjacency.parquet",
)
_lock = threading.Lock()


def _year(year: int | None) -> int:
    return int(year or get_settings().geography_year)


def store_dir(year: int | None = None) -> Path:
    """Directory of the processed store for ``year`` (may not exist)."""
    return get_settings().processed_geo_dir(_year(year))


def is_prepared(year: int | None = None) -> bool:
    """True when the processed store for ``year`` is complete."""
    d = store_dir(year)
    return all((d / f).exists() for f in _REQUIRED)


def _require(year: int | None, name: str) -> Path:
    d = store_dir(year)
    path = d / name
    if not is_prepared(year) or not path.exists():
        raise DataNotPreparedError(
            f"Processed geography for {_year(year)} not found at {d}. Run `python -m app geography download` "
            "and `python -m app geography build` first."
        )
    return path


def _stamp(year: int | None) -> tuple[int, str, int]:
    """Cache key: year, store directory and manifest mtime (ns).

    The directory is part of the key because ``NLFED_DATA_DIR`` may change within one process
    (tests, tools); a rebuild rewrites the manifest and so invalidates cached objects.
    """
    path = _require(year, "manifest.json")
    return _year(year), str(path.parent.resolve()), path.stat().st_mtime_ns


# --------------------------------------------------------------------------- manifest
def manifest(year: int | None = None) -> dict[str, Any]:
    """Parsed ``manifest.json`` of the store (a copy; safe to modify)."""
    return copy.deepcopy(_manifest_cached(*_stamp(year)))


@lru_cache(maxsize=8)
def _manifest_cached(year: int, directory: str, _mtime_ns: int) -> dict[str, Any]:
    return json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- frame
def load_frame(year: int | None = None) -> GeographyFrame:
    """The REAL :class:`GeographyFrame` built from ``units_attrs.parquet`` (cached; no geometry read).

    Provinces follow the canonical order of ``config/geography.yaml``.
    """
    key = _stamp(year)
    with _lock:
        return _frame_cached(*key)


@lru_cache(maxsize=4)
def _frame_cached(year: int, directory: str, _mtime_ns: int) -> GeographyFrame:
    d = Path(directory)
    with Timer(log, f"load_frame({year})"):
        unit_cols = [*UNIT_COLUMNS, *DEMOGRAPHIC_VARIABLES, "imputed_fields"]
        units = pd.read_parquet(d / "units_attrs.parquet", columns=unit_cols)
        munis = pd.read_parquet(d / "municipalities.parquet", columns=["code", "name", "province_code"])
        cfg = load_geography_config()
        provinces = pd.DataFrame(
            {
                "code": [p.code for p in cfg.provinces],
                "name": [p.name for p in cfg.provinces],
                "cbs_code": [p.cbs_code for p in cfg.provinces],
            }
        )
        frame = frame_from_tables(units, munis, provinces, year=year)
    return frame


# --------------------------------------------------------------------------- tables
def load_units_attrs(year: int | None = None, columns: list[str] | None = None) -> pd.DataFrame:
    """Unit attributes without geometry (fast)."""
    return pd.read_parquet(_require(year, "units_attrs.parquet"), columns=columns)


def load_units_gdf(year: int | None = None, columns: list[str] | None = None) -> gpd.GeoDataFrame:
    """Full-resolution unit (CBS buurt) polygons with attributes, EPSG:28992."""
    return _read_geo(year, "units.parquet", columns)


def load_municipalities_gdf(year: int | None = None, columns: list[str] | None = None) -> gpd.GeoDataFrame:
    """Municipality polygons (union of their units) with attributes, EPSG:28992."""
    return _read_geo(year, "municipalities.parquet", columns)


def load_provinces_gdf(year: int | None = None) -> gpd.GeoDataFrame:
    """Province polygons in canonical order, EPSG:28992."""
    return _read_geo(year, "provinces.parquet", None)


def _read_geo(year: int | None, name: str, columns: list[str] | None) -> gpd.GeoDataFrame:
    path = _require(year, name)
    if columns is not None and "geometry" not in columns:
        columns = [*columns, "geometry"]
    return gpd.read_parquet(path, columns=columns)


def load_unit_adjacency(year: int | None = None) -> pd.DataFrame:
    """Unit edge table ``a, b, shared_border_m, kind, gap_m`` (``kind``: border | water_link)."""
    return pd.read_parquet(_require(year, "unit_adjacency.parquet"))


def load_municipality_adjacency(year: int | None = None) -> pd.DataFrame:
    """Municipality edge table ``a, b, shared_border_m, kind, gap_m``."""
    return pd.read_parquet(_require(year, "municipality_adjacency.parquet"))


def web_geojson_path(year: int | None, layer: str, province: str | None = None) -> Path:
    """Path of a web GeoJSON layer: ``'provinces'``, ``'municipalities'`` or unit layers as
    ``'units/<PV>'`` / ``layer='units', province='<PV>'`` (2-letter province code)."""
    name = layer.strip().strip("/")
    if name.endswith(".geojson"):
        name = name[: -len(".geojson")]
    if name.startswith("units/"):
        name, province = "units", name[len("units/") :]
    if name not in WEB_LAYERS:
        raise ValueError(f"unknown web layer {layer!r}; expected one of {WEB_LAYERS}")
    if name != "units":
        return _require(year, f"web/{name}.geojson")
    if not province:
        raise ValueError("unit layers are per province: pass province='NB' or layer='units/NB'")
    codes = load_geography_config().province_codes
    if province not in codes:  # also rejects path tricks such as 'units/../x'
        raise ValueError(f"unknown province {province!r}; expected one of {codes}")
    return _require(year, f"web/units/{province}.geojson")


def clear_cache() -> None:
    """Forget cached frames and manifests (called after a rebuild and by tests)."""
    _frame_cached.cache_clear()
    _manifest_cached.cache_clear()
