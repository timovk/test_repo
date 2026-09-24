"""Election-night reporting timeline (SIMULATED).

:func:`generate_timeline` turns the final ballots per unit (CBS buurt ≙ precinct) into a
deterministic sequence of *reporting events*: batches of counted ballots arriving from
municipalities after the polls close.  The model (see ``docs/RACE_CALLING.md`` §1):

* every municipality gets a **first-report time** that grows with its size (log ballots) and
  urbanity, plus a province effect (fixed + seeded) and multiplicative seeded jitter — small
  rural municipalities report around 21:30–22:30, the big cities after 23:00;
* a **counting duration** that grows sub-linearly with size (and urbanity), so the last big
  cities finish around 03:00–05:00 (soft cap ``latest_end_minutes``);
* a **batch count** drawn from size tiers (``batches_by_size``); batch sizes are Dirichlet
  distributed and batch cuts snap to **wijk** (neighbourhood cluster) boundaries, so a batch is
  usually a set of whole wijken;
* **partial precincts**: a big buurt crossing a batch cut is split across batches;
* units with zero ballots are still "reported" (fraction 1.0) in the batch covering them.

Fractions are stored per event and unit both as the *increment* and as the unit's
*cumulative* fraction after the event.  Cumulative fractions are quantised to multiples of
``2**-20`` so increments are exact binary fractions: summing the increments (e.g. after a
database round trip) reproduces the cumulative values bit-for-bit, and every unit reaches
**exactly 1.0** at its last batch.

The timeline is a pure function of ``(frame, ballots_per_unit, config, seed, units_mask,
election_date, unit_wijk)``; each municipality draws from its own RNG stream
``make_rng(seed, "timeline", "muni", <code>)``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np

from app.core.errors import ElectionNightError
from app.core.logging import get_logger, log_ctx
from app.core.rng import make_rng
from app.geography.frame import GeographyFrame
from app.reporting.config import NightConfig, ReportingSpeedConfig, load_night_config, parse_hhmm

log = get_logger(__name__)

#: Cumulative fractions are multiples of this (exact binary fractions).
FRACTION_QUANTUM: float = 2.0**-20
_BUURT_CODE = re.compile(r"^BU\d{8}$")


# --------------------------------------------------------------------------- public types
@dataclass(frozen=True)
class ReportingEvent:
    """One batch of counted ballots (maps onto ``reporting_event`` + ``reporting_event_unit``)."""

    seq: int  # 1-based position in the night
    sim_time_s: float  # seconds after the earliest poll closing
    timestamp: datetime  # simulated local wall-clock time (tz-aware)
    municipality: int  # index into frame municipalities
    province: int  # index into frame provinces
    units: np.ndarray  # (k,) unit indices completed (partly) in this batch
    increments: np.ndarray  # (k,) fraction of each unit's ballots counted in this batch
    cumulative: np.ndarray  # (k,) cumulative fraction of each unit after this batch
    ballots: int  # ballots counted in this batch
    municipality_fraction_after: float  # share of the municipality's ballots counted after it
    batch_index: int  # 1-based batch number within the municipality
    batches_in_municipality: int
    kind: str  # 'batch' | 'final' (the municipality's last batch)


@dataclass
class Timeline:
    """All reporting events of one election night, in CSR layout for fast replay."""

    seed: int
    reference_close: datetime  # tz-aware local datetime of sim_time_s = 0 (earliest closing)
    timezone: str
    n_units: int
    n_munis: int
    sim_time_s: np.ndarray  # (N,) float64, non-decreasing
    muni: np.ndarray  # (N,) int64
    province: np.ndarray  # (N,) int64
    ballots: np.ndarray  # (N,) int64
    muni_fraction_after: np.ndarray  # (N,) float64
    batch_index: np.ndarray  # (N,) int64 (1-based)
    batches_in_muni: np.ndarray  # (N,) int64
    unit_ptr: np.ndarray  # (N+1,) int64 CSR pointer into units/increments/cumulative
    units: np.ndarray  # (K,) int64
    increments: np.ndarray  # (K,) float64
    cumulative: np.ndarray  # (K,) float64
    muni_close_offset_s: np.ndarray  # (M,) poll closing of each municipality relative to sim 0
    units_mask: np.ndarray  # (U,) bool — units covered by the timeline
    config_fingerprint: str = ""
    checkpoint_every: int = 500
    _checkpoints: list[np.ndarray] = field(default_factory=list, repr=False)

    # ------------------------------------------------------------------ basics
    @property
    def n_events(self) -> int:
        return len(self.sim_time_s)

    @property
    def seq(self) -> np.ndarray:
        """(N,) sequence numbers 1..N."""
        return np.arange(1, self.n_events + 1, dtype=np.int64)

    @property
    def end_time_s(self) -> float:
        return float(self.sim_time_s[-1]) if self.n_events else 0.0

    def _check_seq(self, seq: int) -> int:
        if not 1 <= seq <= self.n_events:
            raise ElectionNightError(f"seq {seq} out of range 1..{self.n_events}")
        return seq - 1

    def event(self, seq: int) -> ReportingEvent:
        """The event with sequence number ``seq`` (1-based)."""
        i = self._check_seq(seq)
        a, b = int(self.unit_ptr[i]), int(self.unit_ptr[i + 1])
        return ReportingEvent(
            seq=seq,
            sim_time_s=float(self.sim_time_s[i]),
            timestamp=self.local_datetime(float(self.sim_time_s[i])),
            municipality=int(self.muni[i]),
            province=int(self.province[i]),
            units=self.units[a:b],
            increments=self.increments[a:b],
            cumulative=self.cumulative[a:b],
            ballots=int(self.ballots[i]),
            municipality_fraction_after=float(self.muni_fraction_after[i]),
            batch_index=int(self.batch_index[i]),
            batches_in_municipality=int(self.batches_in_muni[i]),
            kind="final" if self.batch_index[i] == self.batches_in_muni[i] else "batch",
        )

    def events(self) -> Iterator[ReportingEvent]:
        for s in range(1, self.n_events + 1):
            yield self.event(s)

    def event_units(self, seq: int) -> tuple[np.ndarray, np.ndarray]:
        """``(units, cumulative)`` of event ``seq`` — the hot path of the night engine."""
        i = self._check_seq(seq)
        a, b = int(self.unit_ptr[i]), int(self.unit_ptr[i + 1])
        return self.units[a:b], self.cumulative[a:b]

    # ------------------------------------------------------------------ clock
    def local_datetime(self, sim_time_s: float) -> datetime:
        """Local wall-clock time (tz-aware, DST-correct) of a simulation time."""
        tz = ZoneInfo(self.timezone)
        utc = self.reference_close.astimezone(UTC) + timedelta(seconds=float(sim_time_s))
        return utc.astimezone(tz)

    def local_clock(self, sim_time_s: float) -> str:
        """``'HH:MM'`` local clock string of a simulation time."""
        return self.local_datetime(sim_time_s).strftime("%H:%M")

    def seq_at_time(self, sim_time_s: float) -> int:
        """Number of events with ``sim_time_s ≤ t`` (i.e. the seq reached at time ``t``)."""
        return int(np.searchsorted(self.sim_time_s, float(sim_time_s), side="right"))

    # ------------------------------------------------------------------ replay
    def _build_checkpoints(self) -> None:
        f = np.zeros(self.n_units, dtype=np.float64)
        cps = [f.copy()]
        every = self.checkpoint_every
        for c in range(1, self.n_events // every + 1):
            a, b = int(self.unit_ptr[(c - 1) * every]), int(self.unit_ptr[c * every])
            np.maximum.at(f, self.units[a:b], self.cumulative[a:b])
            cps.append(f.copy())
        self._checkpoints = cps

    def fraction_after(self, seq: int) -> np.ndarray:
        """(U,) cumulative reported fraction of every unit after event ``seq`` (0 = none)."""
        if not 0 <= seq <= self.n_events:
            raise ElectionNightError(f"seq {seq} out of range 0..{self.n_events}")
        if not self._checkpoints:
            self._build_checkpoints()
        c = min(seq // self.checkpoint_every, len(self._checkpoints) - 1)
        f = self._checkpoints[c].copy()
        a, b = int(self.unit_ptr[c * self.checkpoint_every]), int(self.unit_ptr[seq])
        if b > a:
            np.maximum.at(f, self.units[a:b], self.cumulative[a:b])
        return f

    def unit_last_seq(self) -> np.ndarray:
        """(U,) seq at which each unit is fully reported (0 for units outside the timeline)."""
        out = np.zeros(self.n_units, dtype=np.int64)
        ev = np.repeat(np.arange(1, self.n_events + 1), np.diff(self.unit_ptr))
        done = self.cumulative == 1.0
        out[self.units[done]] = ev[done]
        return out

    def unit_first_seq(self) -> np.ndarray:
        """(U,) seq at which each unit first reports (0 for units outside the timeline)."""
        out = np.full(self.n_units, np.iinfo(np.int64).max, dtype=np.int64)
        ev = np.repeat(np.arange(1, self.n_events + 1), np.diff(self.unit_ptr))
        np.minimum.at(out, self.units, ev)
        out[out == np.iinfo(np.int64).max] = 0
        return out

    def muni_first_time_s(self) -> np.ndarray:
        """(M,) time of each municipality's first batch (NaN when it has none)."""
        out = np.full(self.n_munis, np.inf)
        np.minimum.at(out, self.muni, self.sim_time_s)
        out[np.isinf(out)] = np.nan
        return out

    def muni_last_time_s(self) -> np.ndarray:
        """(M,) time of each municipality's last batch (NaN when it has none)."""
        out = np.full(self.n_munis, -np.inf)
        np.maximum.at(out, self.muni, self.sim_time_s)
        out[np.isinf(out)] = np.nan
        return out

    # ------------------------------------------------------------------ integrity
    def validate(self) -> list[str]:
        """Structural invariants; returns a list of problems (empty = OK)."""
        problems: list[str] = []
        N = self.n_events
        if len(self.unit_ptr) != N + 1 or self.unit_ptr[0] != 0 or self.unit_ptr[-1] != len(self.units):
            problems.append("unit_ptr is inconsistent")
            return problems
        if N and np.any(np.diff(self.sim_time_s) < 0):
            problems.append("events are not sorted by time")
        if np.any(np.diff(self.unit_ptr) <= 0):
            problems.append("empty events present")
        ev = np.repeat(np.arange(N), np.diff(self.unit_ptr))
        order = np.lexsort((ev, self.units))
        u, cum = self.units[order], self.cumulative[order]
        same = u[1:] == u[:-1]
        if np.any(same & (cum[1:] <= cum[:-1])):
            problems.append("cumulative fractions not strictly increasing per unit")
        last = np.r_[~same, True]
        if np.any(cum[last] != 1.0):
            problems.append("some units do not reach exactly 1.0")
        covered = np.zeros(self.n_units, dtype=bool)
        covered[self.units] = True
        if not np.array_equal(covered, self.units_mask):
            problems.append("timeline units differ from the units mask")
        inc_sum = np.bincount(self.units, weights=self.increments, minlength=self.n_units)
        if np.any(inc_sum[self.units_mask] != 1.0):
            problems.append("increments do not sum to exactly 1.0 per unit")
        if np.any((self.cumulative <= 0) | (self.cumulative > 1.0)):
            problems.append("cumulative fractions outside (0, 1]")
        return problems

    # ------------------------------------------------------------------ persistence
    def to_event_rows(self, frame: GeographyFrame) -> list[dict]:
        """Rows for ``reporting_event`` (codes instead of DB ids; services map them).

        ``timestamp`` is the tz-aware local time; the naive ``reporting_event.timestamp`` column
        stores its local wall-clock value (``.replace(tzinfo=None)``)."""
        rows = []
        for i in range(self.n_events):
            m = int(self.muni[i])
            k, n = int(self.batch_index[i]), int(self.batches_in_muni[i])
            rows.append(
                {
                    "seq": i + 1,
                    "sim_time_s": float(self.sim_time_s[i]),
                    "timestamp": self.local_datetime(float(self.sim_time_s[i])),
                    "municipality_code": frame.muni_codes[m],
                    "province_code": frame.province_codes[int(self.province[i])],
                    "kind": "final" if k == n else "batch",
                    "ballots_in_batch": int(self.ballots[i]),
                    "municipality_fraction_after": float(self.muni_fraction_after[i]),
                    "description": f"{frame.muni_names[m]}: batch {k}/{n}",
                }
            )
        return rows

    def to_unit_rows(self, frame: GeographyFrame) -> list[tuple[int, str, float]]:
        """Rows ``(seq, unit_code, fraction_increment)`` for ``reporting_event_unit``."""
        ev = np.repeat(np.arange(1, self.n_events + 1), np.diff(self.unit_ptr))
        codes = frame.unit_codes
        return [
            (int(s), codes[int(u)], float(x)) for s, u, x in zip(ev, self.units, self.increments, strict=True)
        ]

    @classmethod
    def from_records(
        cls,
        frame: GeographyFrame,
        events: Sequence[Mapping],
        unit_rows: Sequence[tuple[int, str, float]],
        *,
        reference_close: datetime,
        timezone: str = "Europe/Amsterdam",
        seed: int = 0,
        checkpoint_every: int = 500,
        config_fingerprint: str = "",
    ) -> Timeline:
        """Rebuild a timeline from persisted rows (inverse of :meth:`to_event_rows` /
        :meth:`to_unit_rows`).  Exact: increments are binary fractions, so cumulative values
        are reproduced bit-for-bit."""
        ev_sorted = sorted(events, key=lambda r: int(r["seq"]))
        N = len(ev_sorted)
        seqs = [int(r["seq"]) for r in ev_sorted]
        if seqs != list(range(1, N + 1)):
            raise ElectionNightError("event rows must have seq 1..N")
        muni = np.array([frame.muni_index(str(r["municipality_code"])) for r in ev_sorted], dtype=np.int64)
        seq_arr = np.array([int(r[0]) for r in unit_rows], dtype=np.int64)
        unit_arr = np.array([frame.unit_index(str(r[1])) for r in unit_rows], dtype=np.int64)
        inc_arr = np.array([float(r[2]) for r in unit_rows], dtype=np.float64)
        order = np.lexsort((unit_arr, seq_arr))
        seq_arr, unit_arr, inc_arr = seq_arr[order], unit_arr[order], inc_arr[order]
        cum = _cumulative_from_increments(unit_arr, seq_arr, inc_arr)
        counts = np.bincount(seq_arr - 1, minlength=N)
        ptr = np.r_[0, np.cumsum(counts)].astype(np.int64)
        mask = np.zeros(frame.n_units, dtype=bool)
        mask[unit_arr] = True
        # batch numbering within municipalities follows event order
        batch_index = np.zeros(N, dtype=np.int64)
        batches = np.zeros(N, dtype=np.int64)
        per_muni: dict[int, list[int]] = {}
        for i, m in enumerate(muni.tolist()):
            per_muni.setdefault(m, []).append(i)
        for idx in per_muni.values():
            batch_index[idx] = np.arange(1, len(idx) + 1)
            batches[idx] = len(idx)
        tz = ZoneInfo(timezone)
        ref = reference_close if reference_close.tzinfo else reference_close.replace(tzinfo=tz)
        tl = cls(
            seed=seed,
            reference_close=ref.astimezone(tz),
            timezone=timezone,
            n_units=frame.n_units,
            n_munis=frame.n_munis,
            sim_time_s=np.array([float(r["sim_time_s"]) for r in ev_sorted], dtype=np.float64),
            muni=muni,
            province=frame.muni_province[muni].astype(np.int64),
            ballots=np.array([int(r.get("ballots_in_batch", 0)) for r in ev_sorted], dtype=np.int64),
            muni_fraction_after=np.array(
                [float(r.get("municipality_fraction_after", 0.0)) for r in ev_sorted], dtype=np.float64
            ),
            batch_index=batch_index,
            batches_in_muni=batches,
            unit_ptr=ptr,
            units=unit_arr,
            increments=inc_arr,
            cumulative=cum,
            muni_close_offset_s=np.zeros(frame.n_munis),
            units_mask=mask,
            config_fingerprint=config_fingerprint,
            checkpoint_every=checkpoint_every,
        )
        return tl

    # ------------------------------------------------------------------ reporting statistics
    def summary(self, frame: GeographyFrame | None = None) -> dict:
        """JSON-serialisable statistics of the night (first/last reports, batch counts …)."""
        first = self.muni_first_time_s()
        last = self.muni_last_time_s()
        active = ~np.isnan(first)
        n_batches = np.bincount(self.muni, minlength=self.n_munis)[active]
        partial = int(np.sum(np.bincount(self.units, minlength=self.n_units) > 1))
        out = {
            "data_category": "SIMULATED",
            "seed": self.seed,
            "events": self.n_events,
            "units": int(self.units_mask.sum()),
            "partial_units": partial,
            "municipalities": int(active.sum()),
            "reference_close": self.reference_close.isoformat(),
            "first_report_s": float(np.nanmin(first)) if active.any() else None,
            "last_report_s": self.end_time_s,
            "first_report_clock": self.local_clock(float(np.nanmin(first))) if active.any() else None,
            "last_report_clock": self.local_clock(self.end_time_s),
            "median_first_report_s": float(np.nanmedian(first)) if active.any() else None,
            "median_last_report_s": float(np.nanmedian(last)) if active.any() else None,
            "batches_per_municipality": {
                "min": int(n_batches.min()) if n_batches.size else 0,
                "median": float(np.median(n_batches)) if n_batches.size else 0.0,
                "max": int(n_batches.max()) if n_batches.size else 0,
            },
            "config_fingerprint": self.config_fingerprint,
        }
        if frame is not None and active.any():
            m_last = int(np.nanargmax(np.where(active, last, -np.inf)))
            out["last_municipality"] = frame.muni_codes[m_last]
        return out


