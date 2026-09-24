"""Forecast results: summary types and the reduction of merged Monte Carlo draws into them.

Every number in a :class:`ForecastResult` is a SIMULATED estimate of the FICTIONAL political
model of the NL Federal Election Simulator (``data_category = "SIMULATED"``) — the share of
simulated elections in which something happens under the model's assumptions, never a prediction
of a real election.

Quantiles of scalar outcomes kept per draw (electoral votes, seats, national popular vote,
turnout) are exact (NumPy ``linear`` percentiles); quantiles of race-line and municipality shares
come from histograms with ``outputs.share_bins`` bins over [0, 1] (linear interpolation within the
bin, error ≤ one bin width = 0.25 pp by default).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from app.core.constitution import DataCategory
from app.core.rng import config_hash

if TYPE_CHECKING:
    from app.forecasting.config import ForecastConfig
    from app.forecasting.engine import MergedDraws
    from app.forecasting.plan import ChamberPlan, ForecastPlan

DISCLAIMER = (
    "SIMULATED model estimates of a FICTIONAL electoral system: shares of simulated elections "
    "under the NL Federal Election Simulator's invented political model. Not a prediction of any "
    "real election; parties and candidates are fictional."
)

_QS = (5.0, 25.0, 50.0, 75.0, 95.0)


def _r(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(float(x), nd)


# --------------------------------------------------------------------------- building blocks
@dataclass
class Summary:
    """Distribution summary of a scalar outcome."""

    mean: float
    median: float
    p05: float
    p25: float
    p75: float
    p95: float

    @classmethod
    def of(cls, x: np.ndarray) -> Summary:
        x = np.asarray(x, dtype=np.float64)
        if x.size == 0:
            return cls(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        p05, p25, p50, p75, p95 = np.percentile(x, _QS)
        return cls(float(x.mean()), float(p50), float(p05), float(p25), float(p75), float(p95))

    def to_dict(self) -> dict[str, float | None]:
        return {k: _r(v) for k, v in self.__dict__.items()}


@dataclass
class LineForecast:
    """Monte Carlo summary of one ballot line of one race."""

    key: str
    party_code: str | None
    label: str
    win_probability: float
    share_mean: float
    share_p05: float
    share_p50: float
    share_p95: float
    mean_votes: float
    expected_share: float  # deterministic model expectation (no shocks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "party_code": self.party_code,
            "label": self.label,
            "win_probability": _r(self.win_probability),
            "share_mean": _r(self.share_mean),
            "share_p05": _r(self.share_p05),
            "share_p50": _r(self.share_p50),
            "share_p95": _r(self.share_p95),
            "mean_votes": _r(self.mean_votes, 2),
            "expected_share": _r(self.expected_share),
        }


@dataclass
class RaceForecast:
    """Monte Carlo summary of one race (``derived``: the national PRES race = sum of provinces;
    its ``win_probability`` is the probability of an outright Electoral College majority)."""

    key: str
    race_type: str
    lines: list[LineForecast]
    province_code: str | None = None
    district_code: str | None = None
    municipality_code: str | None = None
    electoral_votes: int | None = None
    derived: bool = False

    @property
    def favourite(self) -> LineForecast:
        return max(self.lines, key=lambda ln: ln.win_probability)

    def party_win_probability(self) -> dict[str, float]:
        """Win probability per party code (independents under ``"independent"``)."""
        from app.elections.seats import INDEPENDENT

        out: dict[str, float] = {}
        for ln in self.lines:
            k = ln.party_code or INDEPENDENT
            out[k] = out.get(k, 0.0) + ln.win_probability
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "race_type": self.race_type,
            "province_code": self.province_code,
            "district_code": self.district_code,
            "municipality_code": self.municipality_code,
            "electoral_votes": self.electoral_votes,
            "derived": self.derived,
            "lines": [ln.to_dict() for ln in self.lines],
        }


@dataclass
class TicketForecast:
    """Electoral College and popular-vote summary of one presidential ticket."""

    key: str
    party_code: str | None
    label: str
    ev: Summary
    ev_histogram: list[float]  # P(EV = k) for k = 0 … total EV
    prob_majority: float  # ≥ majority (outright win)
    prob_plurality: float  # strictly the most electoral votes
    pv: Summary  # national popular-vote share
    prob_pv_plurality: float
    pv_histogram: list[tuple[float, float]]  # (bin centre, probability), non-empty bins
    expected_pv_share: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "party_code": self.party_code,
            "label": self.label,
            "ev": self.ev.to_dict(),
            "ev_histogram": [_r(p) for p in self.ev_histogram],
            "prob_majority": _r(self.prob_majority),
            "prob_plurality": _r(self.prob_plurality),
            "pv_share": self.pv.to_dict(),
            "prob_pv_plurality": _r(self.prob_pv_plurality),
            "pv_histogram": [[_r(c), _r(p)] for c, p in self.pv_histogram],
            "expected_pv_share": _r(self.expected_pv_share),
        }


@dataclass
class ProvinceForecast:
    """A province's Electoral College contest."""

    code: str
    electoral_votes: int
    win: dict[str, float]  # ticket → probability of carrying the province
    share_mean: dict[str, float]
    tipping_point: float  # probability of being the tipping-point province

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "electoral_votes": self.electoral_votes,
            "win": {k: _r(v) for k, v in self.win.items()},
            "share_mean": {k: _r(v) for k, v in self.share_mean.items()},
            "tipping_point": _r(self.tipping_point),
        }


