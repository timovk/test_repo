"""Monte Carlo forecasting engine: vectorised, block-seeded, optionally multi-process.

:func:`run_forecast` prepares a :class:`~app.forecasting.plan.ForecastPlan` (cells, anchored race
pipelines, resolved error structure), simulates ``n_sims`` elections in blocks of
``config.block_size`` draws and summarises them into a :class:`~app.forecasting.result.ForecastResult`.

Per block of ``n`` draws (docs/FORECASTING.md §3):

* a national party error with Student-t tails, a common ideological swing, and the occurrence of
  probabilistic scenario events (as in ``simulate_election``);
* province × party errors and municipality × party errors — an iid and a spatially correlated
  Gaussian-process part (the simulation model's kernel) drawn as one Gaussian field;
* cell noise ``unit_sd / √n_eff`` (the neighbourhood noise that survives aggregation to a cell);
* turnout errors (national, municipal, cell) and a differential mobilisation of each party;
* per race × line candidate-performance errors (a presidential ticket's error is national: shared
  by every province contest, as in ``app.simulation.voting.race_line_shocks``).

Party-utility errors reach each fragment through its Jacobian-matching maps (national errors
including the local elasticity) and turnout errors through its per-party turnout gains, so a
fragment responds to shocks like the sum of its neighbourhoods (plan module).  The fragment party
state goes through the anchored race pipelines of every layer; cell votes are summed to race totals
(``np.add.reduceat``) and winners, electoral votes and seats derived.

**Reproducibility.**  Block ``b`` draws all its random numbers from streams of
``derive_seed(seed, "forecast", b)`` and is always evaluated as one array of the same shape, so a
draw's numbers do not depend on ``chunk_size``, on the number of worker processes or on the order in
which workers finish: integer accumulators are summed (order-free), floating-point sums are kept per
block and added in block order, per-draw arrays are concatenated in block order.

All outputs are SIMULATED estimates of a FICTIONAL model — never predictions of real elections.
"""

from __future__ import annotations

import math
import multiprocessing
import os
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from app.core.constitution import ConstitutionConfig, EVAllocationMethod
from app.core.errors import ElectionError
from app.core.logging import Timer, get_logger, log_ctx
from app.core.rng import derive_seed, make_rng
from app.core.settings import get_settings
from app.elections.types import RaceSpec
from app.forecasting import kernel
from app.forecasting.config import ForecastConfig, load_forecast_config
from app.forecasting.plan import SOURCE_FRAGMENT, ChamberPlan, ForecastPlan, prepare_forecast
from app.forecasting.result import ForecastResult, build_result
from app.simulation.structural import StructuralModel
from app.simulation.voting import ElectionContext

log = get_logger(__name__)

ProgressCallback = Callable[[int, int], None]

_F32 = np.float32
#: Per-chunk counters (a chunk holds far fewer than 2**31 draws); merged into int64.
_COUNT = np.int32
_BLAS_ENV = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
_MAX_SEED = 2**63


# --------------------------------------------------------------------------- one block
@dataclass
class BlockOutput:
    """Race-level totals of one block of ``n`` draws (before summarising)."""

    n: int
    race_votes: list[np.ndarray]  # per layer (n, R_l, L_l) float64
    race_valid: list[np.ndarray]  # per layer (n, R_l) float64
    turnout: np.ndarray  # (n,) national turnout (ballots / eligible)
    pres_province_votes: np.ndarray | None = None  # (n, Pv, T) ticket votes per province
    pres_muni_votes: np.ndarray | None = None  # (n, M_p, T) ticket votes per municipality
    pres_district_votes: np.ndarray | None = None  # (n, D, T) ticket votes per House district