def _cumulative_from_increments(units: np.ndarray, seqs: np.ndarray, incs: np.ndarray) -> np.ndarray:
    """Per-unit running sums of increments (rows sorted by seq); exact for binary fractions."""
    order = np.lexsort((seqs, units))
    u, x = units[order], incs[order]
    cs = np.cumsum(x)
    starts = np.r_[True, u[1:] != u[:-1]]
    base = np.where(starts, cs - x, 0.0)
    base = np.maximum.accumulate(np.where(starts, base, -np.inf))
    cum_sorted = cs - base
    out = np.empty_like(cum_sorted)
    out[order] = cum_sorted
    return out


# --------------------------------------------------------------------------- generation
def default_wijk_keys(unit_codes: Sequence[str]) -> list[str]:
    """Wijk (neighbourhood cluster) key of each unit derived from its CBS buurt code.

    CBS buurt codes are ``BU`` + 4-digit gemeente + 2-digit wijk + 2-digit buurt, so the wijk
    is the first eight characters.  Codes of another shape form singleton clusters.
    """
    return [c[:8] if _BUURT_CODE.match(c) else c for c in unit_codes]


def _close_datetimes(frame: GeographyFrame, cfg: NightConfig, election_date: date) -> list[datetime]:
    pc = cfg.polls_close
    tz = ZoneInfo(pc.timezone)
    out = []
    for m, code in enumerate(frame.muni_codes):
        pcode = frame.province_codes[int(frame.muni_province[m])]
        hhmm = pc.municipalities.get(code) or pc.provinces.get(pcode) or pc.default
        t = parse_hhmm(hhmm)
        out.append(
            datetime(election_date.year, election_date.month, election_date.day, t.hour, t.minute, tzinfo=tz)
        )
    return out