@dataclass
class EVCombination:
    """One Electoral College map (province → carrying ticket) and how often it occurred."""

    rank: int
    frequency: float
    winners: dict[str, str]
    ev: dict[str, float]  # mean EV per ticket in draws with this map (exact for winner-take-all)

    @property
    def key(self) -> str:
        return "|".join(f"{p}:{t}" for p, t in self.winners.items())

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "frequency": _r(self.frequency),
            "winners": dict(self.winners),
            "ev": {k: _r(v, 3) for k, v in self.ev.items()},
        }


@dataclass
class PresidentialForecast:
    """Presidency: Electoral College, popular vote, provinces, municipalities, maps."""

    method: str
    total_ev: int
    majority: int
    tickets: list[TicketForecast]
    prob_contingent: float  # nobody reaches the majority
    prob_ev_tie: float  # two tickets with exactly half of the electoral votes each (87–87)
    prob_pv_ev_divergence: float
    provinces: dict[str, ProvinceForecast]
    tipping_point: dict[str, float]
    tipping_point_by_ticket: dict[str, dict[str, float]]  # conditional on that ticket's win/lead
    combinations: list[EVCombination]
    #: municipality → ticket → {mean, p05, p95, lead}
    municipalities: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)

    def ticket(self, key: str) -> TicketForecast:
        for t in self.tickets:
            if t.key == key:
                return t
        raise KeyError(key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "total_ev": self.total_ev,
            "majority": self.majority,
            "tickets": [t.to_dict() for t in self.tickets],
            "prob_contingent": _r(self.prob_contingent),
            "prob_ev_tie": _r(self.prob_ev_tie),
            "prob_pv_ev_divergence": _r(self.prob_pv_ev_divergence),
            "provinces": {k: v.to_dict() for k, v in self.provinces.items()},
            "tipping_point": {k: _r(v) for k, v in self.tipping_point.items()},
            "tipping_point_by_ticket": {
                t: {k: _r(v) for k, v in d.items()} for t, d in self.tipping_point_by_ticket.items()
            },
            "combinations": [c.to_dict() for c in self.combinations],
            "municipalities": {
                m: {t: {k: _r(v) for k, v in d.items()} for t, d in by_t.items()}
                for m, by_t in self.municipalities.items()
            },
        }


@dataclass
class SeatForecast:
    """Seat (or win-count) distribution of one party."""

    key: str
    seats: Summary
    histogram: list[float]  # P(seats = k), k = 0 … chamber size
    prob_majority: float | None
    prob_plurality: float
    holdover: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "seats": self.seats.to_dict(),
            "histogram": [_r(p) for p in self.histogram],
            "prob_majority": _r(self.prob_majority),
            "prob_plurality": _r(self.prob_plurality),
            "holdover": self.holdover,
        }


@dataclass
class ChamberForecast:
    """House / Senate composition (``majority`` seats for control) or governorships (no majority)."""

    chamber: str
    seats_total: int
    seats_up: int
    majority: int | None
    parties: dict[str, SeatForecast]
    prob_no_majority: float | None
    race_win: dict[str, dict[str, float]]  # race → party → probability
    compositions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chamber": self.chamber,
            "seats_total": self.seats_total,
            "seats_up": self.seats_up,
            "majority": self.majority,
            "parties": {k: v.to_dict() for k, v in self.parties.items()},
            "prob_no_majority": _r(self.prob_no_majority),
            "race_win": {r: {k: _r(v) for k, v in d.items()} for r, d in self.race_win.items()},
            "compositions": [
                {"seats": dict(c["seats"]), "frequency": _r(c["frequency"])} for c in self.compositions
            ],
        }


