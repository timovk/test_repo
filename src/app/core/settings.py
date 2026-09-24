"""Runtime settings (paths, database URL, server options).

Values can be overridden through environment variables prefixed ``NLFED_``
(e.g. ``NLFED_DATABASE_URL=postgresql+psycopg://...``) or a ``.env`` file.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Repository root (…/src/app/core/settings.py → parents[3]).
PROJECT_ROOT: Path = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NLFED_", env_file=".env", extra="ignore")

    project_root: Path = PROJECT_ROOT
    config_dir: Path = Field(default=PROJECT_ROOT / "config")
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    database_url: str | None = None  # default: sqlite:///<data_dir>/nlfed.db
    #: Geography vintage (CBS "Wijk- en Buurtkaart" year) used by default.
    geography_year: int = 2025
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    log_json: bool = False
    #: Worker processes for Monte Carlo forecasting (0 = auto, 1 = in-process).
    forecast_workers: int = 0
    #: Allow network downloads (set to False to guarantee offline operation).
    allow_network: bool = True
    http_timeout_s: float = 600.0

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'nlfed.db').as_posix()}"

    def processed_geo_dir(self, year: int | None = None) -> Path:
        return self.processed_dir / f"geo_{year or self.geography_year}"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.raw_dir, self.processed_dir, self.cache_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """For tests that patch environment variables."""
    get_settings.cache_clear()