def polls_close_times(
    frame: GeographyFrame,
    config: NightConfig | None = None,
    election_date: date | None = None,
    units_mask: np.ndarray | None = None,
) -> tuple[datetime, np.ndarray]:
    """Poll closing per municipality.

    Returns ``(reference_close, offset_s)``: the tz-aware local datetime of the earliest closing
    among the municipalities with units in ``units_mask`` (``sim_time_s = 0``) and each
    municipality's closing relative to it, in seconds (M,).  Services use this to rebuild a
    persisted timeline (:meth:`Timeline.from_records`).
    """
    cfg = config or load_night_config()
    edate = election_date or cfg.polls_close.fallback_election_date
    closes = _close_datetimes(frame, cfg, edate)
    close_ts = np.array([c.timestamp() for c in closes])
    mask = np.ones(frame.n_units, dtype=bool) if units_mask is None else np.asarray(units_mask, dtype=bool)
    active = np.unique(frame.unit_muni[mask])
    i = int(active[np.argmin(close_ts[active])])
    return closes[i], close_ts - close_ts[i]


def _tier_range(rep: ReportingSpeedConfig, ballots: float) -> tuple[int, int]:
    for tier in rep.batches_by_size:
        if tier.max_ballots is None or ballots <= tier.max_ballots:
            return tier.min, tier.max
    last = rep.batches_by_size[-1]  # pragma: no cover - validated to be open-ended
    return last.min, last.max


