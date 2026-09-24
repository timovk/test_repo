"""Fixtures for the export tests (all data FICTIONAL / SIMULATED, built by hand)."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

from app.analytics.results import build_results_frame

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


def _rows(
    eid, year, race, rtype, level, geo, lines, prov=None, name=None, eligible=None, winner=None
) -> list[dict]:
    valid = sum(v for _, _, v in lines)
    out = []
    for key, party, votes in lines:
        r = {
            "election_id": eid,
            "year": year,
            "race_code": race,
            "race_type": rtype,
            "level": level,
            "geo_code": geo,
            "geo_name": name or geo,
            "province_code": prov,
            "line_key": key,
            "candidate": f"Cand {key}",
            "party_code": party,
            "votes": votes,
            "valid_votes": valid,
            "ballots_cast": valid + 2,
            "eligible": eligible if eligible is not None else 2 * valid + 10,
        }
        if winner is not None:
            r["winner"] = key == winner
        out.append(r)
    return out


def general_election(eid: int, year: int, *, flip: bool = False) -> pd.DataFrame:
    """PRES (national + 12 province contests + 3 municipalities + 2 units), 3 House districts,
    2 Senate seats and 1 governor race."""
    rows: list[dict] = []
    nat = {"A": 0, "B": 0}
    for i, prov in enumerate(CANONICAL_EV):
        a = 40 + 3 * i + (8 if flip else 0)
        b = 100 - a
        if prov == "ZE":
            a, b = 50, 50
        nat["A"] += a
        nat["B"] += b
        rows += _rows(
            eid,
            year,
            f"PRES-{prov}",
            "PRESIDENT_PROVINCE",
            "province",
            prov,
            [("t-a", "A", a), ("t-b", "B", b)],
            prov,
            f"Provincie {prov}",
            winner="t-a" if prov == "ZE" else None,
        )
    rows += _rows(
        eid,
        year,
        "PRES",
        "PRESIDENT",
        "national",
        "NL",
        [("t-a", "A", nat["A"]), ("t-b", "B", nat["B"])],
        None,
        "Nederland",
    )
    for gm, (a, b) in {"GM0855": (30, 20), "GM0772": (25, 25), "GM0363": (10, 40)}.items():
        prov = "NB" if gm != "GM0363" else "NH"
        rows += _rows(
            eid,
            year,
            f"PRES-{prov}",
            "PRESIDENT_PROVINCE",
            "municipality",
            gm,
            [("t-a", "A", a), ("t-b", "B", b)],
            prov,
        )
    for bu, (a, b) in {"BU08550101": (7, 3), "BU08550102": (2, 9)}.items():
        rows += _rows(
            eid, year, "PRES-NB", "PRESIDENT_PROVINCE", "unit", bu, [("t-a", "A", a), ("t-b", "B", b)], "NB"
        )
    house = {
        "NB-01": [("h1a", "A", 55 if not flip else 45), ("h1b", "B", 45 if not flip else 55)],
        "NB-02": [("h2a", "A", 30), ("h2b", "B", 60), ("h2i", None, 10)],
        "UT-01": [("h3i", None, 70)],
    }
    for d, lines in house.items():
        rows += _rows(eid, year, f"HOUSE-{d}", "HOUSE", "district", d, lines, d[:2], f"District {d}")
    rows += _rows(
        eid,
        year,
        "SEN-NB-1",
        "SENATE",
        "province",
        "NB",
        [("s1a", "A", 51), ("s1b", "B", 49)],
        "NB",
        "Noord-Brabant",
    )
    rows += _rows(
        eid,
        year,
        "SEN-UT-2",
        "SENATE",
        "province",
        "UT",
        [("s2a", "A", 20), ("s2b", "B", 80)],
        "UT",
        "Utrecht",
    )
    rows += _rows(
        eid, year, "GOV-GR", "GOVERNOR", "province", "GR", [("g1", "A", 0), ("g2", "B", 0)], "GR", "Groningen"
    )
    return build_results_frame(rows)


@pytest.fixture(scope="session")
def canonical_ev() -> dict[str, int]:
    return dict(CANONICAL_EV)


@pytest.fixture()
def election_prev() -> pd.DataFrame:
    return general_election(1, 2028)


@pytest.fixture()
def election_curr() -> pd.DataFrame:
    return general_election(2, 2032, flip=True)


@pytest.fixture()
def timeline_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "election_id": [2, 2, 2],
            "seq": [2, 1, 3],
            "sim_time_s": [120.5, 60.25, 3600.0],
            "time": [
                datetime(2032, 11, 3, 21, 2, 0),
                datetime(2032, 11, 3, 21, 1, 0),
                datetime(2032, 11, 3, 22, 0, 0),
            ],
            "municipality_code": ["GM0855", "GM0772", "GM0855"],
            "municipality_name": ["Tilburg", "Eindhoven", "Tilburg"],
            "province_code": ["NB", "NB", "NB"],
            "kind": ["batch", "batch", "final"],
            "ballots_in_batch": [1000, 500, 2000],
            "cumulative_ballots": [1500, 500, 3500],
            "municipality_fraction_after": [1 / 3, 0.25, 1.0],
            "national_fraction": [0.0001, 0.00005, 0.0003],
        }
    )


@pytest.fixture()
def polls_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "poll_id": [7, 7, 8],
            "election_id": [2, 2, None],
            "pollster": ["Fictional Research Bureau", "Fictional Research Bureau", "Imaginary Panel"],
            "poll_type": ["national_president"] * 3,
            "geo_code": [None, None, None],
            "district_code": [None, None, None],
            "start_date": [date(2032, 10, 1), date(2032, 10, 1), date(2032, 10, 5)],
            "end_date": [date(2032, 10, 4), date(2032, 10, 4), date(2032, 10, 9)],
            "sample_size": [1200, 1200, 800],
            "population": ["LV", "LV", "RV"],
            "method": ["online", "online", "panel"],
            "margin_of_error": [2.8, 2.8, 3.5],
            "undecided_pct": [None, None, 4.0],
            "label": ["A", "B", "A"],
            "party_code": ["A", "B", "A"],
            "value_pct": [48.0, 45.5, 50.25],
            "is_fictional": [True, True, True],
        }
    )
