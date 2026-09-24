"""Election results read models: lists, summaries and every results page of the UI.

All numbers are SIMULATED results of FICTIONAL races.  Each function returns the standard
election envelope (:meth:`ElectionRef.envelope`): ``election``, ``data_category``,
``results_source`` (``final`` | ``live`` | ``hidden``) and the page payload.  The
hidden-until-reported rule is enforced here: stored results are only read for reported
elections, live results only come from the night service, and hidden elections expose ballots,
candidates, incumbents and electoral geography — never votes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analytics.history import compare_elections, ec_pv_divergence
from app.analytics.metrics import tipping_point_from_frame
from app.analytics.results import ResultsFrameError
from app.core.config import get_constitution
from app.core.constitution import RaceStatus, RaceType
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.elections.seats import INDEPENDENT, chamber_control
from app.models import (
    BallotCandidate,
    ContingentElection,
    Election,
    ElectionResult,
    ElectoralVoteAllocation,
    GeoUnit,
    Municipality,
    MunicipalityLineage,
    Race,
    RaceCall,
    Recount,
    RecountAdjustment,
    ReportingEvent,
    SenateSeat,
    TurnoutResult,
)
from app.services import elections as election_service
from app.services._common import REPORTED_STATUSES, latest_run, loads
from app.services.read._base import (
    SIMULATED,
    ElectionRef,
    all_election_refs,
    cached,
    chunks,
    demo_election_id,
    iso,
    latest_election_id,
    leader_of,
    municipality_maps,
    pct,
    province_maps,
    ref_of,
    require_reported,
    rnd,
    with_votes,
)
from app.services.read._races import FAMILIES, RaceBook, compact_line, holders_on, party_key, share_map
from app.services.read.live import (
    live_municipality_detail,
    live_municipality_rows_many,
    live_race_detail,
    live_state,
    night_available,
)
from app.services.results import results_frame

log = get_logger(__name__)

#: Sortable fields of the province page's municipality table.
MUNICIPALITY_SORT_FIELDS: tuple[str, ...] = (
    "name",
    "population",
    "votes",
    "reporting_pct",
    "margin_pp",
    "swing_pp",
    "turnout_pct",
    "outstanding_est",
)


# =========================================================================== helpers
def family_code(race: str | None, default: str = "PRES") -> str:
    fam = (race or default).upper()
    if fam not in FAMILIES:
        raise ValidationError(f"unknown race family {race!r}; expected one of {sorted(FAMILIES)}")
    return fam


def municipal_types(family: str) -> tuple[RaceType, ...]:
    """Race types whose municipality rows describe a family without double counting (the national
    ``PRES`` race covers every municipality; its province contests would repeat it)."""
    return (RaceType.PRESIDENT,) if family == "PRES" else FAMILIES[family]


def previous_with(session: Session, ref: ElectionRef, types: Iterable[RaceType]) -> ElectionRef | None:
    """The latest reported election before ``ref`` that held races of ``types``."""
    eid = session.scalar(
        select(Election.id)
        .join(Race, Race.election_id == Election.id)
        .where(
            Election.status.in_(list(REPORTED_STATUSES)),
            Election.election_date < ref.election_date,
            Race.race_type.in_([t.value for t in types]),
        )
        .order_by(Election.election_date.desc(), Election.id.desc())
        .limit(1)
    )
    if eid is None:
        return None
    el = session.get(Election, int(eid))
    return None if el is None else ref_of(el)


def lineage_between(session: Session, prev: ElectionRef, curr: ElectionRef) -> pd.DataFrame | None:
    """Municipality lineage between two vintages (``None`` when they share the vintage)."""
    if prev.vintage_id == curr.vintage_id:
        return None
    rows = session.execute(
        select(
            MunicipalityLineage.from_code, MunicipalityLineage.to_code, MunicipalityLineage.population_weight
        ).where(
            MunicipalityLineage.from_vintage_id == prev.vintage_id,
            MunicipalityLineage.to_vintage_id == curr.vintage_id,
        )
    ).all()
    return pd.DataFrame(rows, columns=["from_code", "to_code", "population_weight"])


def stored_frame(
    session: Session, ref: ElectionRef, levels: Sequence[str], types: Sequence[RaceType]
) -> pd.DataFrame:
    """Standard results frame of a reported election (cached; never mutate the result)."""
    require_reported(ref)
    return cached(
        session,
        "frame",
        (*ref.cache_key(), tuple(levels), tuple(t.value for t in types)),
        lambda: results_frame(session, [ref.id], levels=list(levels), race_types=list(types)),
        big=True,
    )


def geo_party_table(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Per geo code of a (single-level) frame: party votes pooled over the races, valid votes,
    eligible, ballots, leader, runner-up and margin."""
    if df.empty:
        return {}
    d = df.assign(party=df["party_code"].map(party_key))
    votes = d.groupby(["geo_code", "party"], sort=True)["votes"].sum()
    per_geo = (
        d.drop_duplicates(["race_code", "geo_code"])
        .groupby("geo_code", sort=True)[["valid_votes", "eligible", "ballots_cast"]]
        .sum()
    )
    names = d.drop_duplicates("geo_code").set_index("geo_code")[["geo_name", "province_code"]]
    out: dict[str, dict[str, Any]] = {}
    for geo, grp in votes.groupby(level=0, sort=True):
        v = {str(p): int(x) for (_g, p), x in grp.items()}
        tot = per_geo.loc[geo]
        lead, run, margin, mv = leader_of(v)
        out[str(geo)] = {
            "name": names.loc[geo, "geo_name"],
            "province_code": names.loc[geo, "province_code"],
            "votes": v,
            "valid_votes": int(tot["valid_votes"]),
            "eligible": int(tot["eligible"]),
            "ballots_cast": int(tot["ballots_cast"]),
            "leader": lead,
            "runner_up": run,
            "margin_pp": margin,
            "margin_votes": mv,
        }
    return out


def _swing_table(
    session: Session, ref: ElectionRef, prev: ElectionRef | None, family: str, level: str
) -> dict[str, dict[str, Any]]:
    """Lineage-aware swing per geo (``app.analytics.history.compare_elections``, family-pooled):
    ``{geo: {"swing": {party: pp}, "winner_prev": party, "flip_status": str}}``."""
    if prev is None:
        return {}
    types = municipal_types(family) if level == "municipality" else FAMILIES[family]

    def build() -> dict[str, dict[str, Any]]:
        p = stored_frame(session, prev, (level,), types)
        c = stored_frame(session, ref, (level,), types)
        if p.empty or c.empty:
            return {}
        try:
            table = compare_elections(
                p, c, level, race_type=types[0], lineage=lineage_between(session, prev, ref), by="family"
            )
        except (ResultsFrameError, ValueError, KeyError) as exc:
            log.warning("swing %s → %s (%s, %s) unavailable: %s", prev.id, ref.id, family, level, exc)
            return {}
        out: dict[str, dict[str, Any]] = {}
        for row in table.itertuples(index=False):
            g = out.setdefault(
                str(row.geo_code),
                {
                    "swing": {},
                    "winner_prev": _nan_none(row.winner_prev),
                    "flip_status": _nan_none(row.flip_status),
                },
            )
            if not pd.isna(row.swing_pp):
                g["swing"][party_key(None if row.party == "_IND" else row.party)] = round(
                    float(row.swing_pp), 3
                )
        return out

    return cached(session, "swing", (*ref.cache_key(), prev.id, family, level), build)


def _nan_none(x: Any) -> Any:
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except (TypeError, ValueError):
        return x
    return x


def _party_colors(book: RaceBook) -> dict[str, str]:
    colors: dict[str, str] = {}
    for lst in book.lines.values():
        for ln in lst:
            colors.setdefault(party_key(ln["party"]), ln["color"])
    return colors


def _pres_race(book: RaceBook) -> Race:
    pres = book.by_code.get("PRES")
    if pres is None:
        raise NotFoundError(f"election {book.ref.id} has no presidential race")
    return pres


# =========================================================================== lists / summary
def _headline(session: Session, ref: ElectionRef) -> dict[str, Any] | None:
    """Short result of a reported election (President, chambers, turnout)."""
    if not ref.reported:
        return None

    def build() -> dict[str, Any]:
        res = election_service.election_summary(session, ref.id).get("results") or {}
        pres = res.get("president") or {}
        house = res.get("house") or {}
        senate = res.get("senate") or {}
        return {
            "president": None
            if not pres
            else {
                "winner": pres.get("winner"),
                "winner_name": pres.get("winner_name"),
                "party": pres.get("winner_party"),
                "electoral_votes": (pres.get("electoral_votes") or {}).get(pres.get("winner")),
                "decided_by": pres.get("decided_by"),
            },
            "house_control": house.get("controlling_party"),
            "senate_control": senate.get("controlling_party"),
            "turnout_pct": rnd(res.get("turnout_pct"), 3),
        }

    return cached(session, "headline", ref.cache_key(), build)


def elections_list(session: Session) -> dict[str, Any]:
    """``GET /api/elections`` — every election with status, contents and (reported) headline."""
    base = {e["id"]: e for e in election_service.list_elections(session)}
    items = []
    for ref in all_election_refs(session):
        b = base.get(ref.id, {})
        items.append(
            {
                **ref.brief(),
                "seed": ref.seed,
                "scenario": b.get("scenario"),
                "contents": b.get("contents"),
                "headline": _headline(session, ref),
                "data_category": SIMULATED,
            }
        )
    return {
        "data_category": SIMULATED,
        "count": len(items),
        "demo_election_id": demo_election_id(session),
        "latest_election_id": latest_election_id(session),
        "elections": items,
    }


def election_detail(session: Session, ref: ElectionRef) -> dict[str, Any]:
    """``GET /api/elections/{id}`` — identity, seed, scenario, contents, counts, runs, previous /
    next election, night availability and (reported only) the result summary."""
    s = election_service.election_summary(session, ref.id)
    skip = {
        "id",
        "year",
        "name",
        "election_type",
        "status",
        "election_date",
        "previous_election_id",
        "reported",
        "data_category",
    }
    nxt = session.scalar(
        select(Election.id)
        .where(Election.election_date > ref.election_date)
        .order_by(Election.election_date, Election.id)
        .limit(1)
    )
    night: dict[str, Any] = {"available": night_available(), "live": ref.live, "clock": None}
    if ref.live:
        state = live_state(session, ref.id)
        night["clock"] = state.get("clock")
    out = ref.envelope(**{k: v for k, v in s.items() if k not in skip})
    out["next_election_id"] = nxt
    out["night"] = night
    out["constitution"] = _thresholds()
    return out