def _ticket_votes(votes: np.ndarray, line_ticket: np.ndarray, n_tickets: int) -> np.ndarray:
    """Re-key line votes (K, n, L) to ticket votes (K, n, T) with a per-row map (K, L) (−1 = none)."""
    K, n, L = votes.shape
    real = line_ticket >= 0
    if n_tickets <= L and bool(np.all(~real | (line_ticket == np.arange(L)[None, :]))):
        out = votes[:, :, :n_tickets].copy()
        if not real[:, :n_tickets].all():
            out *= real[:, None, :n_tickets]
        return out
    out = np.zeros((K, n, n_tickets), dtype=votes.dtype)
    rows = np.arange(K)
    for j in range(L):
        ok = real[:, j]
        if ok.any():
            out[rows[ok], :, line_ticket[ok, j]] += votes[rows[ok], :, j]
    return out


def simulate_block(plan: ForecastPlan, seed: int, block: int, n: int) -> BlockOutput:
    """Simulate block ``block`` (``n`` draws) of a forecast with root ``seed``."""
    base = derive_seed(seed, "forecast", block)
    sh = plan.shocks
    P, F = plan.n_parties, plan.n_fragments
    Pv, M = len(plan.province_codes), len(plan.muni_codes)

    # --- national party error: Student-t tails + common ideological swing + events ----------
    df = sh.tail_df
    idio = make_rng(base, "national").standard_t(df, size=(n, P)) * math.sqrt((df - 2.0) / df)
    swing = make_rng(base, "ideological-swing").standard_normal((n, plan.ideology_unit.shape[1]))
    common = np.zeros((n, P))
    for d in range(plan.ideology_unit.shape[1]):
        common += swing[:, d, None] * plan.ideology_unit[None, :, d]
    rho = sh.ideological_swing_share
    national = sh.national_sd * (math.sqrt(1.0 - rho) * idio + math.sqrt(rho) * common)
    prov = make_rng(base, "province").standard_normal((Pv, n, P)) * sh.province_sd
    t_nat = make_rng(base, "turnout-national").standard_normal(n) * sh.turnout_national_sd
    if len(plan.event_prob):
        occurred = make_rng(base, "events").random((n, len(plan.event_prob))) < plan.event_prob[None, :]
        factor = occurred.astype(float) - plan.event_prob[None, :]
        for k in range(len(plan.event_prob)):
            national += factor[:, k, None] * plan.event_national[k][None, :]
            prov += plan.event_province[k][:, None, :] * factor[None, :, k, None]
            t_nat += factor[:, k] * plan.event_turnout[k]

    # --- municipal errors: iid + spatially correlated GP field, one Gaussian field (M, n, P) ------
    z = make_rng(base, "municipality").standard_normal((M, n * P), dtype=_F32)
    if plan.muni_chol is not None:
        muni = (plan.muni_chol @ z).reshape(M, n, P)
    else:
        muni = (z * _F32(sh.municipality_sd)).reshape(M, n, P)
    del z

    # --- fragment utility: local shocks and the national shock, mapped through each fragment's
    #     Jacobian-matching matrices (national: elasticity-weighted) ------------------------------
    local = make_rng(base, "cell").standard_normal((F, n, P), dtype=_F32)
    local *= (_F32(sh.unit_sd) * plan.cell_sd)[:, None, None]
    local += muni[plan.frag_muni]
    local += prov.astype(_F32)[plan.frag_prov]
    del muni
    util = np.matmul(local, plan.A_loc_T)
    del local
    util += np.matmul(national.astype(_F32)[None, :, :], plan.A_nat_T)

    # --- turnout ----------------------------------------------------------------------------
    tshift = make_rng(base, "turnout-cell").standard_normal((F, n), dtype=_F32)
    tshift *= (_F32(sh.turnout_local_sd * sh.turnout_unit_share) * plan.cell_sd)[:, None]
    t_muni = make_rng(base, "turnout-municipality").standard_normal((M, n), dtype=_F32)
    t_muni *= _F32(sh.turnout_local_sd)
    tshift += t_muni[plan.frag_muni]
    tshift += t_nat.astype(_F32)[None, :]
    party_t = (make_rng(base, "turnout-party").standard_normal((n, P)) * sh.party_turnout_sd).astype(_F32)

    pi, T = kernel.fragment_state(
        plan.V, plan.Tq, util, tshift, party_t, plan.q_min, plan.q_max, plan.turnout_gain
    )
    del util, tshift
    ballots = T * plan.frag_eligible[:, None]
    turnout = ballots.sum(axis=0, dtype=np.float64) / max(plan.total_eligible, 1e-300)
    pim = bm = None
    if plan.needs_munis:
        pim, bm = kernel.municipality_state(pi, ballots, plan.muni_starts)

    out = BlockOutput(n=n, race_votes=[], race_valid=[], turnout=turnout)
    pres = plan.president
    for lp in plan.layers:
        if lp.source == SOURCE_FRAGMENT:
            pic, bc = pi[lp.cell_src], ballots[lp.cell_src]
        else:
            pic, bc = pim[lp.cell_src], bm[lp.cell_src]  # type: ignore[index]
        shock = None
        if sh.race_line_sd > 0:
            z = make_rng(base, "line", lp.index).standard_normal((lp.n_shocks + 1, n), dtype=_F32)
            z *= _F32(sh.race_line_sd)
            z[-1] = 0.0  # padding lines
            shock = z[lp.cell_shock].transpose(0, 2, 1)  # (C, n, L)
        shares, vfrac = kernel.line_shares(
            pic, lp.R, lp.a, lp.indep, lp.delta, lp.G, lp.ratio, lp.blank_base, lp.invalid, shock
        )
        valid = bc * vfrac
        votes = shares * valid[:, :, None]  # (C, n, L)
        rv = kernel.group_sum(votes, lp.race_starts).astype(np.float64)  # (R, n, L)
        out.race_votes.append(np.ascontiguousarray(rv.transpose(1, 0, 2)))
        out.race_valid.append(
            np.ascontiguousarray(kernel.group_sum(valid, lp.race_starts).T, dtype=np.float64)
        )
        if pres is not None and lp.index == pres.layer:
            n_t = len(pres.tickets)
            prov_votes = _ticket_votes(rv[pres.local], pres.line_ticket, n_t)  # (Pv, n, T) float64
            out.pres_province_votes = np.ascontiguousarray(prov_votes.transpose(1, 0, 2))
            local_map = np.full((lp.n_races, lp.L), -1, dtype=np.int64)
            local_map[pres.local] = pres.line_ticket
            cell_votes = _ticket_votes(votes, local_map[lp.cell_race], n_t)  # (C, n, T)
            if pres.district_perm is not None and pres.district_starts is not None:
                dv = kernel.group_sum(cell_votes[pres.district_perm], pres.district_starts)
                out.pres_district_votes = np.ascontiguousarray(dv.transpose(1, 0, 2), dtype=np.float64)
            if plan.municipality_distributions:
                cv = cell_votes if pres.muni_perm is None else cell_votes[pres.muni_perm]
                mv = kernel.group_sum(cv, pres.muni_starts)
                out.pres_muni_votes = np.ascontiguousarray(mv.transpose(1, 0, 2), dtype=np.float64)
    return out


