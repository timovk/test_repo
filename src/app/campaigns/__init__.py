"""Campaigns: FICTIONAL campaign planning and modest, uncertain (SIMULATED) campaign effects."""

from app.campaigns.config import (
    ACTIONS,
    LEVELS,
    ActionProfile,
    CampaignConfig,
    StrategyConfig,
    config_from_spec,
    load_campaign_config,
)
from app.campaigns.engine import (
    CampaignRun,
    build_targets,
    combine_effects,
    competitiveness_from_margin,
    plan_campaign,
    plan_summary,
    realize_effects,
    run_campaigns,
)
from app.campaigns.types import (
    Allocation,
    AllocationEffect,
    CampaignEffects,
    CombinedCampaignEffects,
    Target,
)

__all__ = [
    "ACTIONS",
    "LEVELS",
    "ActionProfile",
    "Allocation",
    "AllocationEffect",
    "CampaignConfig",
    "CampaignEffects",
    "CampaignRun",
    "CombinedCampaignEffects",
    "StrategyConfig",
    "Target",
    "build_targets",
    "combine_effects",
    "competitiveness_from_margin",
    "config_from_spec",
    "load_campaign_config",
    "plan_campaign",
    "plan_summary",
    "realize_effects",
    "run_campaigns",
]
