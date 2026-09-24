"""Polling persistence round-trip (in-memory SQLite)."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Election,
    GeoVintage,
    Party,
    Poll,
    PollResult,
    Pollster,
    PollsterHouseEffect,
    Province,
    Race,
)
from app.polling.aggregate import aggregate_polls
from app.polling.config import PollingConfig, PollsterConfig
from app.polling.generate import generate_polls
from app.polling.service import load_pollster_configs, polls_frames, store_polls, upsert_pollsters

ELECTION_DAY = date(2028, 11, 7)
PARTY_CODES = ["PA", "SAP", "VLP", "DM"]


@pytest.fixture()
def seeded(db_session: Session):
    vintage = GeoVintage(year=2025, label="synthetic test vintage")
    db_session.add(vintage)
    db_session.flush()
    election = Election(
        year=2028,
        election_date=ELECTION_DAY,
        name="Test general election (FICTIONAL)",
        election_type="general",
        seed=1,
        vintage_id=vintage.id,
    )
    parties = [Party(code=c, name=f"Partij {c}", abbreviation=c, color="#123456") for c in PARTY_CODES]
    provinces = [
        Province(code=c, cbs_code=f"PV{20 + i}", name=f"Provincie {c}", sort_order=i)
        for i, c in enumerate(["NB", "ZH"])
    ]
    db_session.add_all([election, *parties, *provinces])
    db_session.flush()
    races = [
        Race(election_id=election.id, race_type=rt, code=code, name=code)
        for rt, code in [
            ("PRESIDENT", "PRES"),
            ("PRESIDENT_PROVINCE", "PRES-NB"),
            ("PRESIDENT_PROVINCE", "PRES-ZH"),
            ("HOUSE", "HOUSE-NB-01"),
            ("SENATE", "SEN-NB-2"),
        ]
    ]
    db_session.add_all(races)
    db_session.flush()
    return {
        "election": election,
        "party_ids": {p.code: p.id for p in parties},
        "province_ids": {p.code: p.id for p in provinces},
        "race_ids": {r.code: r.id for r in races},
    }


def _generated(config: PollingConfig):
    truth = {
        ("national_president", "NL"): {"PA": 0.4, "SAP": 0.35, "VLP": 0.15, "DM": 0.10},
        ("province_president", "NB"): {"PA": 0.45, "SAP": 0.40, "VLP": 0.15},
        ("province_president", "ZH"): {"PA": 0.38, "SAP": 0.42, "VLP": 0.20},
        ("house_district", "NB-01"): {"PA": 0.48, "SAP": 0.52},
        ("senate", "NB"): {"PA": 0.5, "SAP": 0.5},
        ("generic_house", "NL"): {"PA": 0.3, "SAP": 0.3, "VLP": 0.2, "DM": 0.2},
    }
    counts = {
        "national_president": 20,
        "province_president": 10,
        "house_district": 4,
        "senate": 3,
        "generic_house": 6,
    }
    return generate_polls(truth, config.pollsters, counts, ELECTION_DAY, 77, config=config)


def test_upsert_pollsters(db_session: Session, seeded) -> None:
    specs = [
        PollsterConfig(
            name="Polderpeil",
            rating=1.3,
            rating_label="A+",
            method="mixed",
            house_effects={"PA": 1.0, "XX": 2.0},
        ),
        PollsterConfig(name="Deltametrie", rating=1.1, house_effects={"SAP": -0.5}),
    ]
    rows = upsert_pollsters(db_session, specs, seeded["party_ids"])
    assert set(rows) == {"Polderpeil", "Deltametrie"}
    assert rows["Polderpeil"].rating_label == "A+" and rows["Polderpeil"].is_fictional
    assert len(rows["Polderpeil"].house_effects) == 1  # unknown party code skipped
    # update: new rating, replaced house effects
    specs2 = [PollsterConfig(name="Polderpeil", rating=0.9, house_effects={"SAP": 0.4, "DM": -0.2})]
    rows2 = upsert_pollsters(db_session, specs2, seeded["party_ids"])
    assert rows2["Polderpeil"].id == rows["Polderpeil"].id
    assert db_session.scalar(select(func.count()).select_from(Pollster)) == 2
    effects = {he.party_id: he.effect_pct for he in rows2["Polderpeil"].house_effects}
    ids = seeded["party_ids"]
    assert effects == {ids["SAP"]: 0.4, ids["DM"]: -0.2}
    assert db_session.scalar(select(func.count()).select_from(PollsterHouseEffect)) == 3
    loaded = {p.name: p for p in load_pollster_configs(db_session)}
    assert loaded["Polderpeil"].house_effects == {"SAP": 0.4, "DM": -0.2}
    assert loaded["Polderpeil"].rating == 0.9
    with pytest.raises(ValueError):
        upsert_pollsters(
            db_session, [PollsterConfig(name="Polderpeil"), PollsterConfig(name="Polderpeil")], ids
        )


def test_store_and_load_round_trip(db_session: Session, seeded, polling_config: PollingConfig) -> None:
    upsert_pollsters(db_session, polling_config.pollsters, seeded["party_ids"])
    generated = _generated(polling_config)
    rows = store_polls(
        db_session,
        seeded["election"].id,
        generated,
        seeded["party_ids"],
        seeded["race_ids"],
        seeded["province_ids"],
    )
    assert len(rows) == len(generated)
    assert db_session.scalar(select(func.count()).select_from(Poll)) == len(generated)
    n_results = sum(len(p.results) for p in generated)
    assert db_session.scalar(select(func.count()).select_from(PollResult)) == n_results

    by_type = {r.poll_type: r for r in rows}
    ids = seeded["race_ids"]
    assert by_type["national_president"].race_id == ids["PRES"]
    assert by_type["house_district"].race_id == ids["HOUSE-NB-01"]
    assert by_type["house_district"].district_code == "NB-01"
    assert by_type["house_district"].province_id == seeded["province_ids"]["NB"]
    assert by_type["senate"].race_id == ids["SEN-NB-2"]  # the only NB Senate race
    assert by_type["generic_house"].race_id is None and by_type["generic_house"].province_id is None
    assert all(r.is_fictional for r in rows)

    polls_df, results_df = polls_frames(db_session, seeded["election"].id)
    gen_polls, gen_results = generated.frames()
    assert len(polls_df) == len(gen_polls)
    # map DB ids back to generated ids through the insertion order
    id_map = {row.id: gp.poll_id for row, gp in zip(rows, generated, strict=True)}
    polls_df["poll_id"] = polls_df["poll_id"].map(id_map)
    results_df["poll_id"] = results_df["poll_id"].map(id_map)
    merged = polls_df.set_index("poll_id").sort_index()
    expected = gen_polls.set_index("poll_id").sort_index()
    for col in ("pollster", "poll_type", "geo_code", "sample_size", "population", "method"):
        assert merged[col].tolist() == expected[col].tolist(), col
    for col in ("start_date", "end_date"):
        assert (merged[col] == expected[col]).all()
    assert merged["undecided_pct"].tolist() == pytest.approx(expected["undecided_pct"].tolist())
    assert merged["margin_of_error"].tolist() == pytest.approx(expected["margin_of_error"].tolist())
    assert merged["pollster_rating"].notna().all()
    pd.testing.assert_frame_equal(
        results_df.sort_values(["poll_id", "key"]).reset_index(drop=True),
        gen_results.sort_values(["poll_id", "key"]).reset_index(drop=True),
        check_dtype=False,
    )

    # aggregating DB frames gives the same averages as aggregating the generated frames
    as_of = date(2028, 11, 6)
    from_db = aggregate_polls(polls_df, results_df, polling_config, as_of)
    direct = aggregate_polls(gen_polls, gen_results, polling_config, as_of)
    assert set(from_db) == set(direct)
    for key, avg in direct.items():
        assert from_db[key].mean == pytest.approx(avg.mean)

    only_house, only_house_results = polls_frames(
        db_session, seeded["election"].id, poll_type="house_district"
    )
    assert set(only_house["poll_type"]) == {"house_district"}
    assert set(only_house_results["poll_id"]) <= set(only_house["poll_id"])


def test_store_polls_creates_unknown_pollsters_and_handles_empty(
    db_session: Session, seeded, polling_config
) -> None:
    assert store_polls(db_session, seeded["election"].id, [], seeded["party_ids"]) == []
    generated = _generated(polling_config)
    rows = store_polls(db_session, seeded["election"].id, generated[:5], seeded["party_ids"])
    assert len(rows) == 5
    created = {p.name for p in db_session.scalars(select(Pollster))}
    assert {p.pollster for p in generated[:5]} <= created
    assert all(r.race_id is None for r in rows)  # no race map given


def test_polls_frames_empty(db_session: Session, seeded) -> None:
    polls_df, results_df = polls_frames(db_session, seeded["election"].id)
    assert polls_df.empty and results_df.empty
    assert "pollster_rating" in polls_df.columns and list(results_df.columns) == [
        "poll_id",
        "key",
        "value_pct",
    ]