# --------------------------------------------------------------------------- accumulation
@dataclass
class ChunkResult:
    """Accumulated output of consecutive blocks (one worker task)."""

    blocks: list[int]
    sums: list[dict[str, np.ndarray]]  # float64 sums per block (merged in block order)
    counts: dict[str, np.ndarray]  # int32 counters (order-independent)
    draws: dict[str, np.ndarray]  # per-draw arrays, concatenated in block order


@dataclass
class MergedDraws:
    """All chunks merged in block order: float sums, int64 counters and per-draw arrays."""

    n: int
    sums: dict[str, np.ndarray]
    counts: dict[str, np.ndarray]
    draws: dict[str, np.ndarray]
    n_blocks: int = 0


def _bin_index(shares: np.ndarray, bins: int) -> np.ndarray:
    return np.clip((shares * bins).astype(np.int64), 0, bins - 1)


def _chamber_seats(chamber: ChamberPlan, winners: dict[int, np.ndarray], n: int) -> np.ndarray:
    """Seats per chamber key (n, K) from the per-layer race winners, plus holdovers."""
    K = len(chamber.keys)
    seats = np.broadcast_to(chamber.holdover[None, :], (n, K)).astype(np.int64)
    for li, local, table in chamber.parts:
        w = winners[li][:, local]  # (n, R_s)
        key = table[np.arange(len(local))[None, :], w]  # (n, R_s)
        for k in range(K):
            seats[:, k] += (key == k).sum(axis=1)
    return seats