def _thresholds() -> dict[str, Any]:
    c = get_constitution()
    return {
        "electoral_votes": c.electoral_votes,
        "presidential_majority": c.presidential_majority,
        "house_seats": c.house_seats,
        "house_majority": c.house_majority,
        "senate_seats": c.senate_seats,
        "senate_majority": c.senate_majority,
    }


# =========================================================================== President
def _ev_by_line(session: Session, book: RaceBook, pres: Race) -> dict[str, int]:
    require_reported(book.ref)
    key_of = {ln["ballot_candidate_id"]: ln["key"] for ln in book.lines.get(pres.id, [])}
    out: dict[str, int] = {}
    for bid, n in session.execute(
        select(ElectoralVoteAllocation.ballot_candidate_id, func.sum(ElectoralVoteAllocation.electoral_votes))
        .where(ElectoralVoteAllocation.race_id == pres.id)
        .group_by(ElectoralVoteAllocation.ballot_candidate_id)
    ).all():
        out[key_of.get(int(bid), str(bid))] = int(n)
    return out


def _ev_by_province(session: Session, book: RaceBook, pres: Race) -> dict[str, dict[str, int]]:
    """Province code → line key → electoral votes (stored allocations; handles split EV)."""
    require_reported(book.ref)
    key_of = {ln["ballot_candidate_id"]: ln["key"] for ln in book.lines.get(pres.id, [])}
    out: dict[str, dict[str, int]] = {}
    for pid, bid, n in session.execute(
        select(
            ElectoralVoteAllocation.province_id,
            ElectoralVoteAllocation.ballot_candidate_id,
            ElectoralVoteAllocation.electoral_votes,
        ).where(ElectoralVoteAllocation.race_id == pres.id)
    ).all():
        code = book.prov_code.get(pid)
        if code is not None and int(n) > 0:
            k = key_of.get(int(bid), str(bid))
            out.setdefault(code, {})[k] = out.get(code, {}).get(k, 0) + int(n)
    return out


def _contingent(session: Session, book: RaceBook, pres: Race) -> dict[str, Any] | None:
    require_reported(book.ref)
    c = session.scalar(select(ContingentElection).where(ContingentElection.race_id == pres.id))
    if c is None:
        return None
    key_of = {ln["ballot_candidate_id"]: ln["key"] for ln in book.lines.get(pres.id, [])}
    finalists = [key_of.get(int(b), str(b)) for b in loads(c.finalists_json) or []]
    from app.models import Candidate

    vp = session.get(Candidate, c.vp_winner_candidate_id) if c.vp_winner_candidate_id else None
    return {
        "mode": c.mode,
        "outcome": c.outcome,
        "rounds": c.rounds,
        "finalists": finalists,
        "winner": key_of.get(c.winner_ballot_candidate_id) if c.winner_ballot_candidate_id else None,
        "vice_president": None if vp is None else {"candidate_id": vp.id, "name": vp.full_name},
        "ballots": loads(c.ballots_json),
    }


def _province_contest_rows(
    book: RaceBook, ev_alloc: Mapping[str, Mapping[str, int]] | None
) -> list[dict[str, Any]]:
    rows = []
    for r in book.of_type(RaceType.PRESIDENT_PROVINCE):
        v = book.view(r, lines=False)
        votes = book.line_votes(r)
        row = {
            "code": v["province_code"],
            "name": book.provinces.get(v["province_code"], {}).get("name"),
            "race_code": r.code,
            "ev": r.electoral_votes,
            "status": v["status"],
            "winner": v["winner"],
            "winner_party": v["winner_party"],
            "winner_color": v["winner_color"],
            "leader": v["leader"],
            "leader_party": v["leader_party"],
            "leader_color": v["leader_color"],
            "margin_pp": v["margin_pp"],
            "turnout_pct": v["turnout_pct"],
            "reporting_pct": v["reporting_pct"],
            "win_probability": v["win_probability"],
            "pct": share_map(votes),
            "flip_status": v["flip_status"],
            "previous_party": v["previous_party"],
        }
        if ev_alloc is not None:
            row["ev_awarded"] = dict(ev_alloc.get(v["province_code"], {}))
        rows.append(row)
    rows.sort(key=lambda d: book.provinces.get(d["code"], {}).get("sort_order", 99))
    return rows


def _pres_frame_analysis(
    session: Session, book: RaceBook, winner: str | None, ev_by_line: Mapping[str, int]
) -> dict[str, Any]:
    """Tipping point (+ EC bias) and EV/PV divergence of a reported presidential election."""
    ref = book.ref
    frame = stored_frame(session, ref, ("province", "national"), FAMILIES["PRES"])
    ev_by_prov = {r.province_code: int(r.electoral_votes or 0) for r in _pres_specs(book)}
    out: dict[str, Any] = {"tipping_point": None, "divergence": None}
    key = winner or (max(ev_by_line, key=lambda k: ev_by_line[k]) if ev_by_line else None)
    try:
        bias = tipping_point_from_frame(frame, ev_by_prov, key=key, by="line")
        tp = bias.tipping_point
        cum = dict(zip(tp.order, tp.cumulative, strict=True))
        out["tipping_point"] = {
            "key": bias.key,
            "province_code": tp.province,
            "province_name": book.provinces.get(tp.province, {}).get("name"),
            "margin_pp": rnd(tp.margin_pp, 4),
            "cumulative_ev": tp.cumulative_ev,
            "majority": tp.majority,
            "national_margin_pp": rnd(bias.national_margin_pp, 4),
            "ec_bias_pp": rnd(bias.bias_pp, 4),
            "order": [{"province_code": p, "cumulative_ev": int(cum[p])} for p in tp.order],
        }
    except (ResultsFrameError, ValueError, KeyError, IndexError) as exc:
        log.warning("tipping point of election %s unavailable: %s", ref.id, exc)
    pres = book.by_code["PRES"]
    pv = book.line_votes(pres) or {}
    long = pd.DataFrame(
        [
            {
                "election_id": ref.id,
                "year": ref.year,
                "key": k,
                "popular_votes": int(pv.get(k, 0)),
                "electoral_votes": int(ev_by_line.get(k, 0)),
            }
            for k in pv
        ]
    )
    if not long.empty:
        div = ec_pv_divergence(long, majority=get_constitution().presidential_majority)
        if not div.empty:
            d = div.iloc[0].to_dict()
            out["divergence"] = {k: _py(v) for k, v in d.items() if k not in ("election_id", "year")}
    return out


def _py(v: Any) -> Any:
    """NumPy / pandas scalar → Python (NaN → None)."""
    v = _nan_none(v)
    if isinstance(v, np.generic):
        return v.item()
    return v


class _Spec:
    __slots__ = ("electoral_votes", "province_code")

    def __init__(self, province_code: str, electoral_votes: int | None) -> None:
        self.province_code = province_code
        self.electoral_votes = electoral_votes


def _pres_specs(book: RaceBook) -> list[_Spec]:
    return [
        _Spec(book.prov_code[r.province_id], r.electoral_votes)
        for r in book.of_type(RaceType.PRESIDENT_PROVINCE)
        if r.province_id is not None
    ]


def _ticket(ln: Mapping[str, Any]) -> dict[str, Any]:
    """A presidential ticket line: the line plus explicit ``president`` / ``running_mate``."""
    d = dict(ln)
    d["president"] = d.pop("candidate", None)
    return d