@dataclass
class _MuniBatches:
    times: np.ndarray  # (n,) sim seconds
    ballots: np.ndarray  # (n,)
    frac_after: np.ndarray  # (n,)
    piece_batch: np.ndarray  # (k,) local batch index
    piece_unit: np.ndarray  # (k,) global unit index
    piece_inc: np.ndarray  # (k,)
    piece_cum: np.ndarray  # (k,)


def _quantise(x: np.ndarray) -> np.ndarray:
    q = np.round(x / FRACTION_QUANTUM) * FRACTION_QUANTUM
    return np.clip(q, FRACTION_QUANTUM, 1.0 - FRACTION_QUANTUM)


def _municipality_batches(
    rng: np.random.Generator,
    units: np.ndarray,
    ballots: np.ndarray,
    wijk: np.ndarray,
    n_batches: int,
    start_s: float,
    duration_s: float,
    rep: ReportingSpeedConfig,
) -> _MuniBatches:
    """Compose the batches of one municipality (all vectorised over its units)."""
    k = len(units)
    B = float(ballots.sum())
    # --- reporting order: wijken in random order, units by code within the wijk
    uniq_wijk, wijk_inv = np.unique(wijk, return_inverse=True)
    wijk_rank = rng.permutation(len(uniq_wijk))[wijk_inv]
    order = np.lexsort((units, wijk_rank))
    u, b, w = units[order], ballots[order].astype(np.float64), wijk_rank[order]
    ce = np.cumsum(b)
    cs = ce - b
    # --- batch cuts (Dirichlet sizes), snapped to wijk boundaries
    props = rng.dirichlet(np.full(n_batches, rep.batch_size_concentration)) if n_batches > 1 else np.ones(1)
    cuts = B * np.cumsum(props)[:-1] if B > 0 else np.zeros(0)
    if cuts.size and k > 1:
        boundaries = ce[:-1][w[1:] != w[:-1]]
        if boundaries.size:
            j = np.clip(np.searchsorted(boundaries, cuts), 1, boundaries.size) - 1
            j2 = np.clip(j + 1, 0, boundaries.size - 1)
            cand = np.where(
                np.abs(boundaries[j] - cuts) <= np.abs(boundaries[j2] - cuts), boundaries[j], boundaries[j2]
            )
            tol = rep.wijk_snap_tolerance * B / n_batches
            cuts = np.where(np.abs(cand - cuts) <= tol, cand, cuts)
    cuts = np.unique(cuts[(cuts > 0) & (cuts < B)])
    # --- assign units (or pieces of units) to batches
    mid = 0.5 * (cs + ce)
    j_mid = np.searchsorted(cuts, mid, side="right")
    pp = rep.partial_precinct
    split = (b >= pp.min_ballots) if pp.enabled else np.zeros(k, dtype=bool)
    eps = pp.min_piece_fraction * b
    j0 = np.where(split, np.searchsorted(cuts, cs + eps, side="right"), j_mid)
    j1 = np.where(split, np.searchsorted(cuts, ce - eps, side="right"), j_mid)
    j1 = np.maximum(j1, j0)
    npieces = j1 - j0 + 1
    rep_idx = np.repeat(np.arange(k), npieces)
    offsets = np.cumsum(npieces) - npieces
    within = np.arange(len(rep_idx)) - offsets[rep_idx]
    piece_batch = j0[rep_idx] + within
    is_last = within == npieces[rep_idx] - 1
    piece_cum = np.ones(len(rep_idx))
    mid_piece = np.flatnonzero(~is_last)  # only split units (b ≥ min_ballots > 0) have these
    if mid_piece.size:
        owner = rep_idx[mid_piece]
        piece_cum[mid_piece] = _quantise((cuts[piece_batch[mid_piece]] - cs[owner]) / b[owner])
    prev = np.where(within == 0, 0.0, np.r_[0.0, piece_cum[:-1]])
    piece_inc = piece_cum - prev
    # --- integer ballots per piece consistent with floor(f × ballots) counting
    bb = b[rep_idx]
    piece_ballots = np.floor(piece_cum * bb) - np.floor(prev * bb)
    # --- drop empty batches and renumber
    used, piece_local = np.unique(piece_batch, return_inverse=True)
    n = len(used)
    batch_ballots = np.bincount(piece_local, weights=piece_ballots, minlength=n).astype(np.int64)
    if B > 0:
        frac_after = np.cumsum(batch_ballots) / B
    else:
        frac_after = np.cumsum(np.bincount(piece_local, weights=is_last.astype(float), minlength=n)) / max(
            k, 1
        )
    frac_after[-1] = 1.0
    # --- batch times: first at start, last at start + duration, others uniform in between
    if n == 1:
        rel = np.zeros(1)
    else:
        rel = np.r_[0.0, np.sort(rng.uniform(size=n - 2)), 1.0]
    times = start_s + duration_s * rel
    gap = rep.min_batch_gap_s * np.arange(n)
    times = np.maximum.accumulate(times - gap) + gap
    times = np.round(times)
    return _MuniBatches(
        times=times,
        ballots=batch_ballots,
        frac_after=frac_after,
        piece_batch=piece_local,
        piece_unit=u[rep_idx],
        piece_inc=piece_inc,
        piece_cum=piece_cum,
    )


