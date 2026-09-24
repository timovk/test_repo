"""Deterministic CSV / JSON writers for the export schemas.

* :func:`write_csv` — UTF-8, ``\\n`` line endings, header = schema column order, empty field =
  null, booleans ``true``/``false``, dates ``YYYY-MM-DD``, datetimes ISO 8601, floats rounded to
  the column's decimals and printed in shortest round-trip form (``-0.0`` normalised to ``0.0``).
* :func:`write_json` — an envelope::

      {"schema": name, "schema_version": n, "data_category": "SIMULATED" | "REAL" | …,
       "generated_at": ISO-8601, "metadata": {...}, "columns": [descriptor, …], "rows": [[…], …]}

  rows are arrays aligned with ``columns``; nulls (and non-finite floats) are ``null``.
* :func:`export_bundle` — several datasets in several formats plus ``manifest.json`` (file list,
  schema versions and fingerprints, row counts, SHA-256).

Rows are always conformed to the schema, validated, and sorted by the schema key, so the same
data produce byte-identical CSV files (and identical JSON apart from ``generated_at``, which can
be pinned).  :func:`read_csv` / :func:`read_json` read the files back into conformed frames.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.core.logging import get_logger
from app.export.schemas import (
    DEFAULT_DECIMALS,
    Column,
    ColumnType,
    ExportSchema,
    SchemaError,
    assert_valid,
    conform,
    get_schema,
    sort_rows,
)

log = get_logger(__name__)

#: Version of the bundle manifest layout.
BUNDLE_VERSION: int = 1
FORMATS: tuple[str, ...] = ("csv", "json")
MANIFEST_NAME = "manifest.json"


# =========================================================================== preparation
def prepare(df: pd.DataFrame, schema: str | ExportSchema, *, allow_unknown: bool = False) -> pd.DataFrame:
    """Conform, validate (raises :class:`SchemaError`) and sort ``df`` for export."""
    sch = get_schema(schema)
    out = conform(df, sch, allow_unknown=allow_unknown)
    assert_valid(out, sch)
    return sort_rows(out, sch)


def _float_values(series: pd.Series, col: Column) -> list[float | None]:
    d = col.decimals if col.decimals is not None else DEFAULT_DECIMALS
    vals = series.to_numpy(dtype=float)
    rounded = np.round(vals, d) + 0.0  # + 0.0 turns -0.0 into 0.0
    return [float(v) if math.isfinite(v) else None for v in rounded]


def _python_values(series: pd.Series, col: Column) -> list[Any]:
    """JSON-ready Python scalars for one conformed column (None for null)."""
    t = col.type
    if t is ColumnType.FLOAT:
        return _float_values(series, col)
    na = series.isna().to_numpy()
    if t is ColumnType.INT:
        raw = series.to_numpy(dtype=object, na_value=None)
        return [None if n else int(v) for v, n in zip(raw, na, strict=True)]
    if t is ColumnType.BOOL:
        raw = series.to_numpy(dtype=object, na_value=None)
        return [None if n else bool(v) for v, n in zip(raw, na, strict=True)]
    if t is ColumnType.STR:
        raw = series.to_numpy(dtype=object, na_value=None)
        return [None if n else str(v) for v, n in zip(raw, na, strict=True)]
    if t is ColumnType.DATE:
        return [None if n else ts.strftime("%Y-%m-%d") for ts, n in zip(series, na, strict=True)]
    return [None if n else ts.isoformat() for ts, n in zip(series, na, strict=True)]


def _columns_as_python(df: pd.DataFrame, sch: ExportSchema) -> list[list[Any]]:
    return [_python_values(df[c.name], c) for c in sch.columns]


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


# =========================================================================== CSV
def to_csv_text(df: pd.DataFrame, schema: str | ExportSchema, *, allow_unknown: bool = False) -> str:
    """CSV text of ``df`` under ``schema`` (see module docstring for the format)."""
    sch = get_schema(schema)
    out = prepare(df, sch, allow_unknown=allow_unknown)
    cols = _columns_as_python(out, sch)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(sch.names)
    for row in zip(*cols, strict=True) if cols else ():
        writer.writerow([_csv_cell(v) for v in row])
    return buf.getvalue()


def write_csv(
    df: pd.DataFrame, schema: str | ExportSchema, path: str | Path, *, allow_unknown: bool = False
) -> Path:
    """Write ``df`` as deterministic CSV under ``schema``; returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = to_csv_text(df, schema, allow_unknown=allow_unknown)
    p.write_text(text, encoding="utf-8", newline="")
    log.debug("wrote %s (%s)", p, get_schema(schema).name)
    return p