def _derive_president(plan: ForecastPlan, out: BlockOutput) -> dict[str, np.ndarray]:
    """Electoral votes, winners, tipping points and PV/EV divergence of every draw of a block."""
    pres = plan.president
    assert pres is not None and out.pres_province_votes is not None
    tv = out.pres_province_votes  # (n, Pv, T)
    n, Pv, T = tv.shape
    rows = np.arange(n)
    prov_tot = tv.sum(axis=2)
    share = tv / np.maximum(prov_tot, 1e-300)[:, :, None]
    winner = tv.argmax(axis=2)  # (n, Pv)
    pv = tv.sum(axis=1)
    pv_share = pv / np.maximum(pv.sum(axis=1, keepdims=True), 1e-300)
    ev = np.zeros((n, T), dtype=np.int64)
    method = EVAllocationMethod(pres.method)
    if method is EVAllocationMethod.WINNER_TAKE_ALL:
        for p in range(Pv):
            ev[rows, winner[:, p]] += pres.ev[p]
    elif method is EVAllocationMethod.PROPORTIONAL:
        quotas = share * pres.ev[None, :, None]
        base = np.floor(quotas).astype(np.int64)
        rem = pres.ev[None, :] - base.sum(axis=2)
        order = np.argsort(-(quotas - base), axis=2, kind="stable")
        rank = np.empty_like(order)
        np.put_along_axis(rank, order, np.broadcast_to(np.arange(T), order.shape), axis=2)
        ev = (base + (rank < rem[:, :, None])).sum(axis=1)
    else:  # DISTRICT
        for p in range(Pv):
            ev[rows, winner[:, p]] += pres.statewide_ev
        assert out.pres_district_votes is not None
        dwin = out.pres_district_votes.argmax(axis=2)
        for d in range(dwin.shape[1]):
            ev[rows, dwin[:, d]] += 1
    top = ev.max(axis=1)
    n_top = (ev == top[:, None]).sum(axis=1)
    arg = ev.argmax(axis=1)
    outright = top >= pres.majority
    winner_t = np.where(outright, arg, -1)
    leader = np.where(n_top == 1, arg, -1)
    pv_winner = pv.argmax(axis=1)
    ref = np.where(winner_t >= 0, winner_t, leader)
    tied_pv = np.where(ev == top[:, None], pv, -np.inf)
    tp_ticket = np.where(ref >= 0, ref, tied_pv.argmax(axis=1))
    own = np.take_along_axis(share, tp_ticket[:, None, None].repeat(Pv, axis=1), axis=2)[:, :, 0]
    others = share.copy()
    np.put_along_axis(others, tp_ticket[:, None, None].repeat(Pv, axis=1), -np.inf, axis=2)
    margin = own - (others.max(axis=2) if T > 1 else 0.0)
    order = np.argsort(-margin, axis=1, kind="stable")
    cum = np.cumsum(pres.ev[order], axis=1)
    pos = (cum >= pres.majority).argmax(axis=1)
    tipping = np.take_along_axis(order, pos[:, None], axis=1)[:, 0]
    return {
        "pv_share": pv_share.astype(_F32),
        "pv_votes": pv,
        "ev": ev.astype(np.int16),
        "province_winner": winner.astype(np.int16),
        "winner": winner_t.astype(np.int16),
        "leader": leader.astype(np.int16),
        "pv_winner": pv_winner.astype(np.int16),
        "tipping_point": tipping.astype(np.int16),
        "tipping_ticket": tp_ticket.astype(np.int16),
        "diverged": ((ref >= 0) & (pv_winner != ref)),
    }


