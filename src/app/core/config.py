"""YAML configuration loading with Pydantic validation.

Configuration documents live in ``config/`` (human-readable YAML).  Each subsystem owns a
Pydantic schema for its document and loads it through :func:`load_config`::

    from app.core.config import load_config
    cfg = load_config("districts.yaml", DistrictConfig)

Loaded documents are cached per (path, schema); call :func:`clear_config_cache` in tests.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from app.core.constitution import ConstitutionConfig
from app.core.errors import ConfigError
from app.core.settings import get_settings

M = TypeVar("M", bound=BaseModel)


def config_path(name: str | Path) -> Path:
    p = Path(name)
    if p.is_absolute() or p.exists():
        return p
    return get_settings().config_dir / p


def load_yaml(name: str | Path) -> Any:
    path = config_path(name)
    if not path.exists():
        raise ConfigError(f"Configuration file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        try:
            return yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc


def parse_config(data: Any, schema: type[M], source: str = "<memory>") -> M:
    try:
        return schema.model_validate(data)
    except PydanticValidationError as exc:
        raise ConfigError(f"Invalid configuration in {source}:\n{exc}") from exc


@lru_cache(maxsize=64)
def _load_cached(path_str: str, schema: type[BaseModel]) -> BaseModel:
    return parse_config(load_yaml(path_str), schema, source=path_str)


def load_config(name: str | Path, schema: type[M], *, optional: bool = False) -> M:
    """Load and validate ``config/<name>`` against ``schema``.

    With ``optional=True`` a missing file yields ``schema()`` defaults.
    """
    path = config_path(name)
    if optional and not path.exists():
        return schema()
    return _load_cached(str(path), schema)  # type: ignore[return-value]


def clear_config_cache() -> None:
    _load_cached.cache_clear()


def dump_yaml(data: Any) -> str:
    """Stable, human-readable YAML (used for scenario export)."""
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)


def get_constitution() -> ConstitutionConfig:
    """The active constitution (``config/constitution.yaml``; canonical defaults if absent)."""
    return load_config("constitution.yaml", ConstitutionConfig, optional=True)
