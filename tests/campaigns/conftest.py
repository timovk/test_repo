"""Fixtures for campaign tests (FICTIONAL campaigns)."""

from __future__ import annotations

import pytest

from app.campaigns.config import CampaignConfig, load_campaign_config
from app.campaigns.engine import build_targets
from app.campaigns.types import Target

PROVINCES = ["GR", "FR", "DR", "OV", "FL", "GE", "UT", "NH", "ZH", "ZE", "NB", "LI"]
EV = [6, 6, 5, 10, 5, 20, 13, 26, 33, 4, 22, 11]
POP = [590e3, 660e3, 500e3, 1.18e6, 450e3, 2.15e6, 1.4e6, 2.95e6, 3.9e6, 390e3, 2.65e6, 1.12e6]
MARGIN = [-10.0, 15.0, 20.0, 2.0, -1.0, 0.5, -3.0, -12.0, 1.5, 25.0, 4.0, -6.0]


@pytest.fixture(scope="session")
def campaign_config() -> CampaignConfig:
    return load_campaign_config()


@pytest.fixture()
def province_targets(campaign_config: CampaignConfig) -> list[Target]:
    """Presidential targets: provinces weighted by (illustrative) electoral votes."""
    return build_targets("province", PROVINCES, EV, MARGIN, POP, config=campaign_config)
