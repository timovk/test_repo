"""End-to-end on the REAL CBS geography: setup (incl. districting) → demo-2028 → final.

Runs into a temporary SQLite file created through the Alembic migrations.  Timing targets:
setup < 90 s, create + simulate < 60 s, finalize < 30 s.
"""

from __future__ import annotations

import time
from collections import Counter

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.core.constitution import ELECTORAL_VOTES, HOUSE_SEATS
from app.models import ElectoralVoteAllocation, GeoUnit, Municipality, Race
from app.services.bootstrap import init_db, setup_system
from app.services.elections import create_election, finalize_election, simulate_election
from app.services.results import results_frame
from app.services.validation import validate_system

pytestmark = pytest.mark.realdata


@pytest.fixture(scope="module")
def real_db(tmp_path_factory, real_frame):  # type: ignore[no-untyped-def]
    from app.db.session import get_engine, reset_engines

    url = f"sqlite:///{tmp_path_factory.mktemp('real') / 'real.db'}"
    init_db(url)
    engine = get_engine(url)
    yield sessionmaker(bind=engine, future=True, expire_on_commit=False)
    reset_engines()


def test_real_end_to_end(real_db, real_frame) -> None:  # type: ignore[no-untyped-def]
    timings: dict[str, float] = {}
    s = real_db()
    t = time.perf_counter()
    report = setup_system(s, district_seed=2028, workers=0)
    s.commit()
    timings["setup"] = time.perf_counter() - t
    assert report.provinces == 12 and report.districts == HOUSE_SEATS and report.senate_seats == 24
    assert report.municipalities == real_frame.n_munis and report.units == real_frame.n_units
    assert s.scalar(select(func.count()).select_from(Municipality)) == real_frame.n_munis
    assert s.scalar(select(func.count()).select_from(GeoUnit)) == real_frame.n_units

    t = time.perf_counter()
    el = create_election(s, "demo-2028")
    simulate_election(s, el.id)
    s.commit()
    timings["create+simulate"] = time.perf_counter() - t
    t = time.perf_counter()
    finalize_election(s, el.id)
    s.commit()
    timings["finalize"] = time.perf_counter() - t

    counts = Counter(t for (t,) in s.execute(select(Race.race_type).where(Race.election_id == el.id)))
    assert counts["HOUSE"] == HOUSE_SEATS and counts["PRESIDENT_PROVINCE"] == 12 and counts["SENATE"] == 8
    pres = s.scalar(select(Race).where(Race.election_id == el.id, Race.code == "PRES"))
    ev = s.scalar(
        select(func.sum(ElectoralVoteAllocation.electoral_votes)).where(
            ElectoralVoteAllocation.race_id == pres.id
        )
    )
    assert ev == ELECTORAL_VOTES
    rep = validate_system(s)
    assert rep.ok, rep.errors
    df = results_frame(s, levels=("municipality",), race_types=["PRESIDENT"])
    assert df["geo_code"].nunique() == real_frame.n_munis
    print(f"\nreal-data timings: { {k: round(v, 1) for k, v in timings.items()} }")
    assert timings["setup"] < 90.0
    assert timings["create+simulate"] < 60.0
    assert timings["finalize"] < 30.0
    # a second setup reuses everything (idempotent)
    t = time.perf_counter()
    again = setup_system(s, district_seed=2028, workers=0)
    assert again.plan_reused and again.plan_id == report.plan_id
    assert time.perf_counter() - t < 30.0
    s.close()
