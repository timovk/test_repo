"""Election lifecycle: create (scenario → rows) → simulate (hidden result + night timeline) →
finalize (tabulation, recounts, Electoral College, contingent election, seats, office holders).

    from app.services.elections import create_election, simulate_election, finalize_election

    election = create_election(session, "demo-2028")          # SCHEDULED
    simulate_election(session, election.id)                   # SIMULATED (result hidden)
    finalize_election(session, election.id)                  # FINAL (results reported)

Status flow: ``scheduled`` → ``simulated`` → (``live`` during an election night, managed by the
night service) → ``final``.  **Hidden-until-reported rule:** a SIMULATED or LIVE election already
has its complete unit-level result in the database, but the read layer and
:func:`app.services.results.results_frame` never expose it until the election is FINAL (the
election night reveals it progressively instead).

Every step records a ``simulation_run`` (``election-setup`` / ``election`` / ``election-final``)
with its seed; the same scenario, seed and system give identical stored results.  The caller owns
the transaction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_constitution
from app.core.constitution import ElectionStatus, RaceType
from app.core.errors import ElectionError, ScenarioError
from app.core.logging import Timer, get_logger, log_ctx
from app.core.rng import config_hash
from app.districts.service import active_plan
from app.elections.calendar import ElectionCalendar
from app.elections.recount import RecountConfig, load_recount_config
from app.elections.seats import chamber_composition, chamber_control
from app.elections.types import RaceVotes
from app.models import (
    Apportionment,
    ApportionmentSeat,
    BallotCandidate,
    Campaign,
    Candidate,
    ContingentElection,
    Election,
    ElectoralVoteAllocation,
    GeoUnit,
    LegislatureSeatResult,
    Office,
    OfficeHolder,
    Party,
    Poll,
    Province,
    Race,
    Scenario,
    SenateSeat,
    utcnow,
)
from app.reporting.config import load_night_config
from app.reporting.live import CallRecord
from app.reporting.timeline import generate_timeline
from app.scenarios.loader import dump_scenario, load_scenario, load_scenario_text, validate_scenario
from app.scenarios.schema import ScenarioDocument
from app.services import _create as create_ops
from app.services._common import (
    PRESIDENT_OFFICE,
    REPORTED_STATUSES,
    RunRecorder,
    active_vintage,
    dumps,
    latest_run,
    loads,
)
from app.services._finalize import Finalizer
from app.services._store import clear_simulation, store_results, store_timeline
from app.services.runtime import (
    RUN_FINALIZE,
    RUN_SETUP,
    RUN_SIMULATE,
    ElectionInputs,
    election_inputs,
    get_election,
    get_frame,
    get_model,
    load_final_race_votes,
    plan_mapping,
    scenario_hash,
)
from app.simulation.voting import simulate_election as simulate_votes

log = get_logger(__name__)

__all__ = [
    "create_election",
    "election_summary",
    "finalize_election",
    "instant_finalize",
    "list_elections",
    "president_holder",
    "reported_on_or_after",
    "require_certifiable",
    "simulate_election",
]


# =========================================================================== helpers
def _resolve_scenario(scenario: str | Path | ScenarioDocument) -> ScenarioDocument:
    if isinstance(scenario, ScenarioDocument):
        return scenario
    return load_scenario(scenario)


def _shift_year(doc: ScenarioDocument, year: int) -> ScenarioDocument:
    """The scenario moved to another election year: date and Senate classes from the calendar,
    polling window shifted by the same number of years, the year in the name replaced."""
    data = doc.model_dump(mode="json")
    old = doc.scenario.year
    delta = year - old
    data["scenario"]["year"] = year
    data["scenario"]["election_date"] = None
    data["scenario"]["name"] = str(data["scenario"]["name"]).replace(str(old), str(year))
    data["senate"]["classes_up"] = None
    start = data.get("polling", {}).get("start_date")
    if start:
        d = date.fromisoformat(start)
        try:
            data["polling"]["start_date"] = d.replace(year=d.year + delta).isoformat()
        except ValueError:  # 29 February
            data["polling"]["start_date"] = d.replace(year=d.year + delta, day=28).isoformat()
    return ScenarioDocument.model_validate(data)


def _store_scenario(session: Session, doc: ScenarioDocument, text: str) -> Scenario:
    """The ``scenario`` row holding exactly this document (a changed document with a known slug is
    stored as a variant ``<slug>--<hash8>`` whose parent is the first row)."""
    h = config_hash(text)
    slug = doc.scenario.slug
    row = session.scalar(select(Scenario).where(Scenario.slug == slug))
    if row is not None and row.document_hash == h:
        return row
    parent_id = None
    if row is not None:
        parent_id = row.id
        slug = f"{doc.scenario.slug}--{h[:8]}"[:80]
        variant = session.scalar(select(Scenario).where(Scenario.slug == slug))
        if variant is not None:
            return variant
    new = Scenario(
        slug=slug,
        name=doc.scenario.name[:160],
        description=doc.scenario.description,
        year=doc.scenario.year,
        seed=doc.scenario.seed,
        parent_id=parent_id,
        document=text,
        document_hash=h,
        is_fictional=True,
    )
    session.add(new)
    session.flush()
    return new


def _ev_by_province(session: Session, apportionment_id: int, order: Sequence[str]) -> dict[str, int]:
    rows = session.execute(
        select(Province.code, ApportionmentSeat.electoral_votes)
        .join(Province, Province.id == ApportionmentSeat.province_id)
        .where(ApportionmentSeat.apportionment_id == apportionment_id)
    ).all()
    ev = {c: int(v) for c, v in rows}
    return {c: ev[c] for c in order if c in ev}


def _unit_wijk(session: Session, inputs: ElectionInputs) -> list[str]:
    """Wijk (neighbourhood cluster) code per frame unit from ``geo_unit.wijk_code`` (the unit
    code prefix when the CBS wijk is unknown)."""
    f = inputs.frame
    vintage_id = get_election(session, inputs.election_id).vintage_id
    rows = dict(
        session.execute(select(GeoUnit.id, GeoUnit.wijk_code).where(GeoUnit.vintage_id == vintage_id))
        .tuples()
        .all()
    )
    return [
        rows.get(int(uid)) or code[:8] for uid, code in zip(f.unit_ids.tolist(), f.unit_codes, strict=True)
    ]


# =========================================================================== create
def create_election(
    session: Session,
    scenario: str | Path | ScenarioDocument,
    *,
    seed: int | None = None,
    year: int | None = None,
    strict: bool = True,
) -> Election:
    """Create a SCHEDULED election from a scenario (slug, path or document).

    Races follow ``ElectionCalendar.cycle(year)``: the President (``PRES`` + 12 ``PRES-<PV>``
    electoral-vote contests), all House districts of the active plan, the Senate seats of the
    classes up (plus the scenario's special elections; every seat at the founding election),
    governors with lieutenant governors, provincial legislatures, mayors and municipal councils as
    due and enabled by the scenario.  Incumbents come from the office holders serving on election
    day (else from the scenario's ``incumbent_office`` declarations); they run again or leave open
    seats per the scenario's rules; new FICTIONAL candidates are generated with persistent keys.
    Parties, lineage events, candidates, ballot lines (with party snapshots), campaigns and
    fictional polls are stored.  ``strict=False`` accepts scenario references to places missing
    from the geography (used for the REAL demo scenarios on the synthetic test country).
    """
    doc = _resolve_scenario(scenario)
    if year is not None and int(year) != doc.scenario.year:
        doc = _shift_year(doc, int(year))
    text = dump_scenario(doc)
    doc = load_scenario_text(text)
    year = doc.scenario.year
    frame = get_frame(session)
    problems = validate_scenario(doc, frame)
    if problems:
        if strict:
            raise ScenarioError(
                f"scenario {doc.scenario.slug} does not fit the geography:\n  - " + "\n  - ".join(problems)
            )
        log.warning("scenario %s: %d references ignored (strict=False)", doc.scenario.slug, len(problems))
    cons = get_constitution()
    calendar = ElectionCalendar.from_config(constitution=cons)
    cycle = calendar.cycle(year)
    if not cycle.has_elections or cycle.election_type is None:
        raise ElectionError(f"no regular election is held in {year}")
    if cycle.election_type.value != doc.scenario.election_type:
        log.warning(
            "scenario %s says %s but the calendar holds a %s election in %d",
            doc.scenario.slug,
            doc.scenario.election_type,
            cycle.election_type.value,
            year,
        )
    vintage = active_vintage(session)
    if vintage is None:
        raise ElectionError("no geography loaded (run the setup first)")
    appt = session.scalars(
        select(Apportionment).where(Apportionment.vintage_id == vintage.id, Apportionment.is_active.is_(True))
    ).first()
    if appt is None:
        raise ElectionError("no active apportionment (run the setup first)")
    plan = active_plan(session)
    if plan is None and cycle.house:
        raise ElectionError("no active House district plan (run the setup first)")
    mapping = plan_mapping(session, plan.id, frame) if plan is not None else None
    plan_id = plan.id if plan is not None else None
    edate = doc.scenario.election_date or cycle.date
    require_certifiable(session, edate)
    run_seed = int(seed if seed is not None else doc.scenario.seed)
    scen = _store_scenario(session, doc, text)
    prev = session.scalars(
        select(Election)
        .where(Election.election_date < edate)
        .order_by(Election.election_date.desc(), Election.id.desc())
        .limit(1)
    ).first()
    election = Election(
        year=year,
        election_date=edate,
        name=doc.scenario.name[:160],
        election_type=cycle.election_type.value,
        status=ElectionStatus.SCHEDULED.value,
        scenario_id=scen.id,
        seed=run_seed,
        vintage_id=vintage.id,
        apportionment_id=appt.id,
        district_plan_id=plan_id,
        previous_election_id=prev.id if prev is not None else None,
        polls_close_local=calendar.config.polls_close,
        timezone=calendar.config.timezone,
        is_fictional=True,
        notes="FICTIONAL election in a FICTIONAL federal system over REAL Dutch geography",
    )
    session.add(election)
    session.flush()
    with (
        Timer(log, f"create election {doc.scenario.slug}"),
        RunRecorder(
            session,
            RUN_SETUP,
            run_seed,
            election_id=election.id,
            scenario_id=scen.id,
            config_hash=scenario_hash(doc),
        ) as rec,
    ):
        later = session.scalar(select(func.max(Election.year)).where(Election.id != election.id))
        historical = later is not None and later > year  # an older election created after a newer one
        parties = create_ops.upsert_parties(session, doc, year, historical=historical)
        model = get_model(frame, doc)
        holders = create_ops.holders_at(session, edate)
        prov_ids = dict(session.execute(select(Province.code, Province.id)).tuples().all())
        party_ids = {c: p.id for c, p in parties.items()}
        ideology = {p.code: (p.ideology.economic, p.ideology.social) for p in doc.parties}
        cand_ids = create_ops.upsert_candidates(
            session,
            doc.candidates,
            year,
            party_ids,
            prov_ids,
            frame,
            ideology,
            update_existing=not historical,
        )
        offices = dict(session.execute(select(Office.code, Office.id)).tuples().all())
        for office_code, spec in create_ops.scenario_incumbents(doc).items():
            if office_code in holders or office_code not in offices:
                continue
            holders[office_code] = create_ops.Holder(
                office_code=office_code,
                office_id=offices[office_code],
                holder_id=0,
                candidate_id=cand_ids[spec.key],
                candidate_key=spec.key,
                seat_party=spec.party,
                current_party=spec.party,
                term_start=edate,
                term_end=None,
            )
        pres = holders.get(PRESIDENT_OFFICE)
        if pres is not None:
            president_party = pres.current_party or pres.seat_party
        else:
            president_party = doc.environment.president_party
        if president_party is not None and president_party not in model.party_index:
            president_party = None
        existing = set(session.scalars(select(Candidate.key)).all()) | {c.key for c in doc.candidates}
        state = create_ops.SetupState(
            session=session,
            doc=doc,
            frame=frame,
            model=model,
            mapping=mapping,
            cycle=cycle,
            calendar=calendar,
            year=year,
            seed=run_seed,
            election_date=edate,
            holders=holders,
            ev_by_province=_ev_by_province(session, appt.id, frame.province_codes),
            existing_keys=existing,
            candidate_specs={c.key: c for c in doc.candidates},
        )
        planned: list[create_ops.PlannedRace] = []
        blocked: set[str] = set()
        if cycle.president:
            pres_races = create_ops.presidential_plan(state, president_party)
            planned += pres_races
            for pr in pres_races[:1]:
                for pl in pr.lines:
                    blocked |= {k for k in (pl.line.candidate_key, pl.line.running_mate_key) if k}
        seats_up = create_ops.senate_seats_up(session, doc, cycle)
        if doc.senate.classes_up is not None and set(doc.senate.classes_up) != set(cycle.senate_classes):
            state.warnings.append(
                f"scenario Senate classes {sorted(doc.senate.classes_up)} differ from the calendar's "
                f"{list(cycle.senate_classes)}; classes outside the rotation only fill the remainder of a term"
            )
        municipal = cycle.municipal and doc.municipal.enabled
        if cycle.municipal and not doc.municipal.enabled:
            state.warnings.append("municipal elections are due but disabled by the scenario")
        planned += create_ops.down_ballot_plan(
            state,
            house=cycle.house,
            senate_seats=seats_up,
            governors=cycle.governors and doc.governors.enabled,
            mayors=municipal,
            blocked=blocked,
        )
        if cycle.provincial_legislatures:
            seats = create_ops.legislature_jurisdictions(session, "provincial")
            planned += create_ops.party_list_plan(
                state,
                RaceType.PROVINCIAL_LEGISLATURE,
                [(f"PROVLEG-{pv}", pv, seats[pv]) for pv in frame.province_codes if pv in seats],
            )
        if municipal and doc.municipal.councils:
            if doc.municipal.council_system != "proportional_dhondt":
                state.warnings.append(
                    f"council_system {doc.municipal.council_system!r} is not supported; councils use D'Hondt lists"
                )
            seats = create_ops.legislature_jurisdictions(session, "municipal")
            planned += create_ops.party_list_plan(
                state,
                RaceType.MUNICIPAL_COUNCIL,
                [(f"COUNCIL-{gm}", gm, seats[gm]) for gm in frame.muni_codes if gm in seats],
            )
        if not planned:
            raise ElectionError(f"no races to hold in {year} (check the scenario and the calendar)")
        needed = {
            k
            for pr in planned
            for pl in pr.lines
            for k in (pl.line.candidate_key, pl.line.running_mate_key)
            if k and k not in cand_ids
        }
        cand_ids.update(
            create_ops.upsert_candidates(
                session,
                [state.candidate_specs[k] for k in sorted(needed)],
                year,
                party_ids,
                prov_ids,
                frame,
                ideology,
                update_existing=False,
            )
        )
        create_ops.store_races(session, election, planned, cand_ids, parties, doc)
        up_codes = {pr.senate_seat_code for pr in planned if pr.senate_seat_code}
        holdover = {}
        for seat_code in session.scalars(select(SenateSeat.code)).all():
            if seat_code in up_codes:
                continue
            h = holders.get(seat_code)
            if h is not None and h.holder_id:
                holdover[seat_code] = h.seat_party
        snapshot = {"president_party": president_party, "holdover_senate": holdover}
        rec.summary.update(snapshot)
        inputs = election_inputs(session, election.id, frame=frame, setup_snapshot=snapshot)
        campaigns = create_ops.store_campaigns(session, inputs, parties, create_ops.campaign_seed(run_seed))
        if campaigns.get("enabled"):
            inputs = election_inputs(session, election.id, frame=frame, setup_snapshot=snapshot)
        polls = create_ops.store_generated_polls(session, inputs, parties, create_ops.poll_seed(run_seed))
        counts: dict[str, int] = {}
        for pr in planned:
            counts[pr.race_type.value] = counts.get(pr.race_type.value, 0) + 1
        rec.summary.update(
            {
                "scenario": doc.scenario.slug,
                "races": counts,
                "ballot_lines": sum(len(pr.lines) for pr in planned),
                "new_candidates": len(needed),
                "campaigns": campaigns,
                "polls": polls,
                "warnings": state.warnings + model.warnings[:20],
            }
        )
    log.info(
        "election created",
        extra=log_ctx(election_id=election.id, year=year, races=sum(counts.values()), seed=run_seed),
    )
    return election


# =========================================================================== simulate
def simulate_election(session: Session, election_id: int, *, seed: int | None = None) -> Election:
    """Simulate the complete (hidden) result and the election-night reporting timeline.

    Results are persisted for every race at unit, municipality, district (House and presidential
    races), province and national level with exact reconciliation; the realised environment is
    stored on the election.  A SIMULATED election can be re-simulated (the previous draw is
    replaced); LIVE and FINAL elections cannot.  ``seed`` overrides (and replaces) the election seed.
    """
    el = get_election(session, election_id)
    if el.status in (ElectionStatus.LIVE.value, *REPORTED_STATUSES):
        raise ElectionError(f"election {el.id} is {el.status}; its result can no longer be simulated")
    if seed is not None:
        el.seed = int(seed)
    inputs = election_inputs(session, el.id)
    if el.status == ElectionStatus.SIMULATED.value:
        clear_simulation(session, el.id, list(inputs.race_ids.values()))
    with (
        Timer(log, f"simulate election {el.id}"),
        RunRecorder(
            session,
            RUN_SIMULATE,
            int(el.seed),
            election_id=el.id,
            scenario_id=el.scenario_id,
            config_hash=inputs.scenario_hash,
        ) as rec,
    ):
        draw = simulate_votes(inputs.model, list(inputs.races.values()), int(el.seed), inputs.context)
        counts = store_results(session, inputs, draw.races)
        el.national_environment_json = dumps(draw.environment)
        night = load_night_config()
        tl = generate_timeline(
            inputs.frame,
            draw.turnout.ballots_cast,
            night,
            seed=int(el.seed),
            election_date=el.election_date,
            unit_wijk=_unit_wijk(session, inputs),
        )
        problems = tl.validate()
        if problems:
            raise ElectionError(f"invalid reporting timeline: {problems}")
        meta = store_timeline(session, inputs, tl)
        total_b = int(draw.turnout.ballots_cast.sum())
        total_e = int(draw.turnout.eligible.sum())
        rec.summary.update(
            {
                "races": len(draw.races),
                "rows": counts,
                "ballots_cast": total_b,
                "eligible": total_e,
                "turnout_pct": round(100.0 * total_b / total_e, 4) if total_e else 0.0,
                "model_fingerprint": inputs.model.fingerprint,
                "timeline": meta,
            }
        )
        el.status = ElectionStatus.SIMULATED.value
        el.simulated_at = utcnow()
        session.flush()
    return el


# =========================================================================== finalize
def reported_on_or_after(
    session: Session, election_date: date, *, exclude_id: int | None = None
) -> int | None:
    """Id of a reported (FINAL / CERTIFIED) election held on or after ``election_date``."""
    q = select(Election.id).where(
        Election.status.in_(list(REPORTED_STATUSES)),
        Election.election_date >= election_date,
    )
    if exclude_id is not None:
        q = q.where(Election.id != int(exclude_id))
    return session.scalars(q.order_by(Election.election_date, Election.id).limit(1)).first()


def require_certifiable(session: Session, election_date: date, *, exclude_id: int | None = None) -> None:
    """Elections are certified in chronological order (office holders, incumbents and history
    build on the previous election): raise :class:`ElectionError` when an election held on or
    after ``election_date`` is already reported, so an election of that date could never be
    finalized."""
    later = reported_on_or_after(session, election_date, exclude_id=exclude_id)
    if later is not None:
        raise ElectionError(
            f"election {later} on or after {election_date} is already final; elections are finalized "
            "in chronological order, so an election of that date can no longer be certified"
        )


def finalize_election(
    session: Session,
    election_id: int,
    *,
    call_records: Sequence[CallRecord] | None = None,
    recount_config: RecountConfig | None = None,
    inputs: ElectionInputs | None = None,
    votes: Mapping[str, RaceVotes] | None = None,
) -> Election:
    """Certify a simulated election: tabulation (seeded lots), automatic recounts (audited
    corrections applied to the stored rows), Electoral College and contingent election, race
    summaries, D'Hondt seats, office holders (terms from the calendar) and — when given — the race
    calls of the election night.  Status → FINAL (the result becomes visible).

    ``inputs`` / ``votes`` may pass the engine inputs and the stored unit results already in
    memory (the election-night service holds both), which saves reloading them; ``votes`` is not
    modified.  They must be those of this election as stored (``election_inputs`` /
    ``load_final_race_votes``)."""
    el = get_election(session, election_id)
    if el.status in REPORTED_STATUSES:
        raise ElectionError(f"election {el.id} is already {el.status}")
    if el.status == ElectionStatus.SCHEDULED.value:
        raise ElectionError(f"election {el.id} has not been simulated")
    require_certifiable(session, el.election_date, exclude_id=el.id)
    if inputs is None or votes is None:
        inputs = election_inputs(session, el.id)
        race_votes = load_final_race_votes(session, el.id, inputs, with_expectation=False)
    else:
        missing = sorted(set(inputs.races) - set(votes))
        if int(inputs.election_id) != el.id or missing:
            raise ElectionError(
                f"finalize_election({el.id}): the given inputs/votes do not belong to this election"
                + (f" (missing races {missing[:5]})" if missing else "")
            )
        race_votes = {code: votes[code] for code in inputs.races}
    cfg = recount_config or load_recount_config()
    with (
        Timer(log, f"finalize election {el.id}"),
        RunRecorder(
            session,
            RUN_FINALIZE,
            int(el.seed),
            election_id=el.id,
            scenario_id=el.scenario_id,
            config_hash=inputs.scenario_hash,
        ) as rec,
    ):
        fin = Finalizer(session, el, inputs, race_votes, cfg)
        fin.tabulate_and_recount()
        fin.electoral_college()
        fin.race_summaries()
        fin.legislature_seats()
        fin.office_holders()
        if call_records is not None:
            fin.store_calls(call_records)
        el.status = ElectionStatus.FINAL.value
        el.finalized_at = utcnow()
        r = fin.result
        rec.summary.update(
            {
                "recounts": r.recounts,
                "electoral_votes": r.electoral_votes,
                "president": r.president,
                "decided_by": r.decided_by,
                "contingent": r.contingent,
                "office_holders": r.office_holders,
                "legislature_rows": r.legislature_rows,
                "calls": r.calls,
            }
        )
        session.flush()
    return el


def instant_finalize(session: Session, election_id: int) -> Election:
    """Simulate (if needed) and finalize at once, without an election night."""
    el = get_election(session, election_id)
    if el.status == ElectionStatus.SCHEDULED.value:
        simulate_election(session, election_id)
    return finalize_election(session, election_id)


# =========================================================================== summaries
def _contents(session: Session, el: Election) -> dict[str, Any]:
    cal = ElectionCalendar.from_config()
    cyc = cal.cycle(el.year)
    types = dict(
        session.execute(
            select(Race.race_type, func.count()).where(Race.election_id == el.id).group_by(Race.race_type)
        )
        .tuples()
        .all()
    )
    classes = sorted(
        {
            int(c)
            for (c,) in session.execute(
                select(SenateSeat.senate_class)
                .join(Race, Race.senate_seat_id == SenateSeat.id)
                .where(Race.election_id == el.id)
            ).all()
        }
    )
    return {
        "president": types.get(RaceType.PRESIDENT.value, 0) > 0,
        "house": types.get(RaceType.HOUSE.value, 0) > 0,
        "senate_classes": classes,
        "governors": types.get(RaceType.GOVERNOR.value, 0) > 0,
        "provincial_legislatures": types.get(RaceType.PROVINCIAL_LEGISLATURE.value, 0) > 0,
        "municipal": types.get(RaceType.MAYOR.value, 0) > 0
        or types.get(RaceType.MUNICIPAL_COUNCIL.value, 0) > 0,
        "race_counts": {k: int(v) for k, v in sorted(types.items())},
        "founding": cyc.is_founding,
    }


def _brief(session: Session, el: Election) -> dict[str, Any]:
    scen = session.get(Scenario, el.scenario_id) if el.scenario_id is not None else None
    return {
        "id": el.id,
        "year": el.year,
        "name": el.name,
        "election_type": el.election_type,
        "status": el.status,
        "election_date": el.election_date.isoformat(),
        "seed": int(el.seed),
        "scenario": None
        if scen is None
        else {"id": scen.id, "slug": scen.slug, "name": scen.name, "hash": scen.document_hash},
        "previous_election_id": el.previous_election_id,
        "reported": el.status in REPORTED_STATUSES,
        "data_category": "SIMULATED",
    }


def _party_code_map(session: Session) -> dict[int, str]:
    return dict(session.execute(select(Party.id, Party.code)).tuples().all())


def _final_results(session: Session, el: Election) -> dict[str, Any]:
    cons = get_constitution()
    pcode = _party_code_map(session)
    races = session.scalars(select(Race).where(Race.election_id == el.id)).all()
    by_type: dict[str, list[Race]] = {}
    for r in races:
        by_type.setdefault(r.race_type, []).append(r)
    out: dict[str, Any] = {}
    pres = next(iter(by_type.get(RaceType.PRESIDENT.value, [])), None)
    if pres is not None:
        lines = {
            b.id: b
            for b in session.scalars(select(BallotCandidate).where(BallotCandidate.race_id == pres.id))
        }
        ev: dict[str, int] = {}
        for bid, n in session.execute(
            select(
                ElectoralVoteAllocation.ballot_candidate_id, func.sum(ElectoralVoteAllocation.electoral_votes)
            )
            .where(ElectoralVoteAllocation.race_id == pres.id)
            .group_by(ElectoralVoteAllocation.ballot_candidate_id)
        ).all():
            ev[lines[bid].line_key or str(bid)] = int(n)
        winner = lines.get(pres.winner_ballot_candidate_id) if pres.winner_ballot_candidate_id else None
        cont = session.scalar(select(ContingentElection).where(ContingentElection.race_id == pres.id))
        out["president"] = {
            "winner": None if winner is None else winner.line_key,
            "winner_name": None if winner is None else winner.ballot_name,
            "winner_party": pcode.get(pres.winner_party_id) if pres.winner_party_id else None,
            "electoral_votes": ev,
            "electoral_votes_total": cons.electoral_votes,
            "majority": cons.presidential_majority,
            "decided_by": pres.decided_by,
            "popular_vote_margin_pct": pres.margin_pct,
            "contingent": None
            if cont is None
            else {"mode": cont.mode, "outcome": cont.outcome, "rounds": cont.rounds},
        }
    house = by_type.get(RaceType.HOUSE.value, [])
    if house:
        comp = chamber_composition(
            [pcode.get(r.winner_party_id) if r.winner_party_id else None for r in house]
        )
        ctl = chamber_control(comp, cons.house_seats, cons.house_majority)
        out["house"] = {
            "composition": comp,
            "controlling_party": ctl.controlling_party,
            "majority": cons.house_majority,
        }
    senate = by_type.get(RaceType.SENATE.value, [])
    if senate:
        setup = latest_run(session, el.id, RUN_SETUP)
        holdover = loads(setup.summary_json).get("holdover_senate", {}) if setup is not None else {}
        members = list(holdover.values()) + [
            pcode.get(r.winner_party_id) if r.winner_party_id else None for r in senate
        ]
        comp = chamber_composition(members)
        ctl = chamber_control(comp, cons.senate_seats, cons.senate_majority)
        out["senate"] = {
            "composition": comp,
            "seats_contested": len(senate),
            "holdovers": len(holdover),
            "controlling_party": ctl.controlling_party,
            "majority": cons.senate_majority,
        }
    gov = by_type.get(RaceType.GOVERNOR.value, [])
    if gov:
        out["governors"] = {r.code.removeprefix("GOV-"): pcode.get(r.winner_party_id) for r in gov}
    seats_rows = session.execute(
        select(Race.race_type, LegislatureSeatResult.party_id, func.sum(LegislatureSeatResult.seats))
        .join(Race, Race.id == LegislatureSeatResult.race_id)
        .where(Race.election_id == el.id)
        .group_by(Race.race_type, LegislatureSeatResult.party_id)
    ).all()
    if seats_rows:
        legs: dict[str, dict[str, int]] = {}
        for rt, pid, n in seats_rows:
            legs.setdefault(rt, {})[pcode.get(pid, str(pid))] = int(n)
        out["legislature_seats"] = legs
    sim = latest_run(session, el.id, RUN_SIMULATE)
    if sim is not None:
        out["turnout_pct"] = loads(sim.summary_json).get("turnout_pct")
    out["flips"] = int(sum(1 for r in races if r.flipped))
    fin = latest_run(session, el.id, RUN_FINALIZE)
    out["recounts"] = loads(fin.summary_json).get("recounts", []) if fin is not None else []
    return out


def election_summary(session: Session, election_id: int, *, include_hidden: bool = False) -> dict[str, Any]:
    """JSON-serialisable summary: identity, status, seed, scenario, contents and counts; the
    results (President, EV, chambers, governors, legislature seats) only once the election is
    reported (FINAL) — or with ``include_hidden`` for internal tools."""
    el = get_election(session, election_id)
    out = _brief(session, el)
    out["contents"] = _contents(session, el)
    out["apportionment_id"] = el.apportionment_id
    out["district_plan_id"] = el.district_plan_id
    out["ballot_lines"] = int(
        session.scalar(
            select(func.count())
            .select_from(BallotCandidate)
            .join(Race, Race.id == BallotCandidate.race_id)
            .where(Race.election_id == el.id)
        )
        or 0
    )
    out["polls"] = int(
        session.scalar(select(func.count()).select_from(Poll).where(Poll.election_id == el.id)) or 0
    )
    out["campaigns"] = int(
        session.scalar(select(func.count()).select_from(Campaign).where(Campaign.election_id == el.id)) or 0
    )
    out["simulated_at"] = el.simulated_at.isoformat() if el.simulated_at else None
    out["finalized_at"] = el.finalized_at.isoformat() if el.finalized_at else None
    runs = {}
    for kind in (RUN_SETUP, RUN_SIMULATE, RUN_FINALIZE):
        run = latest_run(session, el.id, kind)
        if run is not None:
            runs[kind] = {"id": run.id, "seed": int(run.seed), "duration_s": run.duration_s}
    out["runs"] = runs
    if el.status in REPORTED_STATUSES or include_hidden:
        if el.status in REPORTED_STATUSES:
            out["results"] = _final_results(session, el)
        elif el.status != ElectionStatus.SCHEDULED.value:
            run = latest_run(session, el.id, RUN_SIMULATE)
            sim = loads(run.summary_json) if run is not None else {}
            timeline = sim.get("timeline") or {}
            out["results"] = {
                "hidden": True,
                "turnout_pct": sim.get("turnout_pct"),
                "ballots_cast": sim.get("ballots_cast"),
                "rows": sim.get("rows"),
                "timeline": {k: timeline.get(k) for k in ("events", "unit_rows", "end_clock", "seed")},
            }
    return out


def list_elections(session: Session) -> list[dict[str, Any]]:
    """Every election (chronological) with identity, status and contents."""
    out = []
    for el in session.scalars(select(Election).order_by(Election.election_date, Election.id)):
        d = _brief(session, el)
        d["contents"] = _contents(session, el)
        out.append(d)
    return out


def president_holder(session: Session) -> dict[str, Any] | None:
    """The currently serving President (latest term start), for summaries."""
    row = session.execute(
        select(OfficeHolder, Candidate.full_name)
        .join(Office, Office.id == OfficeHolder.office_id)
        .join(Candidate, Candidate.id == OfficeHolder.candidate_id)
        .where(Office.code == PRESIDENT_OFFICE, OfficeHolder.ended_on.is_(None))
        .order_by(OfficeHolder.term_start.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    oh, name = row
    return {
        "candidate_id": oh.candidate_id,
        "name": name,
        "party": _party_code_map(session).get(oh.party_id) if oh.party_id else None,
        "term_start": oh.term_start.isoformat(),
        "term_end": oh.term_end.isoformat() if oh.term_end else None,
        "start_reason": oh.start_reason,
    }