@dataclass
class ForecastDraws:
    """Per-draw outcomes (optional; kept in memory only, never serialised)."""

    tickets: list[str]
    provinces: list[str]
    ev: np.ndarray | None = None  # (N, T) int16
    pv_share: np.ndarray | None = None  # (N, T) float32
    province_winner: np.ndarray | None = None  # (N, Pv) ticket index
    winner: np.ndarray | None = None  # (N,) outright winner ticket index, −1 = contingent
    tipping_point: np.ndarray | None = None  # (N,) province index
    house_keys: list[str] = field(default_factory=list)
    house: np.ndarray | None = None  # (N, K)
    senate_keys: list[str] = field(default_factory=list)
    senate: np.ndarray | None = None  # (N, K) incl. holdovers
    governor_keys: list[str] = field(default_factory=list)
    governors: np.ndarray | None = None  # (N, K)
    turnout: np.ndarray | None = None  # (N,)


@dataclass
class ForecastResult:
    """Complete Monte Carlo forecast (JSON-serialisable through :meth:`to_dict`)."""

    seed: int
    n_simulations: int
    metadata: dict[str, Any]
    races: dict[str, RaceForecast]
    turnout: Summary
    president: PresidentialForecast | None = None
    house: ChamberForecast | None = None
    senate: ChamberForecast | None = None
    governors: ChamberForecast | None = None
    runtime: dict[str, Any] = field(default_factory=dict)
    draws: ForecastDraws | None = None
    data_category: str = DataCategory.SIMULATED.value
    disclaimer: str = DISCLAIMER

    @property
    def config_hash(self) -> str:
        return str(self.metadata.get("config_hash", ""))

    def to_dict(self, *, include_runtime: bool = True, include_municipalities: bool = True) -> dict[str, Any]:
        """JSON-serialisable representation (floats rounded to 6 decimals; per-draw arrays excluded)."""
        pres = None
        if self.president is not None:
            pres = self.president.to_dict()
            if not include_municipalities:
                pres.pop("municipalities", None)
        d: dict[str, Any] = {
            "data_category": self.data_category,
            "disclaimer": self.disclaimer,
            "seed": self.seed,
            "n_simulations": self.n_simulations,
            "metadata": self.metadata,
            "turnout": self.turnout.to_dict(),
            "president": pres,
            "house": None if self.house is None else self.house.to_dict(),
            "senate": None if self.senate is None else self.senate.to_dict(),
            "governors": None if self.governors is None else self.governors.to_dict(),
            "races": {k: r.to_dict() for k, r in self.races.items()},
        }
        if include_runtime:
            d["runtime"] = self.runtime
        return d

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(**kwargs), separators=(",", ":"), default=str)

    def fingerprint(self) -> str:
        """Hash of the outcome (everything except runtime information such as timings/workers)."""
        return config_hash(json.dumps(self.to_dict(include_runtime=False), sort_keys=True, default=str))


# --------------------------------------------------------------------------- reduction
def hist_quantiles(hist: np.ndarray, qs: tuple[float, ...]) -> np.ndarray:
    """Quantiles (…, len(qs)) of distributions given as histograms (…, B) over [0, 1]."""
    hist = np.asarray(hist, dtype=np.float64)
    B = hist.shape[-1]
    cdf = np.cumsum(hist, axis=-1)
    total = cdf[..., -1:]
    out = np.zeros((*hist.shape[:-1], len(qs)))
    for k, q in enumerate(qs):
        target = q * total
        idx = np.minimum((cdf < target).sum(axis=-1, keepdims=True), B - 1)
        prev = np.where(idx > 0, np.take_along_axis(cdf, np.maximum(idx - 1, 0), axis=-1), 0.0)
        cnt = np.take_along_axis(hist, idx, axis=-1)
        within = np.where(cnt > 0, (target - prev) / np.maximum(cnt, 1e-300), 0.5)
        out[..., k] = ((idx + np.clip(within, 0.0, 1.0)) / B)[..., 0]
    return np.where(total > 0, out, 0.0)


