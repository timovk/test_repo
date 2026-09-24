"""Hand-built standard results frames for the analytics tests (all numbers FICTIONAL/SIMULATED)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd
import pytest

from app.analytics.results import RESULTS_COLUMNS, build_results_frame
from app.core.constitution import ELECTORAL_VOTES
from app.core.rng import make_rng
from app.geography.frame import GeographyFrame

Line = tuple[str, str | None, int]  # (line_key, party_code, votes)

#: Canonical-shaped Electoral College map (House seats + 2 per province).
CANONICAL_EV: dict[str, int] = {
    "GR": 7,
    "FR": 8,
    "DR": 6,
    "OV": 12,
    "FL": 6,
    "GE": 20,
    "UT": 14,
    "NH": 27,
    "ZH": 34,
    "ZE": 5,
    "NB": 24,
    "LI": 11,
}
assert sum(CANONICAL_EV.values()) == ELECTORAL_VOTES


def contest_rows(
    election_id: int,
    year: int,
    race_code: str,
    race_type: str,
    level: str,
    geo_code: str,
    lines: Sequence[Line],
    *,
    province_code: str | None = None,
    geo_name: str | None = None,
    eligible: int | None = None,
    extra_ballots: int = 0,
    winner: str | None = None,
) -> list[dict]:
    """Rows of one contest-geo.  ``winner`` flags a line explicitly (e.g. a tie decided by lot)."""
    valid = sum(v for _, _, v in lines)
    ballots = valid + extra_ballots
    rows = []
    for key, party, votes in lines:
        row = {
            "election_id": election_id,
            "year": year,
            "race_code": race_code,
            "race_type": race_type,
            "level": level,
            "geo_code": geo_code,
            "geo_name": geo_name or geo_code,
            "province_code": province_code,
            "line_key": key,
            "candidate": f"Cand {key}",
            "party_code": party,
            "votes": votes,
            "valid_votes": valid,
            "ballots_cast": ballots,
            "eligible": eligible if eligible is not None else ballots,
        }
        if winner is not None:
            row["winner"] = key == winner
        rows.append(row)
    return rows


def house_frame(
    election_id: int, year: int, districts: Mapping[str, Sequence[Line]], **kw: object
) -> pd.DataFrame:
    """House races at district level: ``{"NB-01": [(key, party, votes), …]}``."""
    rows: list[dict] = []
    for code, lines in districts.items():
        rows += contest_rows(
            election_id, year, f"HOUSE-{code}", "HOUSE", "district", code, lines, province_code=code[:2], **kw
        )
    return build_results_frame(rows)


def pres_frame(
    election_id: int,
    year: int,
    provinces: Mapping[str, Mapping[str, int]],
    municipalities: Mapping[str, tuple[str, Mapping[str, int]]] | None = None,
    *,
    winners: Mapping[str, str] | None = None,
) -> pd.DataFrame:
    """A presidential election: ``PRES`` national row set (sum of provinces), ``PRES-<PV>``
    province contests and optional municipality rows ``{GM: (province, {party: votes})}``.
    Tickets are keyed ``t-<party>``."""
    winners = dict(winners or {})
    rows: list[dict] = []
    national: dict[str, int] = {}
    for prov, votes in provinces.items():
        lines = [(f"t-{p}", p, v) for p, v in votes.items()]
        for p, v in votes.items():
            national[p] = national.get(p, 0) + v
        w = winners.get(prov)
        rows += contest_rows(
            election_id,
            year,
            f"PRES-{prov}",
            "PRESIDENT_PROVINCE",
            "province",
            prov,
            lines,
            province_code=prov,
            winner=None if w is None else f"t-{w}",
        )
    for gm, (prov, votes) in (municipalities or {}).items():
        lines = [(f"t-{p}", p, v) for p, v in votes.items()]
        rows += contest_rows(
            election_id,
            year,
            f"PRES-{prov}",
            "PRESIDENT_PROVINCE",
            "municipality",
            gm,
            lines,
            province_code=prov,
        )
    rows += contest_rows(
        election_id,
        year,
        "PRES",
        "PRESIDENT",
        "national",
        "NL",
        [(f"t-{p}", p, v) for p, v in national.items()],
    )
    return build_results_frame(rows)


@pytest.fixture()
def house_prev() -> pd.DataFrame:
    """2028 House: three districts, three parties."""
    return house_frame(
        1,
        2028,
        {
            "NB-01": [("a1", "A", 60), ("b1", "B", 40)],
            "NB-02": [("a2", "A", 45), ("b2", "B", 55)],
            "UT-01": [("a3", "A", 30), ("b3", "B", 30), ("c3", "C", 40)],
        },
    )


@pytest.fixture()
def house_curr() -> pd.DataFrame:
    """2030 House: a flip, a hold, a party dropping out, an independent, and a new district."""
    return house_frame(
        2,
        2030,
        {
            "NB-01": [("a1", "A", 48), ("b1", "B", 52)],
            "NB-02": [("a2", "A", 40), ("b2", "B", 60)],
            "UT-01": [("a3", "A", 50), ("c3", "C", 45), ("i3", None, 5)],
            "UT-02": [("a4", "A", 10)],
        },
    )


def synthetic_pres_frame(
    geo: GeographyFrame,
    seed: int,
    election_id: int,
    year: int,
    parties: Sequence[str] = ("A", "B", "C", "D"),
    tilt: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    """A vectorised SIMULATED presidential election on a geography frame with rows at unit,
    municipality, province (``PRES-<PV>`` contests) and national (``PRES``) level."""
    rng = make_rng(seed, "analytics-test", "pres", election_id)
    K = len(parties)
    U, P = geo.n_units, geo.n_provinces
    logits = rng.normal(0.0, 0.6, size=(P, K))[geo.unit_province] + rng.normal(0.0, 0.3, size=(U, K))
    if tilt:
        logits = logits + np.array([tilt.get(p, 0.0) for p in parties])[None, :]
    pvals = np.exp(logits)
    pvals /= pvals.sum(axis=1, keepdims=True)
    eligible = geo.unit_eligible.astype(np.int64)
    ballots = np.rint(eligible * rng.uniform(0.65, 0.88, size=U)).astype(np.int64)
    invalid = np.minimum(rng.binomial(ballots, 0.006), ballots)
    votes = rng.multinomial(ballots - invalid, pvals)  # (U, K)

    def level_rows(level, codes, names, prov_codes, race_codes, v, b, e) -> dict[str, np.ndarray]:
        n = len(codes)
        valid = v.sum(axis=1)
        share = np.divide(v, valid[:, None], out=np.zeros_like(v, dtype=float), where=valid[:, None] > 0)
        top = np.argmax(v, axis=1)
        win = (np.arange(K)[None, :] == top[:, None]) & (valid[:, None] > 0)
        return {
            "race_code": np.repeat(race_codes, K),
            "race_type": np.repeat(
                np.where(np.asarray(race_codes) == "PRES", "PRESIDENT", "PRESIDENT_PROVINCE"), K
            ),
            "level": np.full(n * K, level),
            "geo_code": np.repeat(codes, K),
            "geo_name": np.repeat(names, K),
            "province_code": np.repeat(prov_codes, K),
            "line_key": np.tile([f"t-{p}" for p in parties], n),
            "candidate": np.tile([f"Ticket {p}" for p in parties], n),
            "party_code": np.tile(list(parties), n),
            "votes": v.ravel(),
            "share": share.ravel(),
            "valid_votes": np.repeat(valid, K),
            "eligible": np.repeat(e, K),
            "ballots_cast": np.repeat(b, K),
            "winner": win.ravel(),
        }

    pc = np.asarray(geo.province_codes, dtype=object)
    parts = [
        level_rows(
            "unit",
            geo.unit_codes,
            geo.unit_names,
            pc[geo.unit_province],
            np.asarray([f"PRES-{p}" for p in pc[geo.unit_province]]),
            votes,
            ballots,
            eligible,
        ),
        level_rows(
            "municipality",
            geo.muni_codes,
            geo.muni_names,
            pc[geo.muni_province],
            np.asarray([f"PRES-{p}" for p in pc[geo.muni_province]]),
            geo.to_munis(votes),
            geo.to_munis(ballots),
            geo.to_munis(eligible),
        ),
        level_rows(
            "province",
            geo.province_codes,
            geo.province_names,
            pc,
            np.asarray([f"PRES-{p}" for p in pc]),
            geo.to_provinces(votes),
            geo.to_provinces(ballots),
            geo.to_provinces(eligible),
        ),
        level_rows(
            "national",
            ["NL"],
            ["Nederland"],
            np.asarray([None], dtype=object),
            np.asarray(["PRES"]),
            votes.sum(axis=0, keepdims=True),
            ballots.sum(keepdims=True),
            eligible.sum(keepdims=True),
        ),
    ]
    df = pd.concat([pd.DataFrame(p) for p in parts], ignore_index=True)
    df.insert(0, "year", year)
    df.insert(0, "election_id", election_id)
    df["province_code"] = df["province_code"].astype(object)
    return df[list(RESULTS_COLUMNS)]


@pytest.fixture(scope="session")
def make_rows():  # type: ignore[no-untyped-def]
    return contest_rows


@pytest.fixture(scope="session")
def make_house():  # type: ignore[no-untyped-def]
    return house_frame


@pytest.fixture(scope="session")
def make_pres():  # type: ignore[no-untyped-def]
    return pres_frame


@pytest.fixture(scope="session")
def make_synthetic_pres():  # type: ignore[no-untyped-def]
    return synthetic_pres_frame


@pytest.fixture()
def canonical_ev() -> dict[str, int]:
    return dict(CANONICAL_EV)


# --------------------------------------------------------------------------- engine-built frames
def synthetic_unit_districts(geo: GeographyFrame, seats: Mapping[str, int]) -> np.ndarray:
    """Toy House plan: ``seats[pv]`` districts per province (``NB-01`` …), cut by unit x order."""
    out = np.empty(geo.n_units, dtype=object)
    for p, code in enumerate(geo.province_codes):
        idx = geo.units_in_province(p)
        n = max(1, min(int(seats[code]), len(idx)))
        order = np.argsort(geo.unit_xy[idx, 0], kind="stable")
        labels = np.empty(len(idx), dtype=object)
        labels[order] = [f"{code}-{1 + (i * n) // len(idx):02d}" for i in range(len(idx))]
        out[idx] = labels
    return out


def engine_election(
    geo: GeographyFrame,
    seed: int,
    election_id: int,
    year: int,
    *,
    parties: Sequence[str] = ("A", "B", "C", "D"),
    tilt: Mapping[str, float] | None = None,
    levels: Sequence[str] = ("unit", "municipality", "district", "province", "national"),
    local: bool = True,
) -> pd.DataFrame:
    """A SIMULATED general election laid out exactly as services store results: **every race at
    every level** (unit → national; presidential and House races also by district), aggregated
    by the engine (:func:`app.analytics.results.results_frame_from_draw`).

    Races: ``PRES`` (stitched from the province contests, as services do), ``PRES-<PV>``, one
    ``HOUSE`` race per district of :data:`CANONICAL_EV` − 2 seats per province (every 7th district
    has an independent, every 21st two), ``SEN-<PV>-1`` in four provinces and ``GOV-<PV>``; with
    ``local`` also ``MAYOR``/``COUNCIL`` (proportional lists) in three municipalities and
    ``PROVLEG-UT``.
    """
    from app.analytics.results import results_frame_from_draw
    from app.core.constitution import ElectoralSystem, RaceType
    from app.elections.types import BallotLine, ElectionDraw, RaceSpec, RaceVotes, UnitTurnout

    rng = make_rng(seed, "analytics-test", "engine", election_id)
    U, K = geo.n_units, len(parties)
    logits = rng.normal(0.0, 0.5, size=(geo.n_provinces, K))[geo.unit_province]
    logits = logits + rng.normal(0.0, 0.3, size=(U, K))
    if tilt:
        logits = logits + np.array([tilt.get(p, 0.0) for p in parties])[None, :]
    probs = np.exp(logits)
    probs /= probs.sum(axis=1, keepdims=True)
    eligible = geo.unit_eligible.astype(np.int64)
    ballots = np.rint(eligible * rng.uniform(0.6, 0.85, size=U)).astype(np.int64)
    unit_district = synthetic_unit_districts(geo, {p: ev - 2 for p, ev in CANONICAL_EV.items()})
    specs: dict[str, RaceSpec] = {}
    draws: dict[str, RaceVotes] = {}

    def add(code: str, rtype: RaceType, units: np.ndarray, lines: list[BallotLine], p: np.ndarray) -> None:
        b = ballots[units]
        invalid = rng.binomial(b, 0.004)
        blank = rng.binomial(b - invalid, 0.003)
        votes = rng.multinomial(b - invalid - blank, p)
        system = (
            ElectoralSystem.PROPORTIONAL_DHONDT
            if rtype
            in (
                RaceType.MUNICIPAL_COUNCIL,
                RaceType.PROVINCIAL_LEGISLATURE,
            )
            else ElectoralSystem.FPTP
        )
        specs[code] = RaceSpec(code, rtype, units, lines, electoral_system=system)
        draws[code] = RaceVotes(
            code, [ln.key for ln in lines], units, votes, b, blank, invalid, eligible[units]
        )

    tickets = [BallotLine(f"t-{p}", p, label=f"Ticket {p}") for p in parties]
    pres_votes: dict[str, RaceVotes] = {}
    for pi, pv in enumerate(geo.province_codes):
        units = geo.units_in_province(pi)
        add(f"PRES-{pv}", RaceType.PRESIDENT_PROVINCE, units, tickets, probs[units])
        pres_votes[pv] = draws[f"PRES-{pv}"]
    all_units = np.arange(U)
    parent = np.zeros((U, K), dtype=np.int64)
    blank = np.zeros(U, dtype=np.int64)
    invalid = np.zeros(U, dtype=np.int64)
    for rv in pres_votes.values():
        parent[rv.unit_index] = rv.votes
        blank[rv.unit_index] = rv.blank
        invalid[rv.unit_index] = rv.invalid
    specs["PRES"] = RaceSpec("PRES", RaceType.PRESIDENT, all_units, tickets)
    draws["PRES"] = RaceVotes(
        "PRES", [t.key for t in tickets], all_units, parent, ballots, blank, invalid, eligible
    )
    for n, d in enumerate(sorted(set(unit_district.tolist()))):
        units = np.flatnonzero(unit_district == d)
        lines = [BallotLine(f"{d}-{p}", p, label=f"Cand {d} {p}") for p in parties]
        p = probs[units]
        n_ind = 2 if n % 21 == 0 else (1 if n % 7 == 0 else 0)
        for i in range(n_ind):
            lines.append(BallotLine(f"{d}-ind{i}", None, label=f"Independent {d} {i}"))
            p = np.hstack([p * 0.8, np.full((len(units), 1), 0.2)])
            p /= p.sum(axis=1, keepdims=True)
        add(f"HOUSE-{d}", RaceType.HOUSE, units, lines, p)
    for pi, pv in enumerate(geo.province_codes):
        units = geo.units_in_province(pi)
        if pi % 3 == 0:
            add(
                f"SEN-{pv}-1",
                RaceType.SENATE,
                units,
                [BallotLine(f"s{pv}-{p}", p) for p in parties],
                probs[units],
            )
        add(
            f"GOV-{pv}",
            RaceType.GOVERNOR,
            units,
            [BallotLine(f"g{pv}-{p}", p) for p in parties],
            probs[units],
        )
    if local:
        for m in (0, 1, geo.n_munis - 1):
            units = geo.units_in_muni(m)
            gm = geo.muni_codes[m]
            mayor = [BallotLine(f"m{gm}-{p}", p) for p in parties[:2]] + [BallotLine(f"m{gm}-ind", None)]
            add(f"MAYOR-{gm}", RaceType.MAYOR, units, mayor, np.full((len(units), 3), 1 / 3))
            add(
                f"COUNCIL-{gm}",
                RaceType.MUNICIPAL_COUNCIL,
                units,
                [BallotLine(p, p) for p in parties],
                probs[units],
            )
        ut = geo.units_in_province(geo.province_index("UT"))
        add("PROVLEG-UT", RaceType.PROVINCIAL_LEGISLATURE, ut, [BallotLine(p, p) for p in parties], probs[ut])
    draw = ElectionDraw(seed, UnitTurnout(eligible, ballots), races=draws)
    return results_frame_from_draw(
        draw, specs, geo, election_id=election_id, year=year, unit_district=unit_district, levels=levels
    )


@pytest.fixture(scope="session")
def make_engine_election():  # type: ignore[no-untyped-def]
    return engine_election