def generate_timeline(
    frame: GeographyFrame,
    ballots_per_unit: np.ndarray,
    config: NightConfig | None = None,
    seed: int = 0,
    units_mask: np.ndarray | None = None,
    *,
    election_date: date | None = None,
    unit_wijk: Sequence[str] | np.ndarray | None = None,
) -> Timeline:
    """Generate the election-night reporting timeline.

    Args:
        frame: the geography (REAL CBS or synthetic).
        ballots_per_unit: (U,) ballots cast per unit (e.g. ``ElectionDraw.turnout.ballots_cast``);
            drives batch sizes and counting speed.
        config: night configuration (default: ``config/night.yaml``).
        seed: root seed; the timeline is a pure function of its inputs.
        units_mask: (U,) bool — only these units report (default: all units).
        election_date: local date of the election (default ``polls_close.fallback_election_date``).
        unit_wijk: (U,) wijk code per unit (default: derived from CBS buurt codes).

    Returns:
        The :class:`Timeline` (events sorted by time, seq 1..N).
    """
    cfg = config or load_night_config()
    rep = cfg.reporting
    U, M = frame.n_units, frame.n_munis
    ballots = np.asarray(ballots_per_unit)
    if ballots.shape != (U,):
        raise ElectionNightError(f"ballots_per_unit must have shape ({U},), got {ballots.shape}")
    ballots = ballots.astype(np.int64)
    if (ballots < 0).any():
        raise ElectionNightError("ballots_per_unit must be non-negative")
    mask = np.ones(U, dtype=bool) if units_mask is None else np.asarray(units_mask, dtype=bool)
    if mask.shape != (U,):
        raise ElectionNightError("units_mask must have one entry per unit")
    if not mask.any():
        raise ElectionNightError("units_mask selects no units")
    wijk_keys = default_wijk_keys(frame.unit_codes) if unit_wijk is None else [str(x) for x in unit_wijk]
    if len(wijk_keys) != U:
        raise ElectionNightError("unit_wijk must have one entry per unit")
    _, wijk_id = np.unique(np.asarray(wijk_keys, dtype=object).astype(str), return_inverse=True)

    reference_close, offset_s = polls_close_times(frame, cfg, election_date, mask)
    active_munis = np.unique(frame.unit_muni[mask])

    # --- municipality aggregates (vectorised over units)
    um = frame.unit_muni
    B_m = np.bincount(um[mask], weights=ballots[mask], minlength=M)
    urb_class = np.asarray(frame.unit_urbanity_class, dtype=np.int64)
    urb_steps = np.where((urb_class >= 1) & (urb_class <= 5), 5 - urb_class, 2).astype(float)  # 0..4
    wts = np.where(mask, np.maximum(ballots, 0).astype(float) + 1.0, 0.0)
    urb_m = np.bincount(um, weights=urb_steps * wts, minlength=M) / np.maximum(
        np.bincount(um, weights=wts, minlength=M), 1e-9
    )
    # --- province effects (fixed + seeded)
    P = frame.n_provinces
    prov_first = np.zeros(P)
    prov_dur = np.ones(P)
    for p, pcode in enumerate(frame.province_codes):
        prng = make_rng(seed, "timeline", "province", pcode)
        prov_first[p] = rep.province_effect_minutes.get(pcode, 0.0) + prng.normal(
            0.0, rep.province_random_sd_minutes
        )
        prov_dur[p] = float(np.exp(prng.normal(0.0, rep.province_duration_sd)))

    fr, du = rep.first_report, rep.duration
    parts: list[tuple[int, _MuniBatches]] = []
    by_muni, bounds = _units_by_muni(frame, mask)
    for m in active_munis.tolist():
        rng = make_rng(seed, "timeline", "muni", frame.muni_codes[m])
        units_m = by_muni[bounds[m] : bounds[m + 1]]
        Bm = float(B_m[m])
        p = int(frame.muni_province[m])
        z_first, z_dur, z_end = rng.normal(size=3)
        size_term = fr.minutes_per_log10_ballots * np.log10(max(Bm, 1.0) / rep.size_ref_ballots)
        first_min = (
            fr.base_minutes + size_term + fr.minutes_per_urbanity_step * urb_m[m] + prov_first[p]
        ) * np.exp(fr.jitter_sd * z_first)
        first_min = float(np.clip(first_min, fr.min_minutes, fr.max_minutes))
        lo, hi = _tier_range(rep, Bm)
        n_pos = int(np.count_nonzero(ballots[units_m]))
        cap = n_pos
        if rep.partial_precinct.enabled:
            cap += int((ballots[units_m] // rep.partial_precinct.min_ballots).sum())
        n_b = int(rng.integers(lo, hi + 1))
        n_b = max(1, min(n_b, cap)) if Bm > 0 else 1
        start_s = float(offset_s[m]) + 60.0 * first_min
        if n_b > 1:
            dur_min = (
                du.base_minutes
                * (max(Bm, 1.0) / du.ref_ballots) ** du.elasticity
                * (1.0 + du.urbanity_factor_per_step * urb_m[m])
                * np.exp(du.jitter_sd * z_dur)
                * prov_dur[p]
            )
            dur_min = max(float(dur_min), du.min_minutes)
            cap_end = rep.latest_end_minutes - rep.end_spread_minutes * float(1.0 / (1.0 + np.exp(-z_end)))
            if start_s / 60.0 + dur_min > cap_end:
                dur_min = max(cap_end - start_s / 60.0, du.min_minutes)
        else:
            dur_min = 0.0
        mb = _municipality_batches(
            rng, units_m, ballots[units_m], wijk_id[units_m], n_b, start_s, 60.0 * dur_min, rep
        )
        parts.append((m, mb))

    tl = _assemble(frame, parts, mask, seed, reference_close, cfg, offset_s)
    log.info(
        "generated election-night timeline",
        extra=log_ctx(events=tl.n_events, units=int(mask.sum()), end=tl.local_clock(tl.end_time_s)),
    )
    return tl


def _units_by_muni(frame: GeographyFrame, mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """``(order, bounds)``: masked units grouped by municipality (sorted within each)."""
    sel = np.flatnonzero(mask)
    order = sel[np.lexsort((sel, frame.unit_muni[sel]))]
    bounds = np.searchsorted(frame.unit_muni[order], np.arange(frame.n_munis + 1))
    return order, bounds


def _assemble(
    frame: GeographyFrame,
    parts: list[tuple[int, _MuniBatches]],
    mask: np.ndarray,
    seed: int,
    reference_close: datetime,
    cfg: NightConfig,
    offset_s: np.ndarray,
) -> Timeline:
    times = np.concatenate([mb.times for _, mb in parts])
    munis = np.concatenate([np.full(len(mb.times), m, dtype=np.int64) for m, mb in parts])
    bidx = np.concatenate([np.arange(1, len(mb.times) + 1, dtype=np.int64) for _, mb in parts])
    nb = np.concatenate([np.full(len(mb.times), len(mb.times), dtype=np.int64) for _, mb in parts])
    ballots = np.concatenate([mb.ballots for _, mb in parts])
    fracs = np.concatenate([mb.frac_after for _, mb in parts])
    ev_offsets = np.cumsum([0] + [len(mb.times) for _, mb in parts])
    piece_ev = np.concatenate([mb.piece_batch + ev_offsets[i] for i, (_, mb) in enumerate(parts)])
    piece_unit = np.concatenate([mb.piece_unit for _, mb in parts])
    piece_inc = np.concatenate([mb.piece_inc for _, mb in parts])
    piece_cum = np.concatenate([mb.piece_cum for _, mb in parts])

    order = np.lexsort((bidx, munis, times))  # time, then municipality index, then batch
    new_pos = np.empty_like(order)
    new_pos[order] = np.arange(len(order))
    pev = new_pos[piece_ev]
    porder = np.lexsort((piece_unit, pev))
    counts = np.bincount(pev, minlength=len(order))
    ptr = np.r_[0, np.cumsum(counts)].astype(np.int64)
    tz = cfg.polls_close.timezone
    return Timeline(
        seed=seed,
        reference_close=reference_close,
        timezone=tz,
        n_units=frame.n_units,
        n_munis=frame.n_munis,
        sim_time_s=times[order].astype(np.float64),
        muni=munis[order],
        province=frame.muni_province[munis[order]].astype(np.int64),
        ballots=ballots[order].astype(np.int64),
        muni_fraction_after=fracs[order].astype(np.float64),
        batch_index=bidx[order],
        batches_in_muni=nb[order],
        unit_ptr=ptr,
        units=piece_unit[porder].astype(np.int64),
        increments=piece_inc[porder].astype(np.float64),
        cumulative=piece_cum[porder].astype(np.float64),
        muni_close_offset_s=offset_s.astype(np.float64),
        units_mask=mask.copy(),
        config_fingerprint=cfg.fingerprint(),
        checkpoint_every=cfg.reporting.checkpoint_every,
    )