def _prob_hist(values: np.ndarray, size: int) -> list[float]:
    n = max(len(values), 1)
    return (np.bincount(np.asarray(values, dtype=np.int64), minlength=size)[:size] / n).tolist()


def _seat_forecasts(
    seats: np.ndarray, keys: list[str], total: int, majority: int | None, holdover: np.ndarray
) -> dict[str, SeatForecast]:
    N = max(seats.shape[0], 1)
    top = seats.max(axis=1) if seats.size else np.zeros(0)
    unique_top = (seats == top[:, None]).sum(axis=1) == 1 if seats.size else np.zeros(0, dtype=bool)
    out = {}
    for k, key in enumerate(keys):
        s = seats[:, k].astype(np.int64)
        out[key] = SeatForecast(
            key=key,
            seats=Summary.of(s),
            histogram=_prob_hist(s, total + 1),
            prob_majority=None if majority is None else float((s >= majority).sum() / N),
            prob_plurality=float(((s == top) & unique_top).sum() / N),
            holdover=int(holdover[k]),
        )
    return out


def _chamber(
    plan: ForecastPlan, chamber: ChamberPlan, seats: np.ndarray, races: dict[str, RaceForecast], top_k: int
) -> ChamberForecast:
    N = max(seats.shape[0], 1)
    total = chamber.seats_total
    race_win = {
        plan.races[int(i)].key: races[plan.races[int(i)].key].party_win_probability() for i in chamber.races
    }
    no_majority = None
    if chamber.majority is not None:
        no_majority = float((seats.max(axis=1) < chamber.majority).sum() / N) if seats.size else 1.0
    comps: list[dict[str, Any]] = []
    if top_k and seats.size:
        rows, counts = np.unique(seats, axis=0, return_counts=True)
        order = sorted(range(len(counts)), key=lambda i: (-int(counts[i]), tuple(-int(x) for x in rows[i])))
        for i in order[:top_k]:
            comps.append(
                {
                    "seats": {k: int(v) for k, v in zip(chamber.keys, rows[i], strict=True) if v > 0},
                    "frequency": float(counts[i] / N),
                }
            )
    return ChamberForecast(
        chamber=chamber.name,
        seats_total=total,
        seats_up=chamber.seats_up,
        majority=chamber.majority,
        parties=_seat_forecasts(seats, chamber.keys, total, chamber.majority, chamber.holdover),
        prob_no_majority=no_majority,
        race_win=race_win,
        compositions=comps,
    )


def _pv_histogram(x: np.ndarray, width: float) -> list[tuple[float, float]]:
    idx = np.floor(np.asarray(x, dtype=np.float64) / width).astype(np.int64)
    vals, counts = np.unique(idx, return_counts=True)
    n = max(len(x), 1)
    return [((int(v) + 0.5) * width, float(c / n)) for v, c in zip(vals, counts, strict=True)]