class ChunkAccumulator:
    """Reduces block outputs to the (small) sufficient statistics of the summary."""

    def __init__(self, plan: ForecastPlan) -> None:
        self.plan = plan
        self.blocks: list[int] = []
        self.sums: list[dict[str, np.ndarray]] = []
        self.counts: dict[str, np.ndarray] = {}
        self.draws: dict[str, list[np.ndarray]] = {}
        bins = plan.share_bins
        for lp in plan.layers:
            self.counts[f"wins:{lp.index}"] = np.zeros((lp.n_races, lp.L), dtype=_COUNT)
            self.counts[f"hist:{lp.index}"] = np.zeros(lp.n_races * lp.L * bins, dtype=_COUNT)
        pres = plan.president
        if pres is not None and plan.municipality_distributions:
            Mp, T = len(pres.muni_index), len(pres.tickets)
            self.counts["muni_hist"] = np.zeros(Mp * T * bins, dtype=_COUNT)
            self.counts["muni_lead"] = np.zeros((Mp, T), dtype=_COUNT)

    def _keep(self, name: str, arr: np.ndarray) -> None:
        self.draws.setdefault(name, []).append(arr)

    def add(self, block: int, out: BlockOutput) -> None:
        plan = self.plan
        bins = plan.share_bins
        n = out.n
        sums: dict[str, np.ndarray] = {}
        winners: dict[int, np.ndarray] = {}
        for lp, rv, rvalid in zip(plan.layers, out.race_votes, out.race_valid, strict=True):
            li, R, L = lp.index, lp.n_races, lp.L
            shares = rv / np.maximum(rvalid, 1e-300)[:, :, None]
            w = rv.argmax(axis=2)
            winners[li] = w
            flat = (np.arange(R)[None, :] * L + w).ravel()
            self.counts[f"wins:{li}"] += np.bincount(flat, minlength=R * L).reshape(R, L)
            cell = (np.arange(R * L).reshape(1, R, L) * bins) + _bin_index(shares, bins)
            self.counts[f"hist:{li}"] += np.bincount(cell.ravel(), minlength=R * L * bins)
            sums[f"share:{li}"] = shares.sum(axis=0)
            sums[f"votes:{li}"] = rv.sum(axis=0)
        self._keep("turnout", out.turnout.astype(_F32))
        sums["turnout"] = np.array([out.turnout.sum()])
        if plan.president is not None:
            d = _derive_president(plan, out)
            sums["pv_votes"] = d.pop("pv_votes").sum(axis=0)
            for k, v in d.items():
                self._keep(k, v)
            if out.pres_muni_votes is not None:
                mv = out.pres_muni_votes
                Mp, T = mv.shape[1], mv.shape[2]
                ms = mv / np.maximum(mv.sum(axis=2, keepdims=True), 1e-300)
                sums["muni_share"] = ms.sum(axis=0)
                cell = (np.arange(Mp * T).reshape(1, Mp, T) * bins) + _bin_index(ms, bins)
                self.counts["muni_hist"] += np.bincount(cell.ravel(), minlength=Mp * T * bins)
                lead = mv.argmax(axis=2)
                self.counts["muni_lead"] += np.bincount(
                    (np.arange(Mp)[None, :] * T + lead).ravel(), minlength=Mp * T
                ).reshape(Mp, T)
        for name in ("house", "senate", "governors"):
            chamber = getattr(plan, name)
            if chamber is not None:
                self._keep(name, _chamber_seats(chamber, winners, n).astype(np.int16))
        self.blocks.append(block)
        self.sums.append(sums)

    def result(self) -> ChunkResult:
        return ChunkResult(
            blocks=list(self.blocks),
            sums=self.sums,
            counts=self.counts,
            draws={k: np.concatenate(v) for k, v in self.draws.items()},
        )


def run_chunk(plan: ForecastPlan, seed: int, blocks: Sequence[tuple[int, int]]) -> ChunkResult:
    """Simulate and accumulate consecutive ``(block index, draws)`` blocks."""
    acc = ChunkAccumulator(plan)
    for b, n in blocks:
        acc.add(b, simulate_block(plan, seed, b, n))
    return acc.result()