def president(session: Session, ref: ElectionRef) -> dict[str, Any]:
    """``GET /api/elections/{id}/president`` — national tickets (running mates, colours, portrait
    keys), popular vote, electoral votes (174 / 88 TO WIN), winner, margins, tipping point,
    EV/PV divergence, contingent election and per-province map data."""

    def build() -> dict[str, Any]:
        book = RaceBook.load(session, ref, types=FAMILIES["PRES"])
        pres = _pres_race(book)
        cons = get_constitution()
        lines = book.lines.get(pres.id, [])
        out: dict[str, Any] = {
            "race_code": pres.code,
            "race_name": pres.name,
            "electoral_votes_total": cons.electoral_votes,
            "majority": cons.presidential_majority,
            "label": f"{cons.presidential_majority} TO WIN",
            "ev_allocation": get_constitution().ev_allocation.value,
        }
        if ref.reported:
            ev = _ev_by_line(session, book, pres)
            ev_prov = _ev_by_province(session, book, pres)
            pv = book.line_votes(pres) or {}
            total = sum(pv.values())
            winner = book.winner_key(pres)
            provinces_won: dict[str, list[str]] = {}
            for code, alloc in ev_prov.items():
                for k in alloc:
                    provinces_won.setdefault(k, []).append(code)
            tickets = []
            for ln in lines:
                k = ln["key"]
                tickets.append(
                    {
                        **_ticket(ln),
                        "votes": int(pv.get(k, 0)),
                        "pct": pct(pv.get(k, 0), total),
                        "electoral_votes": int(ev.get(k, 0)),
                        "provinces_won": sorted(provinces_won.get(k, [])),
                        "winner": k == winner,
                    }
                )
            tickets.sort(key=lambda t: (-t["electoral_votes"], -t["votes"], t["order"]))
            t = book.turnout.get(pres.id, {})
            lead, run, margin_pp, mv = leader_of(pv)
            evs = [*sorted(ev.values(), reverse=True), 0, 0]
            analysis = _pres_frame_analysis(session, book, winner, ev)
            wl = book.line(pres, winner)
            out.update(
                {
                    "status": pres.status,
                    "winner": None
                    if wl is None
                    else {
                        "key": winner,
                        "name": wl["name"],
                        "party": wl["party"],
                        "color": wl["color"],
                        "president": wl["candidate"],
                        "running_mate": wl["running_mate"],
                        "electoral_votes": int(ev.get(winner, 0)),
                    },
                    "decided_by": pres.decided_by,
                    "tickets": tickets,
                    "popular_vote": {
                        "total_valid": total,
                        "ballots_cast": t.get("ballots_cast"),
                        "eligible": t.get("eligible"),
                        "turnout_pct": t.get("turnout_pct"),
                        "leader": lead,
                        "runner_up": run,
                        "margin_votes": mv,
                        "margin_pp": margin_pp,
                        "reporting_pct": 100.0,
                    },
                    "electoral_vote_margin": int(evs[0] - evs[1]),
                    "majority_reached": bool(evs[0] >= cons.presidential_majority),
                    "tipping_point": analysis["tipping_point"],
                    "divergence": analysis["divergence"],
                    "contingent": _contingent(session, book, pres),
                    "provinces": _province_contest_rows(book, ev_prov),
                }
            )
        elif ref.live:
            snap = book.snapshot
            p = snap.get("president") or {}
            pvs = snap.get("popular_vote") or {}
            lt = {t.get("key"): t for t in p.get("tickets") or []}
            proj = {ln.get("key"): ln for ln in pvs.get("lines") or []}
            tickets = []
            for ln in lines:
                k = ln["key"]
                x = lt.get(k, {})
                tickets.append(
                    {
                        **_ticket(ln),
                        "votes": int(x.get("votes", 0) or 0),
                        "pct": rnd(x.get("pct"), 3),
                        "projected_pct": rnd((proj.get(k) or {}).get("projected_pct"), 3),
                        "electoral_votes": int(x.get("ev_decided", 0) or 0),
                        "ev_leading": int(x.get("ev_leading", 0) or 0),
                        "ev_max_possible": x.get("ev_max_possible"),
                        "winner": p.get("winner") == k,
                    }
                )
            tickets.sort(key=lambda t: (-t["electoral_votes"], -t["ev_leading"], -t["votes"], t["order"]))
            wl = book.line(pres, p.get("winner"))
            out.update(
                {
                    "status": p.get("status", RaceStatus.POLLS_CLOSED.value),
                    "winner": None
                    if wl is None
                    else {
                        "key": wl["key"],
                        "name": wl["name"],
                        "party": wl["party"],
                        "color": wl["color"],
                        "president": wl["candidate"],
                        "running_mate": wl["running_mate"],
                        "electoral_votes": int(lt.get(wl["key"], {}).get("ev_decided", 0) or 0),
                    },
                    "decided_by": None,
                    "tickets": tickets,
                    "popular_vote": {
                        "total_valid": pvs.get("counted_valid"),
                        "projected_valid": pvs.get("projected_valid"),
                        "outstanding_ballots_est": pvs.get("outstanding_ballots_est"),
                        "reporting_pct": pvs.get("reporting_pct"),
                    },
                    "ev_decided_total": p.get("ev_decided_total"),
                    "ev_uncalled": p.get("ev_uncalled"),
                    "majority_reached": p.get("winner") is not None,
                    "contingent_likely": bool(p.get("contingent_likely", False)),
                    "tipping_point": None,
                    "divergence": None,
                    "contingent": None,
                    "provinces": _province_contest_rows(book, None),
                    "seq": snap.get("seq"),
                    "clock": snap.get("clock"),
                }
            )
        else:
            out.update(
                {
                    "status": RaceStatus.SCHEDULED.value,
                    "winner": None,
                    "decided_by": None,
                    "tickets": [
                        {**_ticket(ln), "votes": None, "pct": None, "electoral_votes": None, "winner": False}
                        for ln in lines
                    ],
                    "popular_vote": None,
                    # nothing is allocated before the night: 0 EV decided, every EV available
                    "ev_decided_total": 0,
                    "ev_uncalled": sum(
                        int(r.electoral_votes or 0) for r in book.of_type(RaceType.PRESIDENT_PROVINCE)
                    ),
                    "majority_reached": False,
                    "tipping_point": None,
                    "divergence": None,
                    "contingent": None,
                    "provinces": _province_contest_rows(book, None),
                }
            )
        return ref.envelope(**out)

    return build() if ref.live else cached(session, "president", ref.cache_key(), build)


# =========================================================================== Electoral College
def electoral_college(session: Session, ref: ElectionRef) -> dict[str, Any]:
    """``GET /api/elections/{id}/electoral-college`` — EV by ticket with EC efficiency, province
    contests ordered along the winner's path to 88, tipping point, closest province, largest
    victory, EC bias and EV/PV divergence (live: decided / leading / uncalled EV)."""

    def build() -> dict[str, Any]:
        pres_page = president(session, ref)
        cons = get_constitution()
        tickets = pres_page["tickets"]
        provinces = pres_page["provinces"]
        out: dict[str, Any] = {
            "electoral_votes_total": cons.electoral_votes,
            "majority": cons.presidential_majority,
            "label": f"{cons.presidential_majority} TO WIN",
            "status": pres_page["status"],
            "winner": pres_page["winner"],
        }
        if ref.reported:
            from app.analytics.metrics import ec_efficiency

            pv = {t["key"]: t["votes"] for t in tickets}
            ev = {t["key"]: t["electoral_votes"] for t in tickets}
            eff = ec_efficiency(pv, ev).set_index("key")
            tp = pres_page["tipping_point"]
            order = [o["province_code"] for o in (tp or {}).get("order", [])]
            cum = {o["province_code"]: o["cumulative_ev"] for o in (tp or {}).get("order", [])}
            by_code = {p["code"]: p for p in provinces}
            path = []
            for code in order or [p["code"] for p in provinces]:
                p = by_code.get(code)
                if p is None:
                    continue
                path.append(
                    {
                        "code": code,
                        "name": p["name"],
                        "ev": p["ev"],
                        "winner": p["winner"],
                        "winner_party": p["winner_party"],
                        "winner_color": p["winner_color"],
                        "margin_pp": p["margin_pp"],
                        "cumulative_ev": cum.get(code),
                        "is_tipping_point": tp is not None and code == tp["province_code"],
                        "flip_status": p["flip_status"],
                    }
                )
            margins = [(p["margin_pp"], p["code"]) for p in provinces if p["margin_pp"] is not None]
            closest = min(margins) if margins else None
            largest = max(margins) if margins else None
            out.update(
                {
                    "tickets": [
                        {
                            "key": t["key"],
                            "name": t["name"],
                            "party": t["party"],
                            "color": t["color"],
                            "electoral_votes": t["electoral_votes"],
                            "ev_share": rnd(eff.loc[t["key"], "ev_share"], 6)
                            if t["key"] in eff.index
                            else None,
                            "popular_votes": t["votes"],
                            "pv_share": rnd(eff.loc[t["key"], "pv_share"], 6)
                            if t["key"] in eff.index
                            else None,
                            "efficiency_pp": rnd(eff.loc[t["key"], "efficiency_pp"], 4)
                            if t["key"] in eff.index
                            else None,
                            "provinces_won": t.get("provinces_won", []),
                        }
                        for t in tickets
                    ],
                    "path": path,
                    "tipping_point": tp,
                    "closest": None
                    if closest is None
                    else {"province_code": closest[1], "margin_pp": closest[0]},
                    "largest_victory": None
                    if largest is None
                    else {"province_code": largest[1], "margin_pp": largest[0]},
                    "divergence": pres_page["divergence"],
                    "contingent": pres_page["contingent"],
                    "provinces": provinces,
                }
            )
        else:
            out.update(
                {
                    "tickets": [
                        {
                            "key": t["key"],
                            "name": t["name"],
                            "party": t["party"],
                            "color": t["color"],
                            "electoral_votes": t.get("electoral_votes"),
                            "ev_leading": t.get("ev_leading"),
                            "ev_max_possible": t.get("ev_max_possible"),
                            "popular_votes": t.get("votes"),
                        }
                        for t in tickets
                    ],
                    "ev_decided_total": pres_page.get("ev_decided_total"),
                    "ev_uncalled": pres_page.get("ev_uncalled"),
                    "contingent_likely": pres_page.get("contingent_likely"),
                    "provinces": provinces,
                    "path": None,
                    "tipping_point": None,
                }
            )
        return ref.envelope(**out)

    return build() if ref.live else cached(session, "ec", ref.cache_key(), build)


# =========================================================================== provinces
def _province_rows_generic(book: RaceBook, family: str) -> list[dict[str, Any]]:
    by_prov: dict[str, list[Race]] = {}
    for r in book.of_type(*FAMILIES[family]):
        if r.province_id is not None:
            by_prov.setdefault(book.prov_code[r.province_id], []).append(r)
    rows = []
    for code, races in by_prov.items():
        races.sort(key=lambda r: r.code)
        r = races[0]
        v = book.view(r, lines=False)
        rows.append(
            {
                "code": code,
                "name": book.provinces.get(code, {}).get("name"),
                "race_code": r.code,
                "races": [x.code for x in races],
                "ev": None,
                "status": v["status"],
                "winner": v["winner"],
                "winner_party": v["winner_party"],
                "winner_color": v["winner_color"],
                "leader": v["leader"],
                "leader_party": v["leader_party"],
                "leader_color": v["leader_color"],
                "margin_pp": v["margin_pp"],
                "turnout_pct": v["turnout_pct"],
                "reporting_pct": v["reporting_pct"],
                "win_probability": v["win_probability"],
                "pct": share_map(book.line_votes(r)),
                "flip_status": v["flip_status"],
                "previous_party": v["previous_party"],
            }
        )
    rows.sort(key=lambda d: book.provinces.get(d["code"], {}).get("sort_order", 99))
    return rows


def provinces_results(session: Session, ref: ElectionRef, race: str | None = None) -> dict[str, Any]:
    """``GET /api/elections/{id}/provinces?race=PRES|GOV|SEN|PROVLEG`` — one row per province of
    the family's province-wide contest: EV (President), winner / leader, status, margin, turnout,
    reporting, shares by line key and hold / flip against the seat's previous party."""
    family = family_code(race)
    if family in ("HOUSE", "MAYOR", "COUNCIL"):
        raise ValidationError("provinces view needs a province-wide race family (PRES, GOV, SEN, PROVLEG)")

    def build() -> dict[str, Any]:
        book = RaceBook.load(session, ref, types=FAMILIES[family])
        if not book.races:
            raise NotFoundError(f"election {ref.id} has no {family} races")
        if family == "PRES":
            pres = book.by_code.get("PRES")
            ev_prov = _ev_by_province(session, book, pres) if ref.reported and pres is not None else None
            rows = _province_contest_rows(book, ev_prov)
        else:
            rows = _province_rows_generic(book, family)
        return ref.envelope(race=family, colors=_party_colors(book), provinces=rows)

    return build() if ref.live else cached(session, "provinces", (*ref.cache_key(), family), build)