def build_result(
    plan: ForecastPlan, merged: MergedDraws, *, seed: int, config: ForecastConfig, keep_draws: bool = True
) -> ForecastResult:
    """Summarise merged draws into a :class:`ForecastResult`."""
    N = merged.n
    bins = plan.share_bins
    races: dict[str, RaceForecast] = {}
    # ------------------------------------------------------------------ simulated races
    for lp in plan.layers:
        li = lp.index
        wins = merged.counts[f"wins:{li}"] / N
        share = merged.sums[f"share:{li}"] / N
        votes = merged.sums[f"votes:{li}"] / N
        q = hist_quantiles(merged.counts[f"hist:{li}"].reshape(lp.n_races, lp.L, bins), (0.05, 0.5, 0.95))
        for j, ri in enumerate(lp.races):
            info = plan.races[int(ri)]
            lines = []
            for k, lk in enumerate(info.line_keys):
                lines.append(
                    LineForecast(
                        key=lk,
                        party_code=info.line_parties[k],
                        label=info.line_labels[k],
                        win_probability=float(wins[j, k]),
                        share_mean=float(share[j, k]),
                        share_p05=float(q[j, k, 0]),
                        share_p50=float(q[j, k, 1]),
                        share_p95=float(q[j, k, 2]),
                        mean_votes=float(votes[j, k]),
                        expected_share=float(info.expected_shares[k]),
                    )
                )
            races[info.key] = RaceForecast(
                key=info.key,
                race_type=info.race_type,
                lines=lines,
                province_code=info.province_code,
                district_code=info.district_code,
                municipality_code=info.municipality_code,
                electoral_votes=info.electoral_votes,
            )
    draws = merged.draws
    kept = ForecastDraws(tickets=[], provinces=[])
    president = None
    if plan.president is not None:
        president, parent = _presidential(plan, merged, races, config)
        if parent is not None:
            races[parent.key] = parent
        if keep_draws:
            kept.tickets = list(plan.president.tickets)
            kept.provinces = list(plan.president.province_codes)
            kept.ev, kept.pv_share = draws["ev"], draws["pv_share"]
            kept.province_winner, kept.winner = draws["province_winner"], draws["winner"]
            kept.tipping_point = draws["tipping_point"]
    # ------------------------------------------------------------------ chambers
    chambers: dict[str, ChamberForecast | None] = {"house": None, "senate": None, "governors": None}
    for name, top_k in (("house", 0), ("senate", config.outputs.top_compositions), ("governors", 0)):
        cp = getattr(plan, name)
        if cp is None:
            continue
        seats = draws[name].astype(np.int64)
        chambers[name] = _chamber(plan, cp, seats, races, top_k)
        if keep_draws:
            setattr(
                kept,
                {"house": "house_keys", "senate": "senate_keys", "governors": "governor_keys"}[name],
                list(cp.keys),
            )
            setattr(kept, name, draws[name])
    house, senate, governors = chambers["house"], chambers["senate"], chambers["governors"]
    kept.turnout = draws["turnout"] if keep_draws else None
    turnout_mean = float(merged.sums["turnout"][0] / N)
    turnout = Summary.of(draws["turnout"])
    turnout.mean = turnout_mean
    metadata = dict(plan.metadata)
    metadata.update(
        {
            "seed": seed,
            "n_simulations": N,
            "block_size": plan.block_size,
            "n_blocks": merged.n_blocks,
            "share_bins": bins,
            "data_category": DataCategory.SIMULATED.value,
        }
    )
    ordered = {k: races[k] for k in _race_order(plan) if k in races}
    return ForecastResult(
        seed=seed,
        n_simulations=N,
        metadata=metadata,
        races=ordered,
        turnout=turnout,
        president=president,
        house=house,
        senate=senate,
        governors=governors,
        draws=kept if keep_draws else None,
    )


