"""Campaign read models: FICTIONAL campaign plans (budgets, strategies, allocations by action,
target and week) and their SIMULATED effects.

Expected effects (before the seeded draw) are always shown; the *realised* persuasion and
turnout effects are inputs of the hidden result and are only revealed once the election is
reported.  :func:`plan_preview` re-plans an election's campaigns with other budgets / strategies /
seed as a what-if (nothing is stored)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.models import BallotCandidate, Campaign, CampaignAllocation, Party
from app.services.read._base import (
    FICTIONAL,
    SIMULATED,
    ElectionRef,
    cached,
    district_maps,
    municipality_maps,
    province_maps,
    rnd,
)

log = get_logger(__name__)

HIDDEN_FIELDS = ("realized_effect", "turnout_effect")


def _target_names(session: Session, ref: ElectionRef) -> dict[tuple[str, str], str]:
    _ids, pmap = province_maps(session)
    names: dict[tuple[str, str], str] = {("national", "NL"): "Nederland"}
    for code, p in pmap.items():
        names[("province", code)] = p["name"]
    for m in municipality_maps(session, ref.vintage_id).values():
        names[("municipality", m["code"])] = m["name"]
    for d in district_maps(session, ref.district_plan_id).values():
        names[("district", d["code"])] = d["name"] or d["code"]
    return names


def _aggregate(
    rows: Iterable[Mapping[str, Any]], names: Mapping[tuple[str, str], str], *, reveal: bool, top: int | None
) -> dict[str, Any]:
    by_target: dict[tuple[str, str], dict[str, Any]] = {}
    by_action: dict[str, dict[str, Any]] = {}
    by_week: dict[int, float] = {}
    spend = proceeds = 0.0
    n = 0
    for a in rows:
        n += 1
        amount = float(a["amount"] or 0.0)
        spend += amount
        proceeds += float(a.get("proceeds") or 0.0)
        key = (str(a["target_level"]), str(a["target_code"]))
        t = by_target.setdefault(
            key,
            {
                "level": key[0],
                "code": key[1],
                "name": names.get(key),
                "amount": 0.0,
                "expected_effect": 0.0,
                "realized_effect": 0.0 if reveal else None,
                "turnout_effect": 0.0 if reveal else None,
                "actions": {},
            },
        )
        t["amount"] += amount
        t["expected_effect"] += float(a.get("expected_effect") or 0.0)
        if reveal:
            t["realized_effect"] += float(a.get("realized_effect") or 0.0)
            t["turnout_effect"] += float(a.get("turnout_effect") or 0.0)
        t["actions"][a["action"]] = round(t["actions"].get(a["action"], 0.0) + amount, 3)
        ac = by_action.setdefault(
            a["action"], {"action": a["action"], "amount": 0.0, "allocations": 0, "units": 0}
        )
        ac["amount"] += amount
        ac["allocations"] += 1
        ac["units"] += int(a.get("units") or 0)
        w = a.get("week")
        if w is not None:
            by_week[int(w)] = by_week.get(int(w), 0.0) + amount
    targets = sorted(by_target.values(), key=lambda t: (-t["amount"], t["level"], t["code"]))
    for t in targets:
        t["amount"] = round(t["amount"], 3)
        t["expected_effect"] = rnd(t["expected_effect"], 6)
        t["realized_effect"] = rnd(t["realized_effect"], 6)
        t["turnout_effect"] = rnd(t["turnout_effect"], 6)
    return {
        "spend": round(spend, 3),
        "fundraising_proceeds": round(proceeds, 3),
        "allocations": n,
        "targets_total": len(targets),
        "by_action": sorted(
            ({**v, "amount": round(v["amount"], 3)} for v in by_action.values()), key=lambda v: -v["amount"]
        ),
        "by_week": [{"week": w, "amount": round(v, 3)} for w, v in sorted(by_week.items())],
        "targets": targets if top is None else targets[:top],
    }


def campaigns(
    session: Session, ref: ElectionRef, *, party: str | None = None, all_targets: bool = False
) -> dict[str, Any]:
    """``GET /api/campaigns/{id}?party=&all_targets=`` — every party's campaign: budget, strategy,
    seed, ticket, spending by action and week, and spending / effects per target (top 25 targets
    unless ``all_targets``)."""

    def build() -> dict[str, Any]:
        names = _target_names(session, ref)
        rows = session.execute(
            select(Campaign, Party.code, Party.name, Party.color, BallotCandidate.ballot_name)
            .outerjoin(Party, Party.id == Campaign.party_id)
            .outerjoin(BallotCandidate, BallotCandidate.id == Campaign.ballot_candidate_id)
            .where(Campaign.election_id == ref.id)
            .order_by(Campaign.id)
        ).all()
        allocs: dict[int, list[dict[str, Any]]] = {}
        for a in session.scalars(
            select(CampaignAllocation)
            .join(Campaign, Campaign.id == CampaignAllocation.campaign_id)
            .where(Campaign.election_id == ref.id)
        ):
            allocs.setdefault(a.campaign_id, []).append(
                {
                    "target_level": a.target_level,
                    "target_code": a.target_code,
                    "action": a.action,
                    "amount": a.amount,
                    "units": a.units,
                    "proceeds": a.proceeds,
                    "week": a.week,
                    "expected_effect": a.expected_effect,
                    "realized_effect": a.realized_effect,
                    "turnout_effect": a.turnout_effect,
                }
            )
        out = []
        for c, code, name, color, ticket in rows:
            agg = _aggregate(allocs.get(c.id, []), names, reveal=ref.reported, top=None)
            out.append(
                {
                    "id": c.id,
                    "party": code,
                    "party_name": name,
                    "color": color,
                    "ticket": ticket,
                    "budget": rnd(c.budget, 3),
                    "strategy": c.strategy,
                    "seed": int(c.seed),
                    **agg,
                }
            )
        return {"campaigns": out}

    data = cached(session, "campaigns", ref.cache_key(), build)
    items = data["campaigns"]
    if party is not None:
        items = [c for c in items if c["party"] == party.upper()]
    if not all_targets:
        items = [{**c, "targets": c["targets"][:25]} for c in items]
    return ref.envelope(
        provenance={"plans": FICTIONAL, "effects": SIMULATED},
        effects_revealed=ref.reported,
        hidden_fields=[] if ref.reported else list(HIDDEN_FIELDS),
        units={"amount": "abstract resource units", "effects": "logit points of party utility"},
        campaigns=items,
    )


def plan_preview(
    session: Session,
    ref: ElectionRef,
    *,
    seed: int | None = None,
    budgets: Mapping[str, float] | None = None,
    strategies: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """``POST /api/campaigns/{id}/plan`` — what-if campaign plans for the election with other
    budgets / strategies / seed (nothing is stored).  Only expected effects are returned."""
    from app.campaigns import run_campaigns
    from app.services._create import campaign_targets
    from app.services.runtime import election_inputs

    inputs = election_inputs(session, ref.id)
    spec = inputs.scenario.campaigns
    upd: dict[str, Any] = {}
    parties = set(inputs.model.party_codes)
    if budgets:
        bad = sorted(set(budgets) - parties)
        if bad:
            raise ValidationError(f"unknown parties in budgets: {bad}")
        if any(float(v) < 0 for v in budgets.values()):
            raise ValidationError("budgets must be non-negative")
        upd["budgets"] = {**dict(spec.budgets), **{k: float(v) for k, v in budgets.items()}}
    if strategies:
        bad = sorted(set(strategies) - parties)
        if bad:
            raise ValidationError(f"unknown parties in strategies: {bad}")
        upd["strategies"] = {**dict(spec.strategies), **dict(strategies)}
    spec = spec.model_copy(update=upd) if upd else spec
    run_seed = int(seed if seed is not None else inputs.seed)
    try:
        run = run_campaigns(spec, campaign_targets(inputs), run_seed)
    except (ValueError, KeyError) as exc:
        raise ValidationError(f"invalid campaign plan: {exc}") from exc
    names = _target_names(session, ref)
    out = []
    for party in sorted(run.effects):
        eff = run.effects[party]
        rows = [
            {
                "target_level": e.allocation.level,
                "target_code": e.allocation.code,
                "action": e.allocation.action,
                "amount": e.allocation.amount,
                "units": e.allocation.units,
                "proceeds": e.allocation.proceeds,
                "week": e.allocation.week,
                "expected_effect": e.expected_persuasion,
            }
            for e in eff.allocations
        ]
        out.append(
            {
                "party": party,
                "budget": rnd((spec.budgets or {}).get(party), 3),
                "strategy": run.strategies.get(party, "balanced"),
                **_aggregate(rows, names, reveal=False, top=25),
            }
        )
    return ref.envelope(
        preview=True,
        stored=False,
        seed=run_seed,
        provenance={"plans": FICTIONAL, "effects": SIMULATED},
        note="What-if plan: expected effects only; the election's stored campaigns are unchanged.",
        campaigns=out,
    )