class ChunkMerger:
    """Merges chunk results as they arrive, in a way that does not depend on arrival order.

    Integer counters are summed immediately (exact, order-free, so histograms of finished chunks
    are not kept around); floating-point sums stay per block and are added in block order at the
    end; per-draw arrays are concatenated in block order.
    """

    def __init__(self) -> None:
        self.counts: dict[str, np.ndarray] = {}
        self.block_sums: list[tuple[int, dict[str, np.ndarray]]] = []
        self.chunk_draws: list[tuple[int, dict[str, np.ndarray]]] = []

    @property
    def blocks(self) -> set[int]:
        return {b for b, _ in self.block_sums}

    def add(self, chunk: ChunkResult) -> None:
        for k, v in chunk.counts.items():
            if k in self.counts:
                self.counts[k] += v
            else:
                self.counts[k] = v.astype(np.int64)
        self.block_sums.extend(zip(chunk.blocks, chunk.sums, strict=True))
        self.chunk_draws.append((chunk.blocks[0], chunk.draws))

    def result(self, n_total: int) -> MergedDraws:
        ordered = sorted(self.block_sums, key=lambda t: t[0])
        if [b for b, _ in ordered] != list(range(len(ordered))):
            raise ElectionError("forecast blocks are missing or duplicated (internal error)")
        sums: dict[str, np.ndarray] = {}
        for _, s in ordered:
            for k, v in s.items():
                sums[k] = v.copy() if k not in sums else sums[k] + v
        chunks = [d for _, d in sorted(self.chunk_draws, key=lambda t: t[0])]
        draws = {k: np.concatenate([c[k] for c in chunks]) for k in (chunks[0] if chunks else {})}
        return MergedDraws(n=n_total, sums=sums, counts=self.counts, draws=draws, n_blocks=len(ordered))


def merge_chunks(chunks: Sequence[ChunkResult], n_total: int) -> MergedDraws:
    """Merge chunk results in block order (identical for any chunking / completion order)."""
    merger = ChunkMerger()
    for c in chunks:
        merger.add(c)
    return merger.result(n_total)


# --------------------------------------------------------------------------- scheduling
_WORKER_PLAN: ForecastPlan | None = None


def _init_worker(plan: ForecastPlan) -> None:
    global _WORKER_PLAN
    _WORKER_PLAN = plan


def _worker_chunk(seed: int, blocks: list[tuple[int, int]]) -> ChunkResult:
    if _WORKER_PLAN is None:  # pragma: no cover - initializer always runs first
        raise RuntimeError("forecast worker was not initialised")
    return run_chunk(_WORKER_PLAN, seed, blocks)


@contextmanager
def _single_threaded_children() -> Any:
    """Spawned workers inherit the environment: give them single-threaded BLAS (no oversubscription)."""
    saved = {k: os.environ.get(k) for k in _BLAS_ENV}
    os.environ.update(dict.fromkeys(_BLAS_ENV, "1"))
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def available_cpus() -> int:
    """CPUs this process may run on."""
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):  # pragma: no cover - non-Linux
        return max(1, os.cpu_count() or 1)


def resolve_workers(requested: int, n_sims: int, n_blocks: int, config: ForecastConfig) -> int:
    """Worker processes for a run: explicit ``requested`` > config > ``NLFED_FORECAST_WORKERS`` > CPUs.

    In automatic mode (all zero) runs below ``config.parallel_min_simulations`` stay in-process.
    Never more workers than blocks; always in-process inside a daemonic process.
    """
    if requested < 0:
        raise ElectionError("workers must be ≥ 0")
    if multiprocessing.current_process().daemon:
        return 1  # daemonic processes may not have children
    w = requested or config.workers or int(get_settings().forecast_workers)
    if w == 0:
        if n_sims < config.parallel_min_simulations:
            return 1
        w = available_cpus()
    return max(1, min(int(w), n_blocks))