def _presidential(
    plan: ForecastPlan, merged: MergedDraws, races: dict[str, RaceForecast], config: ForecastConfig
) -> tuple[PresidentialForecast, RaceForecast | None]:
    """Presidency summary and the derived national race (``None`` without a ``PRES`` race)."""
    pres = plan.president
    assert pres is not None
    N = merged.n
    bins = plan.share_bins
    draws = merged.draws
    T, Pv = len(pres.tickets), len(pres.province_codes)
    ev = draws["ev"].astype(np.int64)
    pv = draws["pv_share"].astype(np.float64)
    winner = draws["winner"].astype(np.int64)
    leader = draws["leader"].astype(np.int64)
    pv_winner = draws["pv_winner"].astype(np.int64)
    prov_w = draws["province_winner"].astype(np.int64)
    tipping = draws["tipping_point"].astype(np.int64)
    tp_ticket = draws["tipping_ticket"].astype(np.int64)
    tickets = [
        TicketForecast(
            key=key,
            party_code=pres.ticket_parties[t],
            label=pres.ticket_labels[t],
            ev=Summary.of(ev[:, t]),
            ev_histogram=_prob_hist(ev[:, t], pres.total_ev + 1),
            prob_majority=float((winner == t).sum() / N),
            prob_plurality=float((leader == t).sum() / N),
            pv=Summary.of(pv[:, t]),
            prob_pv_plurality=float((pv_winner == t).sum() / N),
            pv_histogram=_pv_histogram(pv[:, t], config.outputs.pv_bin_width),
            expected_pv_share=float(pres.expected_pv[t]),
        )
        for t, key in enumerate(pres.tickets)
    ]
    srt = np.sort(ev, axis=1)
    if T >= 2:
        ev_tie = (srt[:, -1] == srt[:, -2]) & (2 * srt[:, -1] == pres.total_ev)
    else:
        ev_tie = np.zeros(N, dtype=bool)
    tp_freq = np.bincount(tipping, minlength=Pv) / N
    provinces = {}
    for p, code in enumerate(pres.province_codes):
        rf = races[plan.races[int(pres.races[p])].key]
        smean = {pres.tickets[pres.line_ticket[p, j]]: ln.share_mean for j, ln in enumerate(rf.lines)}
        wf = np.bincount(prov_w[:, p], minlength=T) / N
        provinces[code] = ProvinceForecast(
            code=code,
            electoral_votes=int(pres.ev[p]),
            win={pres.tickets[t]: float(wf[t]) for t in range(T)},
            share_mean={pres.tickets[t]: float(smean.get(pres.tickets[t], 0.0)) for t in range(T)},
            tipping_point=float(tp_freq[p]),
        )
    by_ticket: dict[str, dict[str, float]] = {}
    for t, key in enumerate(pres.tickets):
        mask = tp_ticket == t
        if mask.any():
            c = np.bincount(tipping[mask], minlength=Pv) / mask.sum()
            by_ticket[key] = {pres.province_codes[p]: float(c[p]) for p in range(Pv) if c[p] > 0}
    combos: list[EVCombination] = []
    top_k = config.outputs.top_combinations
    if top_k:
        rows, inverse, counts = np.unique(prov_w, axis=0, return_inverse=True, return_counts=True)
        inverse = np.asarray(inverse).reshape(-1)
        order = sorted(range(len(counts)), key=lambda i: (-int(counts[i]), tuple(int(x) for x in rows[i])))
        for rank, i in enumerate(order[:top_k], start=1):
            mean_ev = ev[inverse == i].mean(axis=0)
            combos.append(
                EVCombination(
                    rank=rank,
                    frequency=float(counts[i] / N),
                    winners={pres.province_codes[p]: pres.tickets[int(rows[i][p])] for p in range(Pv)},
                    ev={pres.tickets[t]: float(mean_ev[t]) for t in range(T) if mean_ev[t] > 0},
                )
            )
    munis: dict[str, dict[str, dict[str, float]]] = {}
    if "muni_share" in merged.sums:
        Mp = len(pres.muni_index)
        mean = merged.sums["muni_share"] / N
        mq = hist_quantiles(merged.counts["muni_hist"].reshape(Mp, T, bins), (0.05, 0.95))
        lead = merged.counts["muni_lead"] / N
        for g, m in enumerate(pres.muni_index):
            munis[plan.muni_codes[int(m)]] = {
                pres.tickets[t]: {
                    "mean": float(mean[g, t]),
                    "p05": float(mq[g, t, 0]),
                    "p95": float(mq[g, t, 1]),
                    "lead": float(lead[g, t]),
                }
                for t in range(T)
            }
    forecast = PresidentialForecast(
        method=pres.method,
        total_ev=pres.total_ev,
        majority=pres.majority,
        tickets=tickets,
        prob_contingent=float((winner < 0).sum() / N),
        prob_ev_tie=float(ev_tie.sum() / N),
        prob_pv_ev_divergence=float(draws["diverged"].sum() / N),
        provinces=provinces,
        tipping_point={pres.province_codes[p]: float(tp_freq[p]) for p in range(Pv)},
        tipping_point_by_ticket=by_ticket,
        combinations=combos,
        municipalities=munis,
    )
    parent = None
    if pres.parent is not None:
        info = plan.races[pres.parent]
        pv_votes = merged.sums["pv_votes"] / N
        q = np.percentile(pv, (5.0, 50.0, 95.0), axis=0)
        parent = RaceForecast(
            key=info.key,
            race_type=info.race_type,
            derived=True,
            lines=[
                LineForecast(
                    key=key,
                    party_code=pres.ticket_parties[t],
                    label=pres.ticket_labels[t],
                    win_probability=float((winner == t).sum() / N),
                    share_mean=float(pv[:, t].mean()),
                    share_p05=float(q[0, t]),
                    share_p50=float(q[1, t]),
                    share_p95=float(q[2, t]),
                    mean_votes=float(pv_votes[t]),
                    expected_share=float(pres.expected_pv[t]),
                )
                for t, key in enumerate(pres.tickets)
            ],
        )
    return forecast, parent


def _race_order(plan: ForecastPlan) -> list[str]:
    """Races in input order, the derived national race first among the presidential ones."""
    keys = [r.key for r in plan.races if not r.derived]
    derived = [r.key for r in plan.races if r.derived]
    return derived + keys
