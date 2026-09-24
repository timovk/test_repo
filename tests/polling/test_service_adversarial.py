"""Adversarial persistence tests: lossless geography, pollster ratings and column limits."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Election, GeoVintage, Party, Pollster, Province
from app.polling.aggregate import aggregate_polls
from app.polling.config import PollingConfig, PollsterConfig
from app.polling.generate import generate_polls
from app.polling.service import load_pollster_configs, polls_frames, store_polls, upsert_pollsters

ELECTION_DAY = date(2028, 11, 7)


@pytest.fixture()
def minimal(db_session: Session):
    vintage = GeoVintage(year=2025, label="synthetic test vintage")
    db_session.add(vintage)
    db_session.flush()
    election = Election(
        year=2028,
        election_date=ELECTION_DAY,
        name="Adversarial test election (FICTIONAL)",
        election_type="general",
        seed=3,
        vintage_id=vintage.id,
    )
    parties = [Party(code=c, name=f"Partij {c}", abbreviation=c, color="#654321") for c in ("PA", "SAP")]
    # only NB exists in the province table; ZH polls must still round-trip
    db_session.add_all(
        [election, *parties, Province(code="NB", cbs_code="PV30", name="Noord-Brabant", sort_order=1)]
    )
    db_session.flush()
    return {"election": election, "party_ids": {p.code: p.id for p in parties}}


def _polls(config: PollingConfig):
    truth = {
        ("province_president", "NB"): {"PA": 0.5, "SAP": 0.5},
        ("province_president", "ZH"): {"PA": 0.45, "SAP": 0.55},
        ("governor", "NB"): {"PA": 0.52, "SAP": 0.48},
        ("house_district", "NB-03"): {"PA": 0.5, "SAP": 0.5},
        ("national_president", "NL"): {"PA": 0.5, "SAP": 0.5},
    }
    counts = {"province_president": 12, "governor": 4, "house_district": 4, "national_president": 6}
    return generate_polls(truth, config.pollsters, counts, ELECTION_DAY, 21, config=config)


def test_geo_codes_round_trip_without_province_map(db_session: Session, minimal, polling_config) -> None:
    generated = _polls(polling_config)
    rows = store_polls(db_session, minimal["election"].id, generated, minimal["party_ids"])
    nb_id = db_session.scalar(select(Province.id).where(Province.code == "NB"))
    by_geo = {}
    for row, poll in zip(rows, generated, strict=True):
        by_geo.setdefault(poll.geo_code, row)
    assert by_geo["NB"].province_id == nb_id and by_geo["NB"].district_code is None  # resolved from the DB
    assert by_geo["NB-03"].province_id == nb_id and by_geo["NB-03"].district_code == "NB-03"
    assert by_geo["ZH"].province_id is None and by_geo["ZH"].district_code == "ZH"  # no province row: kept
    assert by_geo["NL"].province_id is None and by_geo["NL"].district_code is None

    polls_df, _ = polls_frames(db_session, minimal["election"].id)
    id_map = {row.id: gp.poll_id for row, gp in zip(rows, generated, strict=True)}
    polls_df["poll_id"] = polls_df["poll_id"].map(id_map)
    expected = generated.frames()[0].set_index("poll_id")["geo_code"]
    assert polls_df.set_index("poll_id")["geo_code"].sort_index().tolist() == expected.sort_index().tolist()


def test_auto_created_pollsters_keep_their_ratings(db_session: Session, minimal, polling_config) -> None:
    generated = _polls(polling_config)
    store_polls(db_session, minimal["election"].id, generated, minimal["party_ids"])
    configured = {p.name: p.rating for p in polling_config.pollsters}
    for row in db_session.scalars(select(Pollster)):
        assert row.rating == pytest.approx(configured[row.name])  # not silently reset to 1.0
        assert row.is_fictional
    # the database frames (whose stored rating the aggregate prefers) therefore aggregate exactly
    # like the in-memory frames
    polls_df, results_df = polls_frames(db_session, minimal["election"].id)
    from_db = aggregate_polls(polls_df, results_df, polling_config, ELECTION_DAY)
    direct = aggregate_polls(*generated.frames(), polling_config, ELECTION_DAY)
    assert set(from_db) == set(direct)
    for key, avg in direct.items():
        assert from_db[key].mean == pytest.approx(avg.mean)
    # scenario pollsters passed explicitly win over the configuration
    scenario = [PollsterConfig(name="Toetsbureau Nieuw", rating=1.7, house_effects={"PA": 1.0})]
    extra = [replace(generated[0], pollster="Toetsbureau Nieuw", poll_id=999)]
    store_polls(db_session, minimal["election"].id, extra, minimal["party_ids"], pollsters=scenario)
    new = db_session.scalars(select(Pollster).where(Pollster.name == "Toetsbureau Nieuw")).one()
    assert new.rating == pytest.approx(1.7) and len(new.house_effects) == 1


def test_invalid_rows_are_rejected(db_session: Session, minimal, polling_config) -> None:
    poll = _polls(polling_config)[0]
    with pytest.raises(ValueError, match="population"):
        store_polls(
            db_session, minimal["election"].id, [replace(poll, population="XX")], minimal["party_ids"]
        )
    # validation happens before any side effect: the batch's pollsters were not created
    with pytest.raises(ValueError, match="sample size"):
        store_polls(
            db_session,
            minimal["election"].id,
            [replace(poll, pollster="Toetsbureau Ongeldig"), replace(poll, sample_size=0)],
            minimal["party_ids"],
        )
    assert db_session.scalars(select(Pollster)).all() == []
    with pytest.raises(ValueError, match="longer than"):
        store_polls(
            db_session,
            minimal["election"].id,
            [replace(poll, results={"K" * 121: 50.0, "PA": 40.0})],
            minimal["party_ids"],
        )
    with pytest.raises(ValueError, match="real polling"):
        store_polls(
            db_session, minimal["election"].id, [replace(poll, pollster="Ipsos I&O")], minimal["party_ids"]
        )
    with pytest.raises(ValueError, match="longer than"):
        upsert_pollsters(
            db_session,
            [PollsterConfig.model_construct(name="X" * 130, rating=1.0, method="online", house_effects={})],
            {},
        )
    # lower-case population labels are normalised, long free-text methods are shortened to fit
    rows = store_polls(
        db_session,
        minimal["election"].id,
        [replace(poll, population="lv", method="computer-assisted telephone interviewing")],
        minimal["party_ids"],
    )
    assert rows[0].population == "LV" and len(rows[0].method) <= 24


def test_stored_rating_outside_schema_is_clamped(db_session: Session, minimal) -> None:
    upsert_pollsters(
        db_session, [PollsterConfig(name="Toetsbureau Streng", rating=1.0)], minimal["party_ids"]
    )
    row = db_session.scalars(select(Pollster).where(Pollster.name == "Toetsbureau Streng")).one()
    row.rating = 9.0  # edited directly in the database
    db_session.flush()
    loaded = {p.name: p for p in load_pollster_configs(db_session)}
    assert loaded["Toetsbureau Streng"].rating == 5.0
    row.rating = 0.0  # an editor excludes the pollster
    db_session.flush()
    assert {p.name: p for p in load_pollster_configs(db_session)}["Toetsbureau Streng"].rating == 0.0
