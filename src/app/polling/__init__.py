"""Polling: FICTIONAL pollsters, SIMULATED poll generation, aggregation and model blending.

Pure engines are re-exported here; the SQLAlchemy persistence layer lives in
:mod:`app.polling.service` and is intentionally *not* imported by this package.
"""

from app.polling.aggregate import PollAverage, aggregate_polls, averages_frame, generic_ballot_average
from app.polling.blend import (
    BlendResult,
    blend_polls_with_model,
    poll_environment_shift,
    shift_from_poll_average,
)
from app.polling.config import (
    AggregationConfig,
    GenerationConfig,
    PollingConfig,
    PollsterConfig,
    load_polling_config,
    resolve_pollsters,
)
from app.polling.generate import (
    GeneratedPoll,
    GeneratedPolls,
    competitiveness_from_shares,
    generate_polls,
    margin_of_error,
    polls_to_frames,
)
from app.polling.types import POLL_TYPES, poll_group_for_race, race_code_for

__all__ = [
    "POLL_TYPES",
    "AggregationConfig",
    "BlendResult",
    "GeneratedPoll",
    "GeneratedPolls",
    "GenerationConfig",
    "PollAverage",
    "PollingConfig",
    "PollsterConfig",
    "aggregate_polls",
    "averages_frame",
    "blend_polls_with_model",
    "competitiveness_from_shares",
    "generate_polls",
    "generic_ballot_average",
    "load_polling_config",
    "margin_of_error",
    "poll_environment_shift",
    "poll_group_for_race",
    "polls_to_frames",
    "race_code_for",
    "resolve_pollsters",
    "shift_from_poll_average",
]