def _municipality_meta(session: Session, ref: ElectionRef) -> dict[str, dict[str, Any]]:
    """code → REAL population / eligible voters of the election's vintage."""
    return cached(
        session,
        "muni-meta",
        (ref.vintage_id,),
        lambda: {
            c: {"population": int(p), "eligible": int(e), "name": n}
            for c, p, e, n in session.execute(
                select(
                    Municipality.cbs_code,
                    Municipality.population,
                    Municipality.eligible_voters_est,
                    Municipality.name,
                ).where(Municipality.vintage_id == ref.vintage_id)
            ).all()
        },
    )


def _final_municipality_rows(
    session: Session, ref: ElectionRef, family: str, colors: Mapping[str, str]
) -> list[dict[str, Any]]:
    types = municipal_types(family)

    def build() -> list[dict[str, Any]]:
        geo = geo_party_table(stored_frame(session, ref, ("municipality",), types))
        prev = previous_with(session, ref, types)
        sw = _swing_table(session, ref, prev, family, "municipality")
        meta = _municipality_meta(session, ref)
        rows = []
        for code, g in geo.items():
            s = sw.get(code, {})
            m = meta.get(code, {})
            lead = g["leader"]
            rows.append(
                {
                    "code": code,
                    "name": g["name"],
                    "province_code": g["province_code"],
                    "population": m.get("population"),
                    "eligible": g["eligible"],
                    "votes": g["valid_votes"],
                    "ballots_cast": g["ballots_cast"],
                    "turnout_pct": pct(g["ballots_cast"], g["eligible"]),
                    "reporting_pct": 100.0,
                    "leader": lead,
                    "leader_party": None if lead == INDEPENDENT else lead,
                    "leader_color": colors.get(lead) if lead else None,
                    "runner_up": g["runner_up"],
                    "margin_pp": g["margin_pp"],
                    "shares": share_map(g["votes"]),
                    "swing_pp": (s.get("swing") or {}).get(lead) if lead else None,
                    "previous_winner": s.get("winner_prev"),
                    "flip_status": s.get("flip_status"),
                    "outstanding_est": 0,
                }
            )
        return rows

    return cached(session, "muni-rows", (*ref.cache_key(), family), build)


