"""Election night (SIMULATED): reporting timeline, probabilistic race calling, live engine, playback.

* :mod:`app.reporting.config` — ``config/night.yaml`` schema (:class:`NightConfig`).
* :mod:`app.reporting.timeline` — :func:`generate_timeline` → :class:`Timeline` of reporting events.
* :mod:`app.reporting.calling` — :class:`RaceCaller` / :class:`RaceProgress` / :class:`CallDecision`.
* :mod:`app.reporting.live` — :class:`NightEngine` (tallies, calls, snapshots).
* :mod:`app.reporting.clock` — :class:`PlaybackClock` (wall clock → simulated time → ``seq``).

Engines are pure (NumPy in, dataclasses out); persistence lives in services.  See
``docs/RACE_CALLING.md``.
"""

from app.reporting.calling import (
    CallDecision,
    CallState,
    RaceCaller,
    RaceProgress,
    RecountCheck,
    RecountInput,
    allocate_counted,
)
from app.reporting.clock import PlaybackClock, PlaybackState
from app.reporting.config import NightConfig, default_night_config, load_night_config
from app.reporting.live import (
    MANUAL_STATUSES,
    CallRecord,
    LeadChange,
    ManualCall,
    NightEngine,
    RaceMeta,
    manual_calls_from_history,
)
from app.reporting.timeline import ReportingEvent, Timeline, generate_timeline, polls_close_times

__all__ = [
    "MANUAL_STATUSES",
    "CallDecision",
    "CallRecord",
    "CallState",
    "LeadChange",
    "ManualCall",
    "NightConfig",
    "NightEngine",
    "PlaybackClock",
    "PlaybackState",
    "RaceCaller",
    "RaceMeta",
    "RaceProgress",
    "RecountCheck",
    "RecountInput",
    "ReportingEvent",
    "Timeline",
    "allocate_counted",
    "default_night_config",
    "generate_timeline",
    "load_night_config",
    "manual_calls_from_history",
    "polls_close_times",
]