def _block_list(n_sims: int, block_size: int) -> list[tuple[int, int]]:
    n_blocks = -(-n_sims // block_size)
    return [(b, min(block_size, n_sims - b * block_size)) for b in range(n_blocks)]


def _chunk_tasks(
    blocks: list[tuple[int, int]], chunk_size: int, block_size: int, workers: int
) -> list[list[tuple[int, int]]]:
    per = max(1, chunk_size // block_size)
    if workers > 1:  # at least ~3 tasks per worker for load balance (scheduling only)
        per = max(1, min(per, -(-len(blocks) // (3 * workers))))
    return [blocks[i : i + per] for i in range(0, len(blocks), per)]


def execute_plan(
    plan: ForecastPlan,
    n_sims: int,
    seed: int,
    *,
    workers: int = 1,
    chunk_size: int = 2000,
    progress: ProgressCallback | None = None,
) -> tuple[MergedDraws, dict[str, Any]]:
    """Run the Monte Carlo loop for a prepared plan; returns the merged draws and runtime info."""
    blocks = _block_list(n_sims, plan.block_size)
    sizes = dict(blocks)
    tasks = _chunk_tasks(blocks, chunk_size, plan.block_size, workers)
    done = 0
    merger = ChunkMerger()
    used = workers if workers > 1 and len(tasks) > 1 else 1

    def report(res: ChunkResult) -> None:
        nonlocal done
        merger.add(res)
        done += sum(sizes[b] for b in res.blocks)
        if progress is not None:
            progress(done, n_sims)

    if used > 1:
        try:
            # processes are spawned while submitting, inside the single-threaded-BLAS environment
            with _single_threaded_children():
                ex = ProcessPoolExecutor(
                    max_workers=used,
                    mp_context=multiprocessing.get_context("spawn"),
                    initializer=_init_worker,
                    initargs=(plan,),
                )
                pending = {ex.submit(_worker_chunk, seed, t) for t in tasks}
            try:
                while pending:
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        report(fut.result())
            finally:
                ex.shutdown(wait=True, cancel_futures=True)
        except BrokenProcessPool as exc:
            log.warning("forecast worker pool failed (%s); finishing in-process", exc)
            used = 1
            have = merger.blocks
            for t in tasks:
                if t[0][0] not in have:
                    report(run_chunk(plan, seed, t))
    else:
        for t in tasks:
            report(run_chunk(plan, seed, t))
    merged = merger.result(n_sims)
    return merged, {"workers": used, "chunks": len(tasks), "blocks": len(blocks)}


# --------------------------------------------------------------------------- public entry point
def run_forecast(
    model: StructuralModel,
    races: Sequence[RaceSpec],
    context: ElectionContext | None,
    n_sims: int | None,
    seed: int,
    *,
    ev_by_province: Mapping[str, int] | None = None,
    constitution: ConstitutionConfig | None = None,
    holdover_senate: Mapping[str, int] | None = None,
    poll_shift: Mapping[str, float] | None = None,
    workers: int = 0,
    config: ForecastConfig | None = None,
    progress: ProgressCallback | None = None,
    ev_method: EVAllocationMethod | str | None = None,
    keep_draws: bool = True,
) -> ForecastResult:
    """Monte Carlo forecast of ``races`` under ``model`` and ``context`` (SIMULATED estimates).

    Parameters
    ----------
    model, races, context:
        The structural model, the contests (``app.simulation.races``) and the election context
        (``None`` = ``ElectionContext.from_scenario``).  A ``PRESIDENT`` race passed together with
        its ``PRESIDENT_PROVINCE`` contests is the national popular vote (sum of the provinces).
    n_sims, seed:
        Number of simulated elections (``None`` = ``config.simulations``) and root seed
        (``0 ≤ seed < 2**63``); same inputs + seed ⇒ identical result for any worker count.
    ev_by_province:
        Province → electoral votes (default: the contests' ``electoral_votes``).
    constitution:
        Majorities and chamber sizes (default: the active constitution).
    holdover_senate:
        Party → Senate seats not up for election (added to every draw's Senate composition).
    poll_shift:
        Party → national logit shift from ``app.polling.blend.shift_from_poll_average``
        (× ``config.polls.weight``; scaled by local elasticity like any national shift).
    workers:
        0 = automatic, 1 = in-process, ≥ 2 = spawned worker processes.
    progress:
        ``progress(done, total)`` called after every finished chunk (in the calling process).
    ev_method:
        Electoral-vote allocation (default: the scenario's ``electoral_college.allocation``).
    keep_draws:
        Keep per-draw arrays (EV, seats, national shares …) on ``result.draws``.
    """
    cfg = config if config is not None else load_forecast_config()
    n, seed = int(n_sims if n_sims is not None else cfg.simulations), int(seed)
    _check_run(n, seed, cfg)
    started = datetime.now(UTC).replace(tzinfo=None)
    t0 = time.perf_counter()
    plan = prepare_forecast(
        model,
        races,
        context,
        config=cfg,
        ev_by_province=ev_by_province,
        constitution=constitution,
        holdover_senate=holdover_senate,
        poll_shift=poll_shift,
        ev_method=ev_method,
    )
    prepare_s = time.perf_counter() - t0
    result = run_plan(plan, n, seed, workers=workers, config=cfg, progress=progress, keep_draws=keep_draws)
    timing = result.runtime["timing"]
    timing["prepare_s"] = round(prepare_s, 3)
    timing["total_s"] = round(prepare_s + timing["simulate_s"] + timing["summarise_s"], 3)
    result.runtime["started_at"] = started.isoformat(timespec="seconds")
    return result


def _check_run(n: int, seed: int, cfg: ForecastConfig) -> None:
    if n < 1 or n > cfg.max_simulations:
        raise ElectionError(f"n_sims must be between 1 and {cfg.max_simulations}")
    if not (0 <= seed < _MAX_SEED):
        raise ElectionError("seed must satisfy 0 ≤ seed < 2**63")


def run_plan(
    plan: ForecastPlan,
    n_sims: int,
    seed: int,
    *,
    workers: int = 0,
    config: ForecastConfig | None = None,
    progress: ProgressCallback | None = None,
    keep_draws: bool = True,
) -> ForecastResult:
    """Simulate and summarise a prepared plan (e.g. to re-run one election with other seeds or
    more draws without preparing it again).  ``config`` must be the configuration the plan was
    prepared with (same hash); see :func:`run_forecast` for the other parameters."""
    cfg = config if config is not None else load_forecast_config()
    if cfg.hash != plan.metadata.get("config_hash"):
        raise ElectionError("the forecast configuration differs from the one the plan was prepared with")
    n, seed = int(n_sims), int(seed)
    _check_run(n, seed, cfg)
    started = datetime.now(UTC).replace(tzinfo=None)
    n_blocks = -(-n // plan.block_size)
    w = resolve_workers(int(workers), n, n_blocks, cfg)
    t1 = time.perf_counter()
    with Timer(log, f"forecast Monte Carlo ({n} draws, {w} worker(s))"):
        merged, runtime = execute_plan(plan, n, seed, workers=w, chunk_size=cfg.chunk_size, progress=progress)
    t2 = time.perf_counter()
    result = build_result(plan, merged, seed=seed, config=cfg, keep_draws=keep_draws)
    t3 = time.perf_counter()
    result.runtime.update(
        {
            **runtime,
            "chunk_size": int(cfg.chunk_size),
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="seconds"),
            "timing": {
                "prepare_s": 0.0,
                "simulate_s": round(t2 - t1, 3),
                "summarise_s": round(t3 - t2, 3),
                "total_s": round(t3 - t1, 3),
            },
            "draws_per_second": round(n / max(t2 - t1, 1e-9), 1),
        }
    )
    log.info(
        "forecast finished",
        extra=log_ctx(
            draws=n,
            seed=seed,
            workers=runtime["workers"],
            seconds=round(t3 - t1, 2),
            input_hash=plan.metadata["input_hash"],
        ),
    )
    return result