def read_csv(path: str | Path, schema: str | ExportSchema) -> pd.DataFrame:
    """Read a CSV written by :func:`write_csv` back into a conformed frame."""
    sch = get_schema(schema)
    raw = pd.read_csv(Path(path), dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8")
    if tuple(raw.columns) != sch.names:
        raise SchemaError(f"{sch.name}: CSV header {list(raw.columns)} differs from the schema")
    return conform(raw, sch)


# =========================================================================== JSON
def _jsonable(value: Any) -> Any:
    """Convert metadata values to JSON-ready structures (sorted mapping keys)."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, int | np.integer):
        return int(value)
    if isinstance(value, float | np.floating):
        f = float(value)
        return f if math.isfinite(f) else None
    if isinstance(value, datetime | pd.Timestamp):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(value[k]) for k in sorted(value, key=str)}
    if isinstance(value, set | frozenset):
        return [_jsonable(v) for v in sorted(value, key=str)]
    if isinstance(value, Sequence | np.ndarray):
        return [_jsonable(v) for v in value]
    raise TypeError(f"metadata value of type {type(value).__name__} is not JSON-serialisable")


def _timestamp(generated_at: datetime | str | None) -> str:
    if generated_at is None:
        return datetime.now(UTC).replace(microsecond=0).isoformat()
    if isinstance(generated_at, str):
        return generated_at
    return generated_at.isoformat()


def to_json_envelope(
    df: pd.DataFrame,
    schema: str | ExportSchema,
    metadata: Mapping[str, Any] | None = None,
    *,
    generated_at: datetime | str | None = None,
    allow_unknown: bool = False,
) -> dict[str, Any]:
    """The JSON envelope as a dict (for API responses and :func:`write_json`)."""
    sch = get_schema(schema)
    out = prepare(df, sch, allow_unknown=allow_unknown)
    cols = _columns_as_python(out, sch)
    rows = [list(r) for r in zip(*cols, strict=True)] if cols else []
    return {
        "schema": sch.name,
        "schema_version": sch.version,
        "data_category": sch.data_category.value,
        "generated_at": _timestamp(generated_at),
        "metadata": _jsonable(dict(metadata or {})),
        "columns": [c.to_dict(sch.data_category) for c in sch.columns],
        "rows": rows,
    }


def dumps_envelope(envelope: Mapping[str, Any], *, indent: int | None = None) -> str:
    """Serialise an envelope deterministically (no NaN; compact unless ``indent``)."""
    separators = (",", ":") if indent is None else (",", ": ")
    return (
        json.dumps(envelope, ensure_ascii=False, allow_nan=False, indent=indent, separators=separators) + "\n"
    )


def write_json(
    df: pd.DataFrame,
    schema: str | ExportSchema,
    path: str | Path,
    metadata: Mapping[str, Any] | None = None,
    *,
    generated_at: datetime | str | None = None,
    allow_unknown: bool = False,
    indent: int | None = None,
) -> Path:
    """Write ``df`` as a JSON envelope under ``schema``; returns the path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    env = to_json_envelope(df, schema, metadata, generated_at=generated_at, allow_unknown=allow_unknown)
    p.write_text(dumps_envelope(env, indent=indent), encoding="utf-8", newline="")
    log.debug("wrote %s (%s, %d rows)", p, env["schema"], len(env["rows"]))
    return p


def read_json(path: str | Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Read a JSON envelope back: (conformed frame, envelope without ``rows``)."""
    env = json.loads(Path(path).read_text(encoding="utf-8"))
    sch = get_schema(env["schema"])
    if int(env.get("schema_version", -1)) != sch.version:
        raise SchemaError(
            f"{sch.name}: file has schema_version {env.get('schema_version')}, registry has {sch.version}"
        )
    names = [c["name"] for c in env["columns"]]
    if tuple(names) != sch.names:
        raise SchemaError(f"{sch.name}: JSON columns differ from the schema")
    frame = pd.DataFrame(env["rows"], columns=names) if env["rows"] else pd.DataFrame({n: [] for n in names})
    head = {k: v for k, v in env.items() if k != "rows"}
    return conform(frame, sch), head


# =========================================================================== bundle
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def export_bundle(
    frames: Mapping[str, pd.DataFrame],
    out_dir: str | Path,
    formats: Iterable[str] = FORMATS,
    metadata: Mapping[str, Any] | None = None,
    *,
    generated_at: datetime | str | None = None,
    allow_unknown: bool = False,
    manifest: bool = True,
) -> list[Path]:
    """Write every ``frames[schema_name]`` in every format to ``out_dir`` (``<schema>.<fmt>``).

    Keys may be canonical schema names or aliases.  A ``manifest.json`` lists each file with its
    schema, version, fingerprint, data category, row count and SHA-256.  Returns the written
    paths (datasets in schema-name order, then the manifest).  One ``generated_at`` is used for
    every file of the bundle.
    """
    fmts = list(dict.fromkeys(formats))
    bad = [f for f in fmts if f not in FORMATS]
    if bad or not fmts:
        raise ValueError(f"unsupported formats {bad or fmts}; expected a subset of {FORMATS}")
    resolved: dict[str, tuple[ExportSchema, pd.DataFrame]] = {}
    for key, df in frames.items():
        sch = get_schema(key)
        if sch.name in resolved:
            raise ValueError(f"dataset {sch.name!r} given twice (via {key!r})")
        resolved[sch.name] = (sch, df)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = _timestamp(generated_at)
    meta = _jsonable(dict(metadata or {}))
    paths: list[Path] = []
    entries: list[dict[str, Any]] = []
    for name in sorted(resolved):
        sch, df = resolved[name]
        prepared = prepare(df, sch, allow_unknown=allow_unknown)
        for fmt in fmts:
            path = out / f"{sch.name}.{fmt}"
            if fmt == "csv":
                write_csv(prepared, sch, path)
            else:
                write_json(prepared, sch, path, meta, generated_at=stamp)
            paths.append(path)
            entries.append(
                {
                    "file": path.name,
                    "schema": sch.name,
                    "schema_version": sch.version,
                    "fingerprint": sch.fingerprint(),
                    "format": fmt,
                    "data_category": sch.data_category.value,
                    "rows": len(prepared),
                    "sha256": _sha256(path),
                }
            )
    if manifest:
        doc = {
            "bundle_version": BUNDLE_VERSION,
            "generated_at": stamp,
            "metadata": meta,
            "data_categories": sorted({e["data_category"] for e in entries}),
            "files": entries,
        }
        mpath = out / MANIFEST_NAME
        mpath.write_text(
            json.dumps(doc, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
            encoding="utf-8",
            newline="",
        )
        paths.append(mpath)
    log.info("exported %d datasets to %s", len(resolved), out)
    return paths
