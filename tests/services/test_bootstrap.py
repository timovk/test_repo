"""System bootstrap: idempotency, offices/seats/legislatures, frames, migrations."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import func, inspect, select

from app.core.constitution import HOUSE_SEATS, SENATE_SEATS, OfficeType
from app.elections.seats import municipal_council_size, provincial_legislature_size
from app.models import (
    Apportionment,
    Base,
    DistrictPlan,
    GeoUnit,
    GovernorSeat,
    Legislature,
    MayorSeat,
    Municipality,
    Office,
    SenateSeat,
    SimulationRun,
)
from app.services._common import active_vintage
from app.services.bootstrap import ensure_offices, setup_synthetic_system, synthetic_district_config
from app.services.runtime import (
    frame_from_database,
    frame_token,
    get_frame,
    get_model,
    plan_mapping,
    synthetic_world,
)


def test_setup_synthetic_is_idempotent(fresh_session) -> None:  # type: ignore[no-untyped-def]
    s = fresh_session
    first = setup_synthetic_system(s, seed=3)
    assert first.provinces == 12 and first.districts == HOUSE_SEATS and first.senate_seats == SENATE_SEATS
    assert not first.plan_reused
    counts = {m.__name__: s.scalar(select(func.count()).select_from(m)) for m in (Office, Legislature, GovernorSeat, MayorSeat, SenateSeat, DistrictPlan, Apportionment)}  # fmt: skip
    second = setup_synthetic_system(s, seed=3)
    assert second.plan_reused and second.plan_id == first.plan_id
    again = {m.__name__: s.scalar(select(func.count()).select_from(m)) for m in (Office, Legislature, GovernorSeat, MayorSeat, SenateSeat, DistrictPlan, Apportionment)}  # fmt: skip
    assert again == counts
    assert counts["DistrictPlan"] == 1 and counts["Apportionment"] == 1
    # the plan generation is audited
    run = s.scalars(select(SimulationRun).where(SimulationRun.kind == "districts")).one()
    assert run.seed == 3 and run.status == "completed"
    plan = s.get(DistrictPlan, first.plan_id)
    assert plan.config_hash == run.config_hash


def test_offices_seats_and_legislatures(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    types = Counter(t for (t,) in s.execute(select(Office.office_type).where(Office.is_active.is_(True))))
    n_munis = s.scalar(select(func.count()).select_from(Municipality))
    assert types[OfficeType.PRESIDENT.value] == 1 and types[OfficeType.VICE_PRESIDENT.value] == 1
    assert types[OfficeType.HOUSE.value] == HOUSE_SEATS
    assert types[OfficeType.SENATE.value] == SENATE_SEATS
    assert types[OfficeType.GOVERNOR.value] == 12 and types[OfficeType.LIEUTENANT_GOVERNOR.value] == 12
    assert types[OfficeType.MAYOR.value] == n_munis
    assert s.scalar(select(func.count()).select_from(GovernorSeat)) == 12
    assert s.scalar(select(func.count()).select_from(MayorSeat)) == n_munis
    for lg in s.scalars(select(Legislature)):
        if lg.level == "municipal":
            m = s.scalar(select(Municipality).where(Municipality.cbs_code == lg.jurisdiction_code))
            assert lg.seats == municipal_council_size(m.population_official or m.population)
        else:
            pop = s.scalar(select(func.sum(GeoUnit.population)).where(GeoUnit.province_id == lg.province_id))
            assert lg.seats == provincial_legislature_size(int(pop))
        assert lg.electoral_system == "proportional_dhondt"
    # idempotent
    before = s.scalar(select(func.count()).select_from(Office))
    ensure_offices(s)
    assert s.scalar(select(func.count()).select_from(Office)) == before


def test_frame_ids_and_plan_mapping(world) -> None:  # type: ignore[no-untyped-def]
    s = world.session
    frame = get_frame(s)
    ids = dict(s.execute(select(GeoUnit.cbs_code, GeoUnit.id)).tuples().all())
    assert frame.unit_ids is not None
    assert [ids[c] for c in frame.unit_codes] == frame.unit_ids.tolist()
    assert get_frame(s) is frame  # cached
    mapping = plan_mapping(s, frame=frame)
    assert mapping.n_districts == HOUSE_SEATS and (mapping.unit_district >= 0).all()
    assert plan_mapping(s, frame=frame) is mapping
    # the registered synthetic geography is used, the database fallback agrees with it
    base = synthetic_world(1)
    assert frame_token(frame) == frame_token(base.frame)
    rebuilt = frame_from_database(s, active_vintage(s))
    assert rebuilt.unit_codes == frame.unit_codes and rebuilt.muni_codes == frame.muni_codes
    assert np.array_equal(rebuilt.unit_population, frame.unit_population)
    assert np.array_equal(rebuilt.unit_muni, frame.unit_muni)


def test_model_cache_keys_on_frame_content(world) -> None:  # type: ignore[no-untyped-def]
    from app.scenarios.loader import load_scenario

    frame = get_frame(world.session)
    doc = load_scenario("founding-2024")
    m1 = get_model(frame, doc)
    assert get_model(frame, doc) is m1
    other = synthetic_world(2).frame
    assert get_model(other, doc) is not m1


def test_synthetic_district_config_disables_recombination() -> None:
    cfg = synthetic_district_config(2)
    assert cfg.restarts == 2 and cfg.workers == 1
    data = cfg.model_dump()
    if "merge_split" in data:
        assert data["merge_split"]["enabled"] is False


def test_migrations_upgrade_and_downgrade(tmp_path: Path) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from app.db.migrate import current_revision, downgrade_db
    from app.db.session import get_engine, reset_engines
    from app.services.bootstrap import init_db

    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    try:
        init_db(url)
        engine = get_engine(url)
        tables = set(inspect(engine).get_table_names())
        assert set(Base.metadata.tables) <= tables and "alembic_version" in tables
        head = current_revision(url)
        assert head is not None
        with engine.connect() as conn:
            diff = compare_metadata(
                MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
            )
        assert diff == []
        fk = [
            f for f in inspect(engine).get_foreign_keys("race") if f["referred_table"] == "ballot_candidate"
        ]
        assert fk and fk[0]["constrained_columns"] == ["winner_ballot_candidate_id"]
        downgrade_db("base", url)
        assert set(inspect(get_engine(url)).get_table_names()) == {"alembic_version"}
        init_db(url)
        assert current_revision(url) == head
        assert set(Base.metadata.tables) <= set(inspect(get_engine(url)).get_table_names())
    finally:
        reset_engines()


def test_migration_downgrade_with_data(world, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A migrated database holding a finalized election can be downgraded (race ↔ ballot cycle)."""
    from app.db.migrate import downgrade_db, upgrade_db
    from app.db.session import reset_engines

    url = f"sqlite:///{tmp_path / 'data.db'}"
    try:
        upgrade_db(url)
        src = world.session.get_bind()
        from sqlalchemy import create_engine

        dst = create_engine(url)
        with src.connect() as a, dst.begin() as b:
            b.exec_driver_sql("PRAGMA foreign_keys=OFF")
            for table in Base.metadata.sorted_tables:
                rows = [dict(r._mapping) for r in a.execute(table.select())]
                if rows:
                    b.execute(table.insert(), rows)
        dst.dispose()
        downgrade_db("base", url)
        assert set(inspect(create_engine(url)).get_table_names()) == {"alembic_version"}
    finally:
        reset_engines()


def test_setup_rejects_missing_geography(fresh_session) -> None:  # type: ignore[no-untyped-def]
    from app.core.errors import DataNotPreparedError
    from app.services.bootstrap import ensure_apportionment

    with pytest.raises(DataNotPreparedError):
        ensure_apportionment(fresh_session)