def _live_rows(
    session: Session,
    ref: ElectionRef,
    book: RaceBook,
    races: Sequence[Race],
    colors: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Live map rows pooled by party over ``races`` (the night service's per-race municipality
    rows: counted votes, reporting, expected / outstanding ballots, previous winner, swing)."""
    meta = _municipality_meta(session, ref)
    line_party = {ln["key"]: ln["party"] for r in races for ln in book.lines.get(r.id, [])}
    pooled: dict[str, dict[str, Any]] = {}
    local = (RaceType.MAYOR.value, RaceType.MUNICIPAL_COUNCIL.value)
    night_rows = live_municipality_rows_many(
        session, ref.id, [r.code for r in races if r.race_type not in local]
    )
    for r in races:
        if r.race_type in local:
            lr = book.live.get(r.code) or {}
            geo = book.geo(r)
            src = [
                {
                    "code": geo["municipality_code"],
                    "name": geo["municipality_name"],
                    "province_code": geo["province_code"],
                    "votes": lr.get("votes") or {},
                    "reporting_pct": lr.get("reporting_pct"),
                }
            ]
        else:
            src = night_rows.get(r.code, [])
        for m in src:
            code = str(m.get("code"))
            agg = pooled.setdefault(
                code,
                {
                    "votes": {},
                    "expected": 0.0,
                    "outstanding": 0.0,
                    "reporting_w": 0.0,
                    "n": 0,
                    "units_total": 0,
                    "units_reported": 0,
                    "src": m,
                },
            )
            if m.get("units_total") is not None:
                agg["units_total"] += int(m["units_total"])
                agg["units_reported"] += int(m.get("units_reported") or 0)
            for k, v in (m.get("votes") or {}).items():
                p = party_key(line_party.get(k))
                agg["votes"][p] = agg["votes"].get(p, 0) + int(v)
            x = float(m.get("expected_ballots") or 0.0)
            agg["expected"] += x
            agg["outstanding"] += float(m.get("outstanding_ballots_est") or 0.0)
            agg["reporting_w"] += float(m.get("reporting_pct") or 0.0) * (x if x > 0 else 1.0)
            agg["n"] += 1
    rows = []
    for code, agg in pooled.items():
        m = agg["src"]
        info = meta.get(code, {})
        votes = agg["votes"]
        counted = sum(votes.values())
        weight = agg["expected"] if agg["expected"] > 0 else float(agg["n"])
        lead, run, margin, _mv = leader_of(votes)
        flipped = m.get("flipped") if len(races) == 1 else None
        rows.append(
            {
                "code": code,
                "name": m.get("name") or info.get("name"),
                "province_code": m.get("province_code"),
                "population": info.get("population"),
                "eligible": info.get("eligible"),
                "votes": counted,
                "reporting_pct": rnd(agg["reporting_w"] / weight, 3) if weight else 0.0,
                "units_total": agg["units_total"] or m.get("units_total"),
                "units_reported": agg["units_reported"] if agg["units_total"] else m.get("units_reported"),
                "leader": lead,
                "leader_party": None if lead in (None, INDEPENDENT) else lead,
                "leader_color": colors.get(lead) if lead else None,
                "runner_up": run,
                "margin_pp": margin,
                "shares": share_map(votes) if counted else None,
                "turnout_pct": None,
                "swing_pp": rnd(m.get("swing_pct"), 3) if len(races) == 1 else None,
                "previous_winner": m.get("previous_winner_party") if len(races) == 1 else None,
                "flip_status": None if flipped is None else ("flip" if flipped else "hold"),
                "expected_ballots": round(agg["expected"]) if agg["expected"] else None,
                "outstanding_est": round(agg["outstanding"]),
            }
        )
    rows.sort(key=lambda r: r["code"])
    return rows


def municipality_rows(
    session: Session, ref: ElectionRef, race: str | None = None, province: str | None = None
) -> dict[str, Any]:
    """``GET /api/elections/{id}/municipalities?race=&province=`` — map / table rows: leader,
    margin, shares (by party), turnout, reporting, previous winner, swing and outstanding votes.

    ``race`` is a family (``PRES`` default, ``HOUSE``, ``SEN``, ``GOV``, ``PROVLEG``, ``MAYOR``,
    ``COUNCIL``; races of a family are pooled per municipality) or a single race code
    (``HOUSE-NB-07``: rows of that race only, keyed by party)."""
    code = (race or "PRES").upper()
    single = code not in FAMILIES
    if province is not None:
        province = province.upper()
    if single:
        book = RaceBook.load(session, ref, codes=[code])
        if not book.races:
            raise NotFoundError(f"race {code} not found in election {ref.id}")
        family = code
    else:
        family = family_code(code)
        book = RaceBook.load(session, ref, types=municipal_types(family))
        if not book.races:
            raise NotFoundError(f"election {ref.id} has no {family} races")
    colors = _party_colors(book)
    if ref.reported:
        if single:
            r = book.races[0]
            rows = [_breakdown_row_to_table(b, colors) for b in _breakdown(session, book, r, "municipality")]
        else:
            rows = _final_municipality_rows(session, ref, family, colors)
    elif ref.live:
        rows = _live_rows(session, ref, book, book.races, colors)
    else:
        rows = _hidden_municipality_rows(session, ref, book)
    if province is not None:
        rows = [r for r in rows if r.get("province_code") == province]
    return ref.envelope(race=family, keyed_by="party", colors=colors, count=len(rows), municipalities=rows)


def _hidden_municipality_rows(session: Session, ref: ElectionRef, book: RaceBook) -> list[dict[str, Any]]:
    """Rows without results: the municipalities of the races' jurisdictions."""
    munis = municipality_maps(session, ref.vintage_id)
    pids = {r.province_id for r in book.races if r.province_id is not None}
    national = any(r.race_type == RaceType.PRESIDENT.value for r in book.races)
    muni_ids = {r.municipality_id for r in book.races if r.municipality_id is not None}
    prov_code, _ = province_maps(session)
    meta = _municipality_meta(session, ref)
    rows = []
    for mid, m in munis.items():
        if national or m["province_id"] in pids or mid in muni_ids:
            rows.append(
                {
                    "code": m["code"],
                    "name": m["name"],
                    "province_code": prov_code.get(m["province_id"]),
                    "population": meta.get(m["code"], {}).get("population"),
                    "eligible": meta.get(m["code"], {}).get("eligible"),
                    "votes": None,
                    "reporting_pct": None,
                    "leader": None,
                    "margin_pp": None,
                    "shares": None,
                    "turnout_pct": None,
                    "swing_pp": None,
                    "outstanding_est": None,
                }
            )
    rows.sort(key=lambda r: r["code"])
    return rows


# =========================================================================== single-race breakdowns
def _breakdown(
    session: Session, book: RaceBook, race: Race, level: str, *, municipality_id: int | None = None
) -> list[dict[str, Any]]:
    """Stored per-geography results of one race at ``level`` (``municipality`` or ``unit``;
    ``municipality_id`` restricts units to one municipality).  Reported elections only."""
    require_reported(book.ref)
    lines = book.lines.get(race.id, [])
    key_of = {ln["ballot_candidate_id"]: ln["key"] for ln in lines}
    party_of = {ln["key"]: ln["party"] for ln in lines}
    q = select(
        ElectionResult.geo_key,
        ElectionResult.ballot_candidate_id,
        ElectionResult.votes,
        ElectionResult.municipality_id,
        ElectionResult.geo_unit_id,
    ).where(ElectionResult.race_id == race.id, ElectionResult.level == level)
    tq = select(
        TurnoutResult.geo_key,
        TurnoutResult.eligible_voters,
        TurnoutResult.ballots_cast,
        TurnoutResult.valid_votes,
    ).where(TurnoutResult.race_id == race.id, TurnoutResult.level == level)
    if municipality_id is not None:
        q = q.where(ElectionResult.municipality_id == municipality_id)
        tq = tq.where(TurnoutResult.municipality_id == municipality_id)
    geo: dict[str, dict[str, Any]] = {}
    for gk, bid, v, mid, uid in session.execute(q).all():
        g = geo.setdefault(gk, {"votes": {}, "municipality_id": mid, "unit_id": uid})
        k = key_of.get(int(bid), str(bid))
        g["votes"][k] = g["votes"].get(k, 0) + int(v)
    for gk, el, cast, valid in session.execute(tq).all():
        if gk in geo:
            geo[gk].update({"eligible": int(el), "ballots_cast": int(cast), "valid_votes": int(valid)})
    munis = municipality_maps(session, book.ref.vintage_id)
    units: dict[int, tuple[str, str]] = {}
    if level == "unit":
        ids = [int(g["unit_id"]) for g in geo.values() if g["unit_id"] is not None]
        for part in chunks(ids):
            for uid, c, n in session.execute(
                select(GeoUnit.id, GeoUnit.cbs_code, GeoUnit.name).where(GeoUnit.id.in_(part))
            ).all():
                units[int(uid)] = (c, n)
    prov_code, _ = province_maps(session)
    out = []
    for g in geo.values():
        if level == "unit":
            code, name = units.get(int(g["unit_id"] or 0), (None, None))
        else:
            m = munis.get(int(g["municipality_id"] or 0), {})
            code, name = m.get("code"), m.get("name")
        m = munis.get(int(g["municipality_id"] or 0), {})
        lead, run, margin, mv = leader_of(g["votes"])
        out.append(
            {
                "code": code,
                "name": name,
                "municipality_code": m.get("code"),
                "province_code": prov_code.get(m.get("province_id")),
                "votes": g["votes"],
                "pct": share_map(g["votes"]),
                "total_votes": g.get("valid_votes", sum(g["votes"].values())),
                "eligible": g.get("eligible"),
                "ballots_cast": g.get("ballots_cast"),
                "turnout_pct": pct(g.get("ballots_cast"), g.get("eligible")),
                "leader": lead,
                "leader_party": party_of.get(lead) if lead else None,
                "runner_up": run,
                "margin_pp": margin,
                "margin_votes": mv,
            }
        )
    out.sort(key=lambda d: d["code"] or "")
    return out


def _breakdown_row_to_table(b: Mapping[str, Any], colors: Mapping[str, str]) -> dict[str, Any]:
    party = party_key(b.get("leader_party")) if b.get("leader") else None
    votes: dict[str, int] = {}
    return {
        "code": b["code"],
        "name": b["name"],
        "province_code": b["province_code"],
        "votes": b["total_votes"],
        "eligible": b["eligible"],
        "ballots_cast": b["ballots_cast"],
        "turnout_pct": b["turnout_pct"],
        "reporting_pct": 100.0,
        "leader": b["leader"],
        "leader_party": b.get("leader_party"),
        "leader_color": colors.get(party) if party else None,
        "runner_up": b["runner_up"],
        "margin_pp": b["margin_pp"],
        "shares": b["pct"] or share_map(votes),
        "swing_pp": None,
        "outstanding_est": 0,
    }


# =========================================================================== province page
def province_page(
    session: Session,
    ref: ElectionRef,
    code: str,
    *,
    sort: str = "population",
    order: str = "desc",
) -> dict[str, Any]:
    """``GET /api/elections/{id}/provinces/{code}`` — the province page: presidential result (or
    the province's headline race in a midterm), previous result and swing, municipality table
    (sortable), largest remaining municipalities, outstanding vote estimate and the province's
    House, Senate, governor and legislature races."""
    code = code.upper()
    _ids, pmap = province_maps(session)
    if code not in pmap:
        raise NotFoundError(f"province {code!r} not found")
    if sort not in MUNICIPALITY_SORT_FIELDS:
        raise ValidationError(f"sort must be one of {list(MUNICIPALITY_SORT_FIELDS)}")
    pid = pmap[code]["id"]
    types = (
        RaceType.PRESIDENT_PROVINCE,
        RaceType.HOUSE,
        RaceType.SENATE,
        RaceType.GOVERNOR,
        RaceType.PROVINCIAL_LEGISLATURE,
    )
    snapshot = None
    if ref.live:
        from app.services.read.live import live_snapshot

        snapshot = live_snapshot(session, ref.id)
    book = RaceBook.load(session, ref, types=types, snapshot=snapshot)
    book.races = [r for r in book.races if r.province_id == pid]
    colors = _party_colors(book)
    by_type = {t: [r for r in book.races if r.race_type == t.value] for t in types}
    headline_type, family = next(
        (
            (t, f)
            for t, f in (
                (RaceType.PRESIDENT_PROVINCE, "PRES"),
                (RaceType.GOVERNOR, "GOV"),
                (RaceType.SENATE, "SEN"),
            )
            if by_type[t]
        ),
        (None, None),
    )
    headline = by_type[headline_type][0] if headline_type is not None else None
    head_view = book.view(headline) if headline is not None else None
    previous = None
    swing = None
    munis: list[dict[str, Any]] = []
    largest_remaining: list[dict[str, Any]] = []
    outstanding = None
    if headline is not None:
        prev_ref = previous_with(session, ref, FAMILIES[family])
        if prev_ref is not None and (ref.reported or ref.live):
            pbook = RaceBook.load(session, prev_ref, codes=[headline.code])
            if pbook.races:
                pv = pbook.view(pbook.races[0])
                previous = {
                    "election_id": prev_ref.id,
                    "year": prev_ref.year,
                    "race_code": headline.code,
                    "winner": pv["winner"],
                    "winner_name": pv["winner_name"],
                    "winner_party": pv["winner_party"],
                    "margin_pp": pv["margin_pp"],
                    "turnout_pct": pv["turnout_pct"],
                    "pct_by_party": _party_pct(pv["lines"]),
                }
                if ref.reported:
                    swing = (
                        _swing_table(session, ref, prev_ref, family, "province").get(code, {}).get("swing")
                    )
                elif head_view is not None:
                    now = _party_pct(head_view["lines"])
                    if now:
                        swing = {
                            p: round(now.get(p, 0.0) - previous["pct_by_party"].get(p, 0.0), 3)
                            for p in sorted(set(now) | set(previous["pct_by_party"]))
                        }
        if ref.reported:
            rows = _final_municipality_rows(session, ref, family, colors)
            munis = [dict(r) for r in rows if r["province_code"] == code]
            outstanding = {"votes_est": 0, "reporting_pct": 100.0}
        elif ref.live:
            munis = _live_rows(session, ref, book, [headline], colors)
            largest_remaining = sorted(munis, key=lambda r: -(r["outstanding_est"] or 0))[:10]
            outstanding = {
                "votes_est": int(sum(r["outstanding_est"] or 0 for r in munis)),
                "expected_ballots": int(sum(r["expected_ballots"] or 0 for r in munis)),
                "reporting_pct": head_view["reporting_pct"] if head_view else None,
                "basis": "pre-election expectation of ballots not yet counted (never the hidden result)",
            }
        else:
            munis = [
                dict(r) for r in _hidden_municipality_rows(session, ref, book) if r["province_code"] == code
            ]
    reverse = order.lower() != "asc"
    munis.sort(key=lambda r: _sort_key(r, sort), reverse=reverse)
    races = {
        "house": [
            book.view(r, top=2, compact=True) for r in sorted(by_type[RaceType.HOUSE], key=lambda r: r.code)
        ],
        "senate": [
            book.view(r, compact=True) for r in sorted(by_type[RaceType.SENATE], key=lambda r: r.code)
        ],
        "governor": book.view(by_type[RaceType.GOVERNOR][0], compact=True)
        if by_type[RaceType.GOVERNOR]
        else None,
        "legislature": book.view(by_type[RaceType.PROVINCIAL_LEGISLATURE][0], compact=True)
        if by_type[RaceType.PROVINCIAL_LEGISLATURE]
        else None,
    }
    return ref.envelope(
        province={
            "code": code,
            "name": pmap[code]["name"],
            "electoral_votes": headline.electoral_votes
            if headline is not None and family == "PRES"
            else None,
        },
        race=family,
        colors=colors,
        headline=head_view,
        previous=previous,
        swing=swing,
        municipalities={
            "sort": sort,
            "order": "desc" if reverse else "asc",
            "sort_fields": list(MUNICIPALITY_SORT_FIELDS),
            "rows": munis,
        },
        largest_remaining=largest_remaining,
        outstanding=outstanding,
        races=races,
    )


def _party_pct(lines: Sequence[Mapping[str, Any]] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for ln in lines or []:
        if ln.get("pct") is None:
            continue
        p = party_key(ln.get("party"))
        out[p] = round(out.get(p, 0.0) + float(ln["pct"]), 3)
    return out


def _sort_key(row: Mapping[str, Any], field: str) -> Any:
    v = row.get(field)
    if field == "name":
        return str(v or "")
    return float("-inf") if v is None else float(v)


# =========================================================================== municipality page
_TYPE_ORDER = {
    RaceType.PRESIDENT.value: 0,
    RaceType.PRESIDENT_PROVINCE.value: 1,
    RaceType.HOUSE.value: 2,
    RaceType.SENATE.value: 3,
    RaceType.GOVERNOR.value: 4,
    RaceType.PROVINCIAL_LEGISLATURE.value: 5,
    RaceType.MAYOR.value: 6,
    RaceType.MUNICIPAL_COUNCIL.value: 7,
}


def _races_touching(session: Session, ref: ElectionRef, m: Municipality) -> list[str]:
    """Codes of the election's races whose jurisdiction includes municipality ``m``."""
    from app.models import DistrictMunicipality

    dist_ids = (
        [
            int(d)
            for (d,) in session.execute(
                select(DistrictMunicipality.district_id)
                .join(Race, Race.district_id == DistrictMunicipality.district_id)
                .where(DistrictMunicipality.municipality_id == m.id, Race.election_id == ref.id)
            ).all()
        ]
        if ref.district_plan_id is not None
        else []
    )
    q = select(Race.code, Race.race_type).where(
        Race.election_id == ref.id,
        (Race.race_type == RaceType.PRESIDENT.value)
        | (
            (Race.province_id == m.province_id)
            & (Race.race_type != RaceType.HOUSE.value)
            & Race.municipality_id.is_(None)
        )
        | (Race.municipality_id == m.id)
        | (Race.district_id.in_(dist_ids) if dist_ids else False),
    )
    rows = session.execute(q).all()
    rows.sort(key=lambda r: (_TYPE_ORDER.get(r[1], 9), r[0]))
    return [c for c, _t in rows]


def municipality_page(
    session: Session, ref: ElectionRef, code: str, *, race: str | None = None
) -> dict[str, Any]:
    """``GET /api/elections/{id}/municipalities/{code}?race=`` — every race touching the
    municipality with its municipal result (House: the district fragment), and the precinct
    (CBS neighbourhood) table of ``race`` (default: the first race, usually ``PRES``)."""
    m = session.scalar(
        select(Municipality).where(
            Municipality.vintage_id == ref.vintage_id, Municipality.cbs_code == code.upper()
        )
    )
    if m is None:
        raise NotFoundError(f"municipality {code!r} not found")
    codes = _races_touching(session, ref, m)
    book = RaceBook.load(session, ref, codes=codes)
    order = {c: i for i, c in enumerate(codes)}
    book.races.sort(key=lambda r: order.get(r.code, 999))
    prov_code, pmap = province_maps(session)
    races_out: list[dict[str, Any]] = []
    precinct_race = (race or (codes[0] if codes else "")).upper()
    precincts: list[dict[str, Any]] = []
    events: list[dict[str, Any]] | None = None
    live_detail: dict[str, Any] = {}
    if ref.live:
        live_detail = live_municipality_detail(session, ref.id, m.cbs_code)
        events = live_detail.get("events")
    live_by_race = {str(r.get("key")): r for r in live_detail.get("races") or []}
    for r in book.races:
        v = book.view(r, lines=False)
        lines = book.lines.get(r.id, [])
        municipal: dict[str, Any] | None = None
        if ref.reported:
            rows = _breakdown(session, book, r, "municipality", municipality_id=m.id)
            b = next((x for x in rows if x["code"] == m.cbs_code), None)
            if b is not None:
                municipal = {
                    "lines": [
                        compact_line(x)
                        for x in with_votes(
                            lines, b["votes"], winner=b["leader"] if b["margin_votes"] else None
                        )
                    ],
                    "leader": b["leader"],
                    "leader_party": b["leader_party"],
                    "margin_pp": b["margin_pp"],
                    "total_votes": b["total_votes"],
                    "turnout_pct": b["turnout_pct"],
                    "reporting_pct": 100.0,
                }
        elif ref.live:
            lr = live_by_race.get(r.code)
            if lr is not None:
                vv = {k: int(x) for k, x in (lr.get("votes") or {}).items()}
                lead, _run, margin, _mv = leader_of(vv)
                municipal = {
                    "lines": [compact_line(x) for x in with_votes(lines, vv)],
                    "leader": lr.get("leader", lead),
                    "leader_party": book.line_party(r, lr.get("leader", lead)),
                    "margin_pp": margin,
                    "total_votes": sum(vv.values()),
                    "turnout_pct": None,
                    "reporting_pct": rnd(lr.get("reporting_pct"), 3),
                }
        else:
            municipal = {
                "lines": [compact_line(x) for x in with_votes(lines, None)],
                "leader": None,
                "margin_pp": None,
                "total_votes": None,
                "turnout_pct": None,
                "reporting_pct": None,
            }
        if r.seats and r.seats > 1 and "seats_won" in v:
            municipal = {**(municipal or {}), "seats_won": v["seats_won"]}
        races_out.append({**v, "municipal": municipal})
    if ref.reported and precinct_race:
        pr = book.by_code.get(precinct_race)
        if pr is None:
            raise NotFoundError(f"race {precinct_race} does not include municipality {m.cbs_code}")
        unit_rows = _breakdown(session, book, pr, "unit", municipality_id=m.id)
        party_color = {ln["key"]: ln["color"] for ln in book.lines.get(pr.id, [])}
        precincts = [
            {
                **u,
                "leader_color": party_color.get(u["leader"]) if u["leader"] else None,
                "municipality_code": None,
            }
            for u in unit_rows
        ]
        for p in precincts:
            p.pop("municipality_code", None)
    return ref.envelope(
        municipality={
            "code": m.cbs_code,
            "name": m.name,
            "province_code": prov_code.get(m.province_id),
            "province_name": pmap.get(prov_code.get(m.province_id), {}).get("name"),
            "population": m.population,
            "eligible_voters_est": m.eligible_voters_est,
            "reporting_pct": 100.0
            if ref.reported
            else (rnd(live_detail.get("reporting_pct"), 3) if ref.live else None),
            "units_total": m.unit_count,
            "units_reported": live_detail.get("units_reported") if ref.live else None,
        },
        provenance={"municipality": "REAL", "results": SIMULATED},
        races=races_out,
        precinct_race=precinct_race or None,
        precincts=precincts,
        precincts_available=bool(ref.reported),
        events=events,
    )


# =========================================================================== House
def _composition(parties: Iterable[str | None]) -> dict[str, int]:
    out: dict[str, int] = {}
    for p in parties:
        k = party_key(p)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def _control(comp: Mapping[str, int], total: int, majority: int) -> dict[str, Any]:
    c = chamber_control(comp, total, majority)
    return {
        "controlling_party": c.controlling_party,
        "largest_party": c.largest_party,
        "largest_seats": c.largest_seats,
        "seats_short": c.seats_short,
        "coalition_hints": [list(h) for h in c.coalition_hints],
        "label": c.label,
    }


def _race_row(book: RaceBook, r: Race, top: int = 3) -> dict[str, Any]:
    """Compact row of one race for lists of many races (House districts, mayors)."""
    v = book.view(r, top=top, compact=True)
    inc = v["incumbent"]
    return {
        "race_code": r.code,
        "province_code": v["province_code"],
        "district_code": v["district_code"],
        "municipality_code": v["municipality_code"],
        "status": v["status"],
        "winner": v["winner"],
        "winner_name": v["winner_name"],
        "winner_party": v["winner_party"],
        "winner_color": v["winner_color"],
        "leader": v["leader"],
        "leader_name": v["leader_name"],
        "leader_party": v["leader_party"],
        "leader_color": v["leader_color"],
        "margin_pp": v["margin_pp"],
        "reporting_pct": v["reporting_pct"],
        "turnout_pct": v["turnout_pct"],
        "win_probability": v["win_probability"],
        "incumbent": None if inc is None else {k: inc[k] for k in ("name", "party", "running")},
        "open_seat": v["open_seat"],
        "previous_party": v["previous_party"],
        "flip_status": v["flip_status"],
        "top": [
            {k: ln[k] for k in ("key", "name", "party", "color", "votes", "pct", "winner", "incumbent")}
            for ln in v["lines"]
        ],
        "_district_name": v["district_name"],
        "_municipality_name": v["municipality_name"],
    }


def _district_row(book: RaceBook, r: Race) -> dict[str, Any]:
    row = _race_row(book, r)
    name = row.pop("_district_name")
    row.pop("_municipality_name")
    return {"code": row["district_code"], "name": name, **row}


def house(session: Session, ref: ElectionRef) -> dict[str, Any]:
    """``GET /api/elections/{id}/house`` — seat counter by party (won / called / leading /
    previous / net change; 76 FOR CONTROL), control, the House popular vote and district rows."""

    def build() -> dict[str, Any]:
        book = RaceBook.load(session, ref, types=[RaceType.HOUSE])
        if not book.races:
            raise NotFoundError(f"election {ref.id} has no House races")
        cons = get_constitution()
        colors = _party_colors(book)
        prev_parties = [book.previous_party.get(r.id) for r in book.races]
        previous = _composition(p for p in prev_parties if p is not None)
        previous_vacant = sum(1 for p in prev_parties if p is None)
        rows = [_district_row(book, r) for r in sorted(book.races, key=lambda r: r.code)]
        by_party: dict[str, dict[str, Any]] = {}

        def slot(p: str) -> dict[str, Any]:
            return by_party.setdefault(
                p,
                {
                    "party": p,
                    "color": colors.get(p),
                    "won": 0,
                    "called": 0,
                    "leading": 0,
                    "total": 0,
                    "previous": previous.get(p, 0),
                    "net_change": 0,
                    "votes": None,
                    "vote_pct": None,
                },
            )

        for p in previous:
            slot(p)
        control = None
        popular: dict[str, int] | None = None
        if ref.reported:
            popular = {}
            for r in book.races:
                votes = book.line_votes(r) or {}
                for ln in book.lines.get(r.id, []):
                    p = party_key(ln["party"])
                    popular[p] = popular.get(p, 0) + int(votes.get(ln["key"], 0))
            for p in popular:
                slot(p)
            comp = _composition(row["winner_party"] for row in rows if row["winner"] is not None)
            for p, n in comp.items():
                s = slot(p)
                s["won"] = s["called"] = s["total"] = n
            control = _control(comp, cons.house_seats, cons.house_majority)
        elif ref.live:
            h = book.snapshot.get("house") or {}
            for x in h.get("by_party") or []:
                s = slot(party_key(x.get("party")))
                s["called"] = int(x.get("called", 0) or 0)
                s["leading"] = int(x.get("leading", 0) or 0)
                s["total"] = int(x.get("total", s["called"] + s["leading"]) or 0)
                s["color"] = s["color"] or x.get("color")
            control = {"controlling_party": h.get("control"), "control_at_seq": h.get("control_at_seq")}
        for s in by_party.values():
            s["net_change"] = (s["total"] - s["previous"]) if not ref.hidden else None
            if ref.hidden:
                s["won"] = s["called"] = s["leading"] = s["total"] = None
            if popular is not None:
                tot = sum(popular.values())
                s["votes"] = popular.get(s["party"], 0)
                s["vote_pct"] = pct(s["votes"], tot)
        parties = sorted(
            by_party.values(), key=lambda s: (-(s["total"] or 0), -(s["previous"] or 0), s["party"])
        )
        status_counts: dict[str, int] = {}
        for row in rows:
            status_counts[row["status"]] = status_counts.get(row["status"], 0) + 1
        return ref.envelope(
            seats_total=cons.house_seats,
            majority=cons.house_majority,
            label=f"{cons.house_majority} FOR CONTROL",
            races=len(rows),
            called=sum(1 for row in rows if row["winner"] is not None),
            status_counts=status_counts,
            by_party=parties,
            previous_composition=previous,
            previous_vacant=previous_vacant,
            control=control,
            flips=sum(1 for row in rows if row["flip_status"] == "flip"),
            districts=rows,
        )

    return build() if ref.live else cached(session, "house", ref.cache_key(), build)


def house_district(session: Session, ref: ElectionRef, code: str) -> dict[str, Any]:
    """``GET /api/elections/{id}/house/{district}`` — the district's race (all lines), municipal
    breakdown, district statistics and history of the district code."""
    from app.models import HouseDistrict
    from app.services.read.districts import house_history

    dcode = code.upper().removeprefix("HOUSE-")
    detail = race_detail(session, ref, f"HOUSE-{dcode}")
    d = (
        session.scalar(
            select(HouseDistrict).where(
                HouseDistrict.plan_id == ref.district_plan_id, HouseDistrict.code == dcode
            )
        )
        if ref.district_plan_id is not None
        else None
    )
    detail["district"] = (
        None
        if d is None
        else {
            "code": d.code,
            "name": d.name,
            "number": d.number,
            "population": d.population,
            "eligible_voters_est": d.eligible_voters_est,
            "deviation_pct": rnd(d.deviation_pct, 3),
            "area_km2": rnd(d.area_km2, 2),
            "urban_share": rnd(d.urban_share, 4),
            "municipality_count": d.n_municipalities,
            "unit_count": d.n_units,
            "data_category": "FICTIONAL",
        }
    )
    detail["history"] = [
        h for h in house_history(session, dcode) if h["election_id"] != ref.id or ref.reported
    ]
    return detail


# =========================================================================== Senate
def senate(session: Session, ref: ElectionRef) -> dict[str, Any]:
    """``GET /api/elections/{id}/senate`` — all 24 seats (contested with candidates, leader and
    projected winner, or not up with the holdover), current vs projected composition, control
    (13 FOR CONTROL) and hold / flip per contested seat."""

    def build() -> dict[str, Any]:
        from app.services.runtime import RUN_SETUP

        book = RaceBook.load(session, ref, types=[RaceType.SENATE])
        cons = get_constitution()
        colors = _party_colors(book)
        setup = latest_run(session, ref.id, RUN_SETUP)
        holdover: dict[str, str | None] = (
            dict(loads(setup.summary_json).get("holdover_senate", {})) if setup else {}
        )
        seats = session.scalars(select(SenateSeat)).all()
        prov_code, pmap = province_maps(session)
        by_seat = {r.senate_seat_id: r for r in book.races if r.senate_seat_id is not None}
        holders = holders_on(session, [s.code for s in seats if s.id not in by_seat], ref.election_date)
        rows = []
        current: list[str | None] = []
        projected: list[str | None] = []
        tallies: dict[str, dict[str, Any]] = {}

        def slot(p: str) -> dict[str, Any]:
            return tallies.setdefault(
                p,
                {
                    "party": p,
                    "color": colors.get(p),
                    "holdover": 0,
                    "won": 0,
                    "called": 0,
                    "leading": 0,
                    "total_decided": 0,
                    "total_projected": 0,
                    "current": 0,
                },
            )

        for s in sorted(seats, key=lambda s: (pmap[prov_code[s.province_id]]["sort_order"], s.seat_number)):
            race = by_seat.get(s.id)
            base = {
                "code": s.code,
                "province_code": prov_code[s.province_id],
                "province_name": pmap[prov_code[s.province_id]]["name"],
                "seat_number": s.seat_number,
                "senate_class": s.senate_class,
            }
            if race is None:
                party = holdover.get(s.code)
                h = holders.get(s.code)
                if party is None and h is not None:
                    party = h.get("party")
                current.append(party)
                projected.append(party)
                sl = slot(party_key(party))
                sl["holdover"] += 1
                sl["current"] += 1
                rows.append({**base, "up": False, "holder": h, "party": party, "race": None})
                continue
            v = book.view(race, compact=True)
            prev = v["previous_party"]
            if prev is not None:
                current.append(prev)
                slot(party_key(prev))["current"] += 1
            if v["winner"] is not None:
                projected.append(v["winner_party"])
                s_ = slot(party_key(v["winner_party"]))
                s_["called"] += 1
                if ref.reported:
                    s_["won"] += 1
            elif ref.live and v["leader"] is not None:
                slot(party_key(v["leader_party"]))["leading"] += 1
            rows.append(
                {
                    **base,
                    "up": True,
                    "is_special": v["is_special"],
                    "holder": v["incumbent"],
                    "party": prev,
                    "race": v,
                    "projected_winner": v["winner"],
                    "projected_party": v["winner_party"],
                    "flip_status": v["flip_status"],
                }
            )
        for sl in tallies.values():
            sl["total_decided"] = sl["holdover"] + sl["called"]
            sl["total_projected"] = sl["total_decided"] + sl["leading"]
            if ref.hidden:
                sl["won"] = sl["called"] = sl["leading"] = None
                sl["total_decided"] = sl["holdover"]
                sl["total_projected"] = None
        cur_comp = _composition(p for p in current if p is not None)
        proj_comp = _composition(p for p in projected if p is not None) if not ref.hidden else None
        control = None
        if ref.reported and proj_comp is not None:
            control = _control(proj_comp, cons.senate_seats, cons.senate_majority)
        elif ref.live:
            sn = book.snapshot.get("senate") or {}
            control = {"controlling_party": sn.get("control"), "control_at_seq": sn.get("control_at_seq")}
        return ref.envelope(
            seats_total=cons.senate_seats,
            majority=cons.senate_majority,
            label=f"{cons.senate_majority} FOR CONTROL",
            up=len(by_seat),
            not_up=len(seats) - len(by_seat),
            classes_up=sorted({s.senate_class for s in seats if s.id in by_seat}),
            by_party=sorted(
                tallies.values(),
                key=lambda t: (-(t["total_projected"] or t["total_decided"] or 0), t["party"]),
            ),
            composition={"current": cur_comp, "projected": proj_comp},
            control=control,
            flips=sum(1 for r in rows if r.get("flip_status") == "flip"),
            seats=rows,
        )

    return build() if ref.live else cached(session, "senate", ref.cache_key(), build)


# =========================================================================== governors / mayors
def governors(session: Session, ref: ElectionRef) -> dict[str, Any]:
    """``GET /api/elections/{id}/governors`` — governor races (tickets with lieutenant governors)."""

    def build() -> dict[str, Any]:
        book = RaceBook.load(session, ref, types=[RaceType.GOVERNOR])
        rows = [book.view(r, compact=True) for r in book.races]
        rows.sort(key=lambda v: book.provinces.get(v["province_code"], {}).get("sort_order", 99))
        won = _composition(v["winner_party"] for v in rows if v["winner"] is not None)
        previous = _composition(v["previous_party"] for v in rows if v["previous_party"] is not None)
        return ref.envelope(
            races=len(rows),
            colors=_party_colors(book),
            by_party={"won": won if not ref.hidden else None, "previous": previous},
            flips=sum(1 for v in rows if v["flip_status"] == "flip"),
            governors=rows,
        )

    return build() if ref.live else cached(session, "governors", ref.cache_key(), build)


def mayors(session: Session, ref: ElectionRef, province: str | None = None) -> dict[str, Any]:
    """``GET /api/elections/{id}/mayors?province=`` — mayor races (compact: top three lines)."""

    def build() -> list[dict[str, Any]]:
        book = RaceBook.load(session, ref, types=[RaceType.MAYOR])
        rows = []
        for r in sorted(book.races, key=lambda r: r.code):
            row = _race_row(book, r, top=2)
            row.pop("_district_name")
            name = row.pop("_municipality_name")
            rows.append({"code": row["municipality_code"], "name": name, **row})
        return rows

    rows = build() if ref.live else cached(session, "mayors", ref.cache_key(), build)
    if province is not None:
        rows = [r for r in rows if r["province_code"] == province.upper()]
    won = _composition(v["winner_party"] for v in rows if v["winner"] is not None)
    return ref.envelope(
        races=len(rows),
        by_party={"won": won if not ref.hidden else None},
        flips=sum(1 for v in rows if v["flip_status"] == "flip"),
        mayors=rows,
    )


# =========================================================================== race detail
def _race_calls(
    session: Session,
    ref: ElectionRef,
    race_ids: Sequence[int] | None,
    *,
    evidence: bool,
    upto_seq: int | None = None,
) -> list[dict[str, Any]]:
    """Stored race calls (reported elections; or, with ``upto_seq``, a live night's calls up to
    the event it has revealed — the night service persists them as they are made)."""
    if upto_seq is None:
        require_reported(ref)
    q = (
        select(RaceCall, Race.code, Race.race_type, Race.name, BallotCandidate)
        .join(Race, Race.id == RaceCall.race_id)
        .outerjoin(BallotCandidate, BallotCandidate.id == RaceCall.ballot_candidate_id)
        .where(RaceCall.election_id == ref.id)
        .order_by(RaceCall.seq, RaceCall.id)
    )
    if upto_seq is not None:
        q = q.where(RaceCall.seq <= int(upto_seq))
    if race_ids is not None:
        q = q.where(RaceCall.race_id.in_(list(race_ids)))
    out = []
    for c, code, rt, name, b in session.execute(q).all():
        d = {
            "id": c.id,
            "race_code": code,
            "race_type": rt,
            "race_name": name,
            "status": c.status,
            "line_key": None if b is None else b.line_key,
            "candidate": None if b is None else b.ballot_name,
            "party": None if b is None else b.party_code_snapshot,
            "color": None if b is None else b.party_color_snapshot,
            "seq": c.seq,
            "sim_time_s": rnd(c.sim_time_s, 1),
            "called_at": iso(c.called_at),
            "clock": c.called_at.strftime("%H:%M") if c.called_at else None,
            "reporting_pct": rnd(c.reporting_pct, 3),
            "margin_pct": rnd(c.leader_margin_pct, 4),
            "win_probability": rnd(c.win_probability, 6),
            "is_manual": bool(c.is_manual),
            "override_reason": c.override_reason,
            "superseded": bool(c.superseded),
        }
        if evidence:
            d["evidence"] = loads(c.evidence_json)
        out.append(d)
    return out


def _recounts(session: Session, book: RaceBook, race: Race) -> list[dict[str, Any]]:
    require_reported(book.ref)
    key_of = {ln["ballot_candidate_id"]: ln["key"] for ln in book.lines.get(race.id, [])}
    out = []
    for rc in session.scalars(select(Recount).where(Recount.race_id == race.id).order_by(Recount.id)):
        adj = session.scalars(select(RecountAdjustment).where(RecountAdjustment.recount_id == rc.id)).all()
        net: dict[str, int] = {}
        for a in adj:
            k = key_of.get(a.ballot_candidate_id, a.pile) if a.ballot_candidate_id is not None else a.pile
            net[k] = net.get(k, 0) + int(a.delta)
        out.append(
            {
                "id": rc.id,
                "reason": rc.reason,
                "threshold_pct": rnd(rc.threshold_pct, 4),
                "margin_before_votes": rc.margin_before_votes,
                "margin_before_pct": rnd(rc.margin_before_pct, 4),
                "margin_after_votes": rc.margin_after_votes,
                "margin_after_pct": rnd(rc.margin_after_pct, 4),
                "status": rc.status,
                "outcome_changed": bool(rc.outcome_changed),
                "seed": int(rc.seed),
                "adjustments": len(adj),
                "net_change": net,
                "audit": [
                    {
                        "line_key": key_of.get(a.ballot_candidate_id)
                        if a.ballot_candidate_id is not None
                        else None,
                        "pile": a.pile,
                        "votes_before": a.votes_before,
                        "votes_after": a.votes_after,
                        "delta": a.delta,
                        "reason": a.reason,
                    }
                    for a in adj[:200]
                ],
            }
        )
    return out


def race_detail(session: Session, ref: ElectionRef, code: str) -> dict[str, Any]:
    """``GET /api/elections/{id}/races/{race}`` — any race: all lines (candidates, running mates,
    votes, pct), status, winner, margin, turnout, incumbent, flip, previous race, call history
    (with evidence), recount audit, municipal breakdown; the President adds EV, the contingent
    election and the province contests; councils add D'Hondt seats.  LIVE: the night service's
    current decision, call history and lead changes."""
    code = code.upper()

    def build() -> dict[str, Any]:
        types = FAMILIES["PRES"] if code == "PRES" else None
        book = RaceBook.load(session, ref, codes=None if types else [code], types=types)
        race = book.by_code.get(code)
        if race is None:
            raise NotFoundError(f"race {code} not found in election {ref.id}")
        v = book.view(race)
        extra: dict[str, Any] = {"calls": [], "recounts": [], "municipalities": None, "previous_race": None}
        if race.previous_race_id is not None:
            prev = session.get(Race, race.previous_race_id)
            pel = session.get(Election, prev.election_id) if prev is not None else None
            if prev is not None and pel is not None and pel.status in REPORTED_STATUSES:
                w = (
                    session.get(BallotCandidate, prev.winner_ballot_candidate_id)
                    if prev.winner_ballot_candidate_id
                    else None
                )
                extra["previous_race"] = {
                    "election_id": pel.id,
                    "year": pel.year,
                    "code": prev.code,
                    "winner": None if w is None else w.line_key,
                    "winner_name": None if w is None else w.ballot_name,
                    "winner_party": None if w is None else w.party_code_snapshot,
                    "margin_pp": rnd(prev.margin_pct, 4),
                    "turnout_pct": rnd(prev.turnout_pct, 3),
                }
        if ref.reported:
            extra["calls"] = _race_calls(session, ref, [race.id], evidence=True)
            extra["recounts"] = _recounts(session, book, race)
            extra["municipalities"] = _breakdown(session, book, race, "municipality")
            if race.race_type == RaceType.PRESIDENT.value:
                extra["electoral_votes"] = _ev_by_line(session, book, race)
                extra["contingent"] = _contingent(session, book, race)
                extra["provinces"] = _province_contest_rows(book, _ev_by_province(session, book, race))
        elif ref.live:
            night = live_race_detail(session, ref.id, code)
            extra["decision"] = night.get("decision")
            extra["calls"] = night.get("history") or []
            extra["lead_changes"] = night.get("lead_changes") or []
            extra["municipalities"] = night.get("municipalities")
            if race.race_type == RaceType.PRESIDENT.value:
                extra["president"] = night.get("president")
                extra["provinces"] = _province_contest_rows(book, None)
        return ref.envelope(race=v, **extra)

    return build() if ref.live else cached(session, "race", (*ref.cache_key(), code), build)


# =========================================================================== calls / timeline
def calls(
    session: Session,
    ref: ElectionRef,
    *,
    race_type: str | None = None,
    status: str | None = None,
    include_superseded: bool = True,
    evidence: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """``GET /api/elections/{id}/calls`` — chronological race-call log (FINAL: every stored call
    state change; LIVE: every call the night has made so far, as persisted by the night service;
    ``evidence`` adds each call's evidence)."""
    rows: list[dict[str, Any]]
    if ref.reported:
        rows = cached(
            session,
            "calls",
            (*ref.cache_key(), evidence),
            lambda: _race_calls(session, ref, None, evidence=evidence),
        )
    elif ref.live:
        # bring the night (and its stored calls) up to date, then read with a fresh snapshot
        state = live_state(session, ref.id)
        upto = int((state.get("clock") or {}).get("seq") or 0)
        session.rollback()
        rows = _race_calls(session, ref, None, evidence=evidence, upto_seq=upto)
    else:
        rows = []
    if race_type is not None:
        rows = [r for r in rows if str(r.get("race_type") or "").upper() == race_type.upper()]
    if status is not None:
        rows = [r for r in rows if str(r.get("status") or "").upper() == status.upper()]
    if not include_superseded:
        rows = [r for r in rows if not r.get("superseded")]
    total = len(rows)
    if limit is not None:
        rows = rows[-int(limit) :] if limit > 0 else []
    return ref.envelope(count=total, calls=rows)


def timeline(
    session: Session,
    ref: ElectionRef,
    *,
    bucket_minutes: int = 15,
    detail: str = "summary",
    limit: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    """``GET /api/elections/{id}/timeline`` — reporting timeline (FINAL: complete; LIVE: up to the
    current event; hidden: only the number of events).  ``detail=summary`` aggregates events in
    ``bucket_minutes`` buckets; ``detail=events`` pages through the individual batches."""
    if detail not in ("summary", "events"):
        raise ValidationError("detail must be 'summary' or 'events'")
    if bucket_minutes < 1:
        raise ValidationError("bucket_minutes must be ≥ 1")
    n_events = int(
        session.scalar(
            select(func.count()).select_from(ReportingEvent).where(ReportingEvent.election_id == ref.id)
        )
        or 0
    )
    if ref.hidden:
        return ref.envelope(total_events=n_events, revealed_events=0, buckets=[], events=[])
    upto = n_events
    clock = None
    if ref.live:
        state = live_state(session, ref.id)
        clock = state.get("clock")
        upto = int((clock or {}).get("seq") or (state.get("snapshot") or {}).get("seq") or 0)

    def load() -> pd.DataFrame:
        munis = municipality_maps(session, ref.vintage_id)
        prov_code, _ = province_maps(session)
        rows = session.execute(
            select(
                ReportingEvent.seq,
                ReportingEvent.sim_time_s,
                ReportingEvent.timestamp,
                ReportingEvent.municipality_id,
                ReportingEvent.province_id,
                ReportingEvent.kind,
                ReportingEvent.ballots_in_batch,
                ReportingEvent.municipality_fraction_after,
            )
            .where(ReportingEvent.election_id == ref.id)
            .order_by(ReportingEvent.seq)
        ).all()
        df = pd.DataFrame(
            rows,
            columns=[
                "seq",
                "sim_time_s",
                "timestamp",
                "municipality_id",
                "province_id",
                "kind",
                "ballots",
                "fraction_after",
            ],
        )
        df["municipality_code"] = df["municipality_id"].map(lambda i: munis.get(int(i), {}).get("code"))
        df["municipality_name"] = df["municipality_id"].map(lambda i: munis.get(int(i), {}).get("name"))
        df["province_code"] = df["province_id"].map(prov_code)
        df["cumulative_ballots"] = df["ballots"].cumsum()
        return df

    df = cached(session, "timeline", (ref.id, iso(ref.simulated_at)), load)
    total_ballots = int(df["ballots"].sum()) if not df.empty else 0
    shown = df[df["seq"] <= upto]
    out: dict[str, Any] = {"total_events": n_events, "revealed_events": len(shown), "clock": clock}
    if ref.reported:
        out["total_ballots"] = total_ballots
    if detail == "events":
        page = shown.iloc[offset : offset + limit]
        out["events"] = [
            {
                "seq": int(r.seq),
                "sim_time_s": rnd(r.sim_time_s, 1),
                "clock": r.timestamp.strftime("%H:%M") if r.timestamp is not None else None,
                "time": iso(r.timestamp),
                "municipality_code": r.municipality_code,
                "municipality_name": r.municipality_name,
                "province_code": r.province_code,
                "kind": r.kind,
                "ballots": int(r.ballots),
                "municipality_fraction_after": rnd(r.fraction_after, 6),
                "cumulative_ballots": int(r.cumulative_ballots),
                "national_fraction": rnd(r.cumulative_ballots / total_ballots, 6)
                if ref.reported and total_ballots
                else None,
            }
            for r in page.itertuples(index=False)
        ]
        out["offset"] = offset
        out["limit"] = limit
        out["buckets"] = None
        return ref.envelope(**out)
    buckets = []
    if not shown.empty:
        size = bucket_minutes * 60.0
        b = (shown["sim_time_s"] // size).astype(int)
        done: set[str] = set()
        seen: set[str] = set()
        for k, g in shown.groupby(b, sort=True):
            seen |= set(g["municipality_code"])
            done |= set(g.loc[g["fraction_after"] >= 1.0 - 1e-9, "municipality_code"])
            last = g.iloc[-1]
            buckets.append(
                {
                    "start_s": float(k * size),
                    "clock": g.iloc[0]["timestamp"].strftime("%H:%M")
                    if g.iloc[0]["timestamp"] is not None
                    else None,
                    "events": len(g),
                    "ballots": int(g["ballots"].sum()),
                    "cumulative_ballots": int(last["cumulative_ballots"]),
                    "cumulative_pct": pct(int(last["cumulative_ballots"]), total_ballots)
                    if ref.reported
                    else None,
                    "municipalities_reporting": len(seen),
                    "municipalities_complete": len(done),
                    "last_seq": int(last["seq"]),
                }
            )
    first = shown.iloc[0] if not shown.empty else None
    last = shown.iloc[-1] if not shown.empty else None
    out.update(
        {
            "bucket_minutes": bucket_minutes,
            "first_report": None
            if first is None
            else {"seq": int(first["seq"]), "clock": first["timestamp"].strftime("%H:%M")},
            "last_report": None
            if last is None
            else {"seq": int(last["seq"]), "clock": last["timestamp"].strftime("%H:%M")},
            "buckets": buckets,
            "events": None,
        }
    )
    return ref.envelope(**out)
