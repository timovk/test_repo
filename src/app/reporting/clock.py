"""Playback clock of the election night (pure time mapping, injectable ``now``).

The clock maps real (wall-clock) seconds to simulated seconds after the polls close::

    target_sim_time(now) = anchor_sim + (now − anchor_wall) × speed × base_rate

and simulated time to a reporting-event sequence number (``target_seq``).  It never touches
results: what is shown at a given ``seq`` is decided by the deterministic night engine, so
playback speed, pauses and steps change *when* content appears, never *what* appears.

States: ``ready`` → ``running`` ⇄ ``paused`` → ``finished``.  ``now`` is any monotonic number
of seconds (e.g. ``time.monotonic()`` or a POSIX timestamp) supplied by the caller, which makes
the clock trivially testable and persistable (``to_dict`` / ``from_dict``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import numpy as np

from app.core.errors import ElectionNightError
from app.reporting.config import NightConfig, load_night_config

if TYPE_CHECKING:
    from app.reporting.timeline import Timeline


class PlaybackState(StrEnum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"


@dataclass
class PlaybackClock:
    """Maps wall-clock time to simulated time / event ``seq`` (see module docstring)."""

    event_times: np.ndarray  # (N,) sim seconds of the reporting events (non-decreasing)
    speeds: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 25.0)
    base_rate: float = 60.0  # simulated seconds per real second at 1×
    speed: float = 1.0
    state: PlaybackState = PlaybackState.READY
    anchor_wall: float = 0.0
    anchor_sim: float = 0.0
    _times: np.ndarray = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._times = np.asarray(self.event_times, dtype=np.float64)
        if self._times.size and np.any(np.diff(self._times) < 0):
            raise ElectionNightError("event_times must be non-decreasing")
        self.speeds = tuple(sorted(float(s) for s in self.speeds))
        self._check_speed(self.speed)
        if self.base_rate <= 0:
            raise ElectionNightError("base_rate must be positive")

    @classmethod
    def from_timeline(
        cls, timeline: Timeline, config: NightConfig | None = None, speed: float | None = None
    ) -> PlaybackClock:
        """Clock over the events of a :class:`~app.reporting.timeline.Timeline`."""
        cfg = config or load_night_config()
        pb = cfg.playback
        return cls(
            event_times=timeline.sim_time_s,
            speeds=tuple(pb.speeds),
            base_rate=pb.base_rate,
            speed=float(speed if speed is not None else pb.default_speed),
        )

    # ------------------------------------------------------------------ queries
    @property
    def end_sim(self) -> float:
        """Simulated time of the last event (0 for an empty night)."""
        return float(self._times[-1]) if self._times.size else 0.0

    @property
    def n_events(self) -> int:
        return int(self._times.size)

    @property
    def rate(self) -> float:
        """Simulated seconds per real second at the current speed."""
        return self.speed * self.base_rate

    def target_sim_time(self, now: float) -> float:
        """Simulated time the display should show at wall-clock ``now``."""
        if self.state == PlaybackState.RUNNING:
            t = self.anchor_sim + max(float(now) - self.anchor_wall, 0.0) * self.rate
            return min(t, self.end_sim)
        if self.state == PlaybackState.FINISHED:
            return self.end_sim
        return self.anchor_sim

    def target_seq(self, now: float) -> int:
        """Number of events whose time is ≤ ``target_sim_time(now)``."""
        if self.state == PlaybackState.FINISHED:
            return self.n_events
        return int(np.searchsorted(self._times, self.target_sim_time(now), side="right"))

    def seconds_until_next_event(self, now: float) -> float | None:
        """Real seconds until the next event becomes due (None when paused/finished/none left)."""
        if self.state != PlaybackState.RUNNING:
            return None
        k = self.target_seq(now)
        if k >= self.n_events:
            return None
        return max((float(self._times[k]) - self.target_sim_time(now)) / self.rate, 0.0)

    # ------------------------------------------------------------------ transitions
    def _check_speed(self, x: float) -> float:
        x = float(x)
        if x not in self.speeds:
            raise ElectionNightError(f"speed {x} not in allowed speeds {list(self.speeds)}")
        return x

    def start(self, now: float, speed: float | None = None) -> None:
        """READY → RUNNING (a PAUSED clock resumes)."""
        if speed is not None:
            self.speed = self._check_speed(speed)
        if self.state == PlaybackState.PAUSED:
            self.resume(now)
            return
        if self.state != PlaybackState.READY:
            raise ElectionNightError(f"cannot start a clock that is {self.state.value}")
        self.anchor_wall = float(now)
        self.state = PlaybackState.RUNNING

    def pause(self, now: float) -> None:
        """RUNNING → PAUSED, freezing the simulated time reached at ``now``."""
        if self.state != PlaybackState.RUNNING:
            raise ElectionNightError(f"cannot pause a clock that is {self.state.value}")
        self.anchor_sim = self.target_sim_time(now)
        self.anchor_wall = float(now)
        self.state = PlaybackState.PAUSED

    def resume(self, now: float) -> None:
        """PAUSED → RUNNING from the frozen simulated time."""
        if self.state != PlaybackState.PAUSED:
            raise ElectionNightError(f"cannot resume a clock that is {self.state.value}")
        self.anchor_wall = float(now)
        self.state = PlaybackState.RUNNING

    def set_speed(self, now: float, speed: float) -> None:
        """Change speed without a jump in simulated time (re-anchors at ``now``)."""
        speed = self._check_speed(speed)
        if self.state == PlaybackState.RUNNING:
            self.anchor_sim = self.target_sim_time(now)
            self.anchor_wall = float(now)
        self.speed = speed

    def step(self, now: float | None = None) -> int:
        """Advance exactly one event and pause there; returns the new target ``seq``.

        A running clock is paused first (at ``now``, default: its anchor)."""
        if self.state == PlaybackState.FINISHED:
            return self.n_events
        if self.state == PlaybackState.RUNNING:
            self.pause(self.anchor_wall if now is None else now)
        k = int(np.searchsorted(self._times, self.anchor_sim, side="right"))
        if k >= self.n_events:
            self.finish()
            return self.n_events
        self.anchor_sim = float(self._times[k])
        if now is not None:
            self.anchor_wall = float(now)
        self.state = PlaybackState.PAUSED
        seq = int(np.searchsorted(self._times, self.anchor_sim, side="right"))
        if seq >= self.n_events:
            self.state = PlaybackState.FINISHED
        return seq

    def seek(self, sim_time_s: float, now: float | None = None) -> None:
        """Jump to a simulated time (keeps RUNNING/PAUSED; READY becomes PAUSED)."""
        t = min(max(float(sim_time_s), 0.0), self.end_sim)
        self.anchor_sim = t
        if now is not None:
            self.anchor_wall = float(now)
        if self.state in (PlaybackState.READY, PlaybackState.FINISHED):
            self.state = PlaybackState.PAUSED

    def finish(self) -> None:
        """Jump to the end of the night."""
        self.anchor_sim = self.end_sim
        self.state = PlaybackState.FINISHED

    def tick(self, now: float) -> int:
        """Target ``seq`` at ``now``; a running clock that reached the end becomes FINISHED."""
        seq = self.target_seq(now)
        if self.state == PlaybackState.RUNNING and self.target_sim_time(now) >= self.end_sim:
            self.finish()
            return self.n_events
        return seq

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> dict:
        """JSON-serialisable clock state (event times excluded)."""
        return {
            "state": self.state.value,
            "speed": self.speed,
            "speeds": list(self.speeds),
            "base_rate": self.base_rate,
            "anchor_wall": self.anchor_wall,
            "anchor_sim": self.anchor_sim,
        }

    @classmethod
    def from_dict(cls, data: dict, event_times: np.ndarray) -> PlaybackClock:
        """Restore a clock saved with :meth:`to_dict`."""
        return cls(
            event_times=event_times,
            speeds=tuple(data.get("speeds", (1.0, 2.0, 5.0, 10.0, 25.0))),
            base_rate=float(data.get("base_rate", 60.0)),
            speed=float(data.get("speed", 1.0)),
            state=PlaybackState(data.get("state", "ready")),
            anchor_wall=float(data.get("anchor_wall", 0.0)),
            anchor_sim=float(data.get("anchor_sim", 0.0)),
        )
