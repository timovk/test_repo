"""Constitutional and data-integrity validation of the stored system (spec §33).

:func:`validate_system` checks the whole database: 12 provinces; the active apportionment
(150 House seats, EV = seats + 2 per province, 174 EV); the derived majorities (88 / 76 / 13);
the active House plan (150 districts, the apportioned number per province, every district in
exactly one province, every geographic unit assigned exactly once, no district crossing a
province); the 24 Senate seats (2 per province, classes 8/8/8, a province's seats in different
classes); municipalities and units in exactly one province; offices; and, for every stored
election, exact reconciliation unit → municipality → province → national and unit → district →
province.  :func:`validate_election` checks one election.

Both return a :class:`ValidationReport` (``ok``, ``errors``, ``warnings``, per-check detail).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_constitution
from app.core.constitution import (
    ELECTORAL_VOTES,
    HOUSE_MAJORITY,
    PRESIDENTIAL_MAJORITY,
    SENATE_MAJORITY,
    ElectionStatus,
    OfficeType,
    RaceType,
    majority_of,
)
from app.core.logging import Timer, get_logger
from app.districts.service import active_plan
from app.elections.calendar import ElectionCalendar
from app.models import (
    Apportionment,
    ApportionmentSeat,
    BallotCandidate,
    DistrictAssignment,
    Election,
    ElectionResult,
    ElectoralVoteAllocation,
    GeoUnit,
    HouseDistrict,
    Municipality,
    Office,
    Province,
    Race,
    SenateSeat,
    TurnoutResult,
)
from app.services._common import REPORTED_STATUSES, active_vintage

log = get_logger(__name__)

_ID_DTYPES = {"m": "Int64", "d": "Int64", "p": "Int64"}
_COUNTS = ("eligible_voters", "ballots_cast", "valid_votes", "blank_votes", "invalid_votes")


@dataclass
class Check:
    """One named check."""

    name: str
    ok: bool
    detail: str = ""
    severity: str = "error"  # error | warning


@dataclass
class ValidationReport:
    """Outcome of a validation run."""

    checks: list[Check] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.ok and c.severity == "error"]

    @property
    def warnings(self) -> list[str]:
        return [f"{c.name}: {c.detail}" for c in self.checks if not c.ok and c.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, name: str, ok: bool, detail: str = "", *, severity: str = "error") -> None:
        self.checks.append(Check(name, bool(ok), detail, severity))

    def extend(self, other: ValidationReport) -> None:
        self.checks.extend(other.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "checks": [asdict(c) for c in self.checks],
        }


# =========================================================================== system
def validate_system(session: Session, *, elections: bool = True) -> ValidationReport:
    """Validate the constitutional arithmetic, the fictional electoral geography and (with
    ``elections``) the reconciliation of every stored election."""
    rep = ValidationReport()
    cons = get_constitution()
    with Timer(log, "validate system"):
        # ---- constitution
        rep.add(
            "majorities derived",
            cons.presidential_majority == majority_of(cons.electoral_votes)
            and cons.house_majority == majority_of(cons.house_seats)
            and cons.senate_majority == majority_of(cons.senate_seats),
            f"EV {cons.electoral_votes}/{cons.presidential_majority}, House {cons.house_seats}/"
            f"{cons.house_majority}, Senate {cons.senate_seats}/{cons.senate_majority}",
        )
        if cons.is_canonical():
            rep.add(
                "canonical constitution",
                (cons.electoral_votes, cons.presidential_majority, cons.house_majority, cons.senate_majority)
                == (ELECTORAL_VOTES, PRESIDENTIAL_MAJORITY, HOUSE_MAJORITY, SENATE_MAJORITY),
                "174 EV / 88 to win, 76 for House control, 13 for Senate control",
            )
        # ---- provinces
        provinces = session.scalars(select(Province).order_by(Province.sort_order)).all()
        rep.add(
            "province count",
            len(provinces) == cons.province_count,
            f"{len(provinces)} provinces (constitution: {cons.province_count})",
        )
        pcode = {p.id: p.code for p in provinces}
        vintage = active_vintage(session)
        if vintage is None:
            rep.add("geography loaded", False, "no active geography vintage")
            return rep
        _geography_checks(session, rep, vintage.id, pcode)
        appt = session.scalars(
            select(Apportionment).where(
                Apportionment.vintage_id == vintage.id, Apportionment.is_active.is_(True)
            )
        ).first()
        seats_by_prov: dict[str, int] = {}
        if appt is None:
            rep.add("apportionment", False, "no active apportionment")
        else:
            seats = session.scalars(
                select(ApportionmentSeat).where(ApportionmentSeat.apportionment_id == appt.id)
            ).all()
            seats_by_prov = {pcode[s.province_id]: int(s.seats) for s in seats}
            total = sum(s.seats for s in seats)
            ev_total = sum(s.electoral_votes for s in seats)
            rep.add(
                "house seats apportioned",
                total == cons.house_seats,
                f"{total} seats (constitution {cons.house_seats})",
            )
            rep.add(
                "apportionment covers every province",
                len(seats) == len(provinces)
                and all(s.seats >= cons.min_house_seats_per_province for s in seats),
                f"{len(seats)} provinces with ≥ {cons.min_house_seats_per_province} seat(s)",
            )
            bad_ev = [
                pcode[s.province_id]
                for s in seats
                if s.electoral_votes != s.seats + cons.senators_per_province
            ]
            rep.add(
                "EV = House seats + senators per province",
                not bad_ev,
                "ok" if not bad_ev else f"wrong in {bad_ev}",
            )
            rep.add(
                "electoral votes total",
                ev_total == cons.electoral_votes == appt.total_electoral_votes,
                f"{ev_total} EV (constitution {cons.electoral_votes}, {cons.presidential_majority} to win)",
            )
        _plan_checks(session, rep, cons, vintage.id, pcode, seats_by_prov)
        _senate_checks(session, rep, cons, pcode)
        _office_checks(session, rep, cons)
        if elections:
            for el in session.scalars(select(Election).order_by(Election.election_date, Election.id)):
                rep.extend(_election_checks(session, el))
    return rep


def _geography_checks(
    session: Session, rep: ValidationReport, vintage_id: int, pcode: dict[int, str]
) -> None:
    munis = session.execute(
        select(Municipality.id, Municipality.cbs_code, Municipality.province_id).where(
            Municipality.vintage_id == vintage_id
        )
    ).all()
    codes = Counter(m[1] for m in munis)
    rep.add(
        "municipalities in exactly one province",
        all(m[2] in pcode for m in munis) and all(v == 1 for v in codes.values()) and len(munis) > 0,
        f"{len(munis)} municipalities",
    )
    mismatched = session.scalar(
        select(func.count())
        .select_from(GeoUnit)
        .join(Municipality, Municipality.id == GeoUnit.municipality_id)
        .where(GeoUnit.vintage_id == vintage_id, GeoUnit.province_id != Municipality.province_id)
    )
    n_units = session.scalar(
        select(func.count()).select_from(GeoUnit).where(GeoUnit.vintage_id == vintage_id)
    )
    rep.add(
        "units in their municipality's province",
        not mismatched,
        f"{n_units} units, {mismatched or 0} in another province than their municipality",
    )


def _plan_checks(
    session: Session,
    rep: ValidationReport,
    cons: Any,
    vintage_id: int,
    pcode: dict[int, str],
    seats_by_prov: dict[str, int],
) -> None:
    plan = active_plan(session)
    if plan is None:
        rep.add("house plan", False, "no active House plan")
        return
    districts = session.execute(
        select(HouseDistrict.id, HouseDistrict.code, HouseDistrict.province_id).where(
            HouseDistrict.plan_id == plan.id
        )
    ).all()
    rep.add(
        "house districts",
        len(districts) == cons.house_seats == plan.total_districts,
        f"{len(districts)} districts in plan {plan.id} (constitution {cons.house_seats})",
    )
    rep.add(
        "districts in exactly one province",
        all(d[2] in pcode for d in districts),
        "every district has one province",
    )
    per_prov = Counter(pcode.get(d[2]) for d in districts)
    wrong = {p: (per_prov.get(p, 0), s) for p, s in seats_by_prov.items() if per_prov.get(p, 0) != s}
    rep.add(
        "districts per province = apportioned seats",
        not wrong,
        "ok" if not wrong else f"(have, apportioned): {wrong}",
    )
    n_units = (
        session.scalar(select(func.count()).select_from(GeoUnit).where(GeoUnit.vintage_id == vintage_id)) or 0
    )
    n_assign, n_distinct = session.execute(
        select(func.count(), func.count(func.distinct(DistrictAssignment.geo_unit_id))).where(
            DistrictAssignment.plan_id == plan.id
        )
    ).one()
    rep.add(
        "every unit assigned exactly once",
        n_assign == n_distinct == n_units,
        f"{n_assign} assignments, {n_distinct} distinct units, {n_units} units in the vintage",
    )
    crossing = session.scalar(
        select(func.count())
        .select_from(DistrictAssignment)
        .join(GeoUnit, GeoUnit.id == DistrictAssignment.geo_unit_id)
        .join(HouseDistrict, HouseDistrict.id == DistrictAssignment.district_id)
        .where(DistrictAssignment.plan_id == plan.id, GeoUnit.province_id != HouseDistrict.province_id)
    )
    rep.add(
        "no district crosses a province",
        not crossing,
        f"{crossing or 0} units outside their district's province",
    )


def _senate_checks(session: Session, rep: ValidationReport, cons: Any, pcode: dict[int, str]) -> None:
    seats = session.execute(select(SenateSeat.province_id, SenateSeat.senate_class)).all()
    rep.add(
        "senate seats",
        len(seats) == cons.senate_seats,
        f"{len(seats)} seats (constitution {cons.senate_seats})",
    )
    per_prov: dict[int, list[int]] = {}
    for pid, cls in seats:
        per_prov.setdefault(pid, []).append(int(cls))
    rep.add(
        "senators per province",
        len(per_prov) == cons.province_count
        and all(len(v) == cons.senators_per_province for v in per_prov.values()),
        f"{cons.senators_per_province} per province",
    )
    distinct = [pcode.get(p) for p, v in per_prov.items() if len(set(v)) != len(v)]
    rep.add(
        "a province's seats in different classes",
        not distinct,
        "ok" if not distinct else f"violated in {distinct}",
    )
    per_class = Counter(int(c) for _, c in seats)
    want = cons.seats_per_senate_class
    rep.add(
        "senate classes",
        sorted(per_class) == list(range(1, cons.senate_classes + 1))
        and all(v == want for v in per_class.values()),
        f"seats per class {dict(sorted(per_class.items()))} (expected {want} each)",
    )


def _office_checks(session: Session, rep: ValidationReport, cons: Any) -> None:
    counts = dict(
        session.execute(
            select(Office.office_type, func.count())
            .where(Office.is_active.is_(True))
            .group_by(Office.office_type)
        )
        .tuples()
        .all()
    )
    expected = {
        OfficeType.PRESIDENT.value: 1,
        OfficeType.VICE_PRESIDENT.value: 1,
        OfficeType.HOUSE.value: cons.house_seats,
        OfficeType.SENATE.value: cons.senate_seats,
        OfficeType.GOVERNOR.value: cons.province_count,
    }
    if cons.lieutenant_governors:
        expected[OfficeType.LIEUTENANT_GOVERNOR.value] = cons.province_count
    wrong = {k: (counts.get(k, 0), v) for k, v in expected.items() if counts.get(k, 0) != v}
    rep.add(
        "offices",
        not wrong,
        "ok" if not wrong else f"(have, expected): {wrong}",
        severity="warning",
    )


# =========================================================================== elections
def validate_election(session: Session, election_id: int) -> ValidationReport:
    """Validate one stored election (races per the calendar, ballots, reconciliation, EV)."""
    el = session.get(Election, election_id)
    rep = ValidationReport()
    if el is None:
        rep.add("election exists", False, f"election {election_id} not found")
        return rep
    rep.extend(_election_checks(session, el))
    return rep


def _election_checks(session: Session, el: Election) -> ValidationReport:
    rep = ValidationReport()
    tag = f"election {el.id} ({el.year})"
    cons = get_constitution()
    cycle = ElectionCalendar.from_config(constitution=cons).cycle(el.year)
    types = dict(
        session.execute(
            select(Race.race_type, func.count()).where(Race.election_id == el.id).group_by(Race.race_type)
        )
        .tuples()
        .all()
    )
    if cycle.president and types.get(RaceType.PRESIDENT.value):
        rep.add(
            f"{tag}: presidential contests",
            types.get(RaceType.PRESIDENT_PROVINCE.value, 0) == cons.province_count,
            f"{types.get(RaceType.PRESIDENT_PROVINCE.value, 0)} province contests",
        )
    if cycle.house:
        rep.add(
            f"{tag}: House races",
            types.get(RaceType.HOUSE.value, 0) == cons.house_seats,
            f"{types.get(RaceType.HOUSE.value, 0)} House races",
        )
    empty = session.scalar(
        select(func.count())
        .select_from(Race)
        .where(
            Race.election_id == el.id,
            ~select(BallotCandidate.id).where(BallotCandidate.race_id == Race.id).exists(),
        )
    )
    rep.add(f"{tag}: every race has ballot lines", not empty, f"{empty or 0} races without lines")
    if el.status == ElectionStatus.SCHEDULED.value:
        return rep
    problems = reconcile_election(session, el.id)
    rep.add(
        f"{tag}: results reconcile",
        not problems,
        "unit → municipality → province → national and unit → district exact"
        if not problems
        else "; ".join(problems[:5]),
    )
    if el.status in REPORTED_STATUSES and types.get(RaceType.PRESIDENT.value):
        pres_id = session.scalar(
            select(Race.id).where(Race.election_id == el.id, Race.race_type == RaceType.PRESIDENT.value)
        )
        ev = session.scalar(
            select(func.sum(ElectoralVoteAllocation.electoral_votes)).where(
                ElectoralVoteAllocation.race_id == pres_id
            )
        )
        rep.add(
            f"{tag}: electoral votes allocated",
            int(ev or 0) == cons.electoral_votes,
            f"{int(ev or 0)} of {cons.electoral_votes} EV allocated",
            severity="error" if cons.province_tie_rule == "lot" else "warning",
        )
    if el.status in REPORTED_STATUSES:
        undecided = session.scalar(
            select(func.count())
            .select_from(Race)
            .where(
                Race.election_id == el.id,
                Race.winner_ballot_candidate_id.is_(None),
                Race.race_type != RaceType.PRESIDENT.value,
            )
        )
        rep.add(f"{tag}: every race has a winner", not undecided, f"{undecided or 0} races without a winner")
    return rep


def reconcile_election(session: Session, election_id: int) -> list[str]:
    """Exact reconciliation of an election's stored rows (all races at once, vectorised):
    unit → municipality → province → national and unit → district → province, for line votes
    and for every turnout count; plus per-geography identities."""
    race_rows = session.execute(select(Race.id).where(Race.election_id == election_id)).scalars().all()
    if not race_rows:
        return []
    problems: list[str] = []
    res_parts, turn_parts = [], []
    ids = list(race_rows)
    for i in range(0, len(ids), 400):
        part = ids[i : i + 400]
        res_parts.append(
            pd.DataFrame(
                session.execute(
                    select(
                        ElectionResult.race_id,
                        ElectionResult.ballot_candidate_id,
                        ElectionResult.level,
                        ElectionResult.municipality_id,
                        ElectionResult.district_id,
                        ElectionResult.province_id,
                        ElectionResult.votes,
                    ).where(ElectionResult.race_id.in_(part))
                ).all(),
                columns=["race", "line", "level", "m", "d", "p", "votes"],
            ).astype(_ID_DTYPES)
        )
        turn_parts.append(
            pd.DataFrame(
                session.execute(
                    select(
                        TurnoutResult.race_id,
                        TurnoutResult.level,
                        TurnoutResult.municipality_id,
                        TurnoutResult.district_id,
                        TurnoutResult.province_id,
                        *[getattr(TurnoutResult, c) for c in _COUNTS],
                    ).where(TurnoutResult.race_id.in_(part))
                ).all(),
                columns=["race", "level", "m", "d", "p", *_COUNTS],
            ).astype(_ID_DTYPES)
        )
    res = _concat(res_parts)
    turn = _concat(turn_parts)
    if res.empty or turn.empty:
        return ["no stored results"]
    for name, df in (("votes", res), ("turnout", turn)):
        have = df.groupby("race")["level"].agg(lambda x: set(x))
        missing = [int(r) for r in ids if r not in have.index or not have[r] >= _REQUIRED_LEVELS]
        if missing:
            problems.append(f"{name}: {len(missing)} races lack a unit/municipality/province/national level")
    # identities per geography
    t = turn
    if ((t["valid_votes"] + t["blank_votes"] + t["invalid_votes"]) != t["ballots_cast"]).any():
        problems.append("valid + blank + invalid != ballots cast")
    if (t["ballots_cast"] > t["eligible_voters"]).any():
        problems.append("ballots cast exceed eligible voters")
    if (res["votes"] < 0).any():
        problems.append("negative votes")
    for child, parent, key in (
        ("unit", "municipality", "m"),
        ("municipality", "province", "p"),
        ("unit", "district", "d"),
        ("district", "province", "p"),
        ("province", "national", None),
    ):
        problems += _compare(res, child, parent, key, ["votes"], ["line"], f"{child}→{parent} votes")
        problems += _compare(turn, child, parent, key, list(_COUNTS), [], f"{child}→{parent} turnout")
    return problems


_REQUIRED_LEVELS = {"unit", "municipality", "province", "national"}


def _concat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [p for p in parts if not p.empty]
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _compare(
    df: pd.DataFrame,
    child: str,
    parent: str,
    key: str | None,
    values: list[str],
    extra: list[str],
    label: str,
) -> list[str]:
    c = df[df["level"] == child]
    p = df[df["level"] == parent]
    both = set(c["race"].unique()) & set(p["race"].unique())
    c = c[c["race"].isin(both)]
    p = p[p["race"].isin(both)]
    if c.empty and p.empty:
        return []
    group = ["race", *extra] + ([key] if key else [])
    if key is not None:
        if c[key].isna().any() or p[key].isna().any():
            return [f"{label}: rows without {key}"]
        c = c.assign(**{key: c[key].astype(np.int64)})
        p = p.assign(**{key: p[key].astype(np.int64)})
    lhs = c.groupby(group, sort=True)[values].sum()
    rhs = p.groupby(group, sort=True)[values].sum()
    if not lhs.index.equals(rhs.index):
        return [f"{label}: geography sets differ ({len(lhs)} vs {len(rhs)})"]
    if not np.array_equal(lhs.to_numpy(), rhs.to_numpy()):
        return [f"{label}: sums differ"]
    return []
