"""Pure helpers behind the scenario editor (UI / API): apply edits and re-validate.

An edit is a small JSON object with an ``op`` and op-specific fields::

    apply_edits(doc, [
        {"op": "set_national_environment", "party": "PA", "value": 0.05},
        {"op": "set_candidate_quality", "candidate": "anne-de-vries", "value": 1.2},
        {"op": "set_party_base_share", "party": "CVU", "value": 0.18},
        {"op": "add_ticket", "party": "SAP", "president": "…", "vice_president": "…"},
        {"op": "remove_ticket", "party": "DM"},
        {"op": "set_ev_allocation", "value": "proportional"},
        {"op": "set_seed", "value": 20280001},
        {"op": "set", "path": "environment.turnout_base", "value": 0.8},
    ])

Edits are applied in order to a copy; the result is re-validated against the scenario schema
(and, when a frame is given, against the geography).  The input document is never modified.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.core.constitution import EVAllocationMethod
from app.core.errors import ScenarioError
from app.geography.frame import GeographyFrame
from app.scenarios.loader import validate_scenario
from app.scenarios.schema import ScenarioDocument


@dataclass
class EditResult:
    """Outcome of :func:`apply_edits`."""

    document: ScenarioDocument
    changes: list[str] = field(default_factory=list)
    #: Problems against the geography (only when a frame was given); empty = valid.
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _find(items: list[dict], key: str, value: Any, what: str) -> dict:
    for it in items:
        if it.get(key) == value:
            return it
    raise ScenarioError(f"unknown {what} {value!r}")


def _req(edit: Mapping[str, Any], *names: str) -> list[Any]:
    missing = [n for n in names if n not in edit]
    if missing:
        raise ScenarioError(f"edit {edit.get('op')!r} requires {missing}")
    return [edit[n] for n in names]


def _set_path(data: dict, path: str, value: Any) -> None:
    """Set a dotted path; list elements are addressed by index or by their ``code``/``key``."""
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise ScenarioError("empty path")
    cur: Any = data
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        if isinstance(cur, list):
            if part.isdigit():
                idx = int(part)
                if idx >= len(cur):
                    raise ScenarioError(f"{path}: index {idx} out of range")
            else:
                idx = next(
                    (
                        j
                        for j, x in enumerate(cur)
                        if isinstance(x, dict) and part in (x.get("code"), x.get("key"))
                    ),
                    -1,
                )
                if idx < 0:
                    raise ScenarioError(f"{path}: no element {part!r}")
            if last:
                cur[idx] = value
            else:
                cur = cur[idx]
        elif isinstance(cur, dict):
            if last:
                if value is None and _is_mapping_entry(path):
                    cur.pop(part, None)  # deleting a free-form mapping entry (absent → no-op)
                else:
                    cur[part] = value
            else:
                if cur.get(part) is None:
                    cur[part] = {}
                cur = cur[part]
        else:
            raise ScenarioError(f"{path}: cannot descend into {type(cur).__name__}")


def _is_mapping_entry(path: str) -> bool:
    """Paths whose last segment is a free-form mapping key (deleting with value None)."""
    head = path.rsplit(".", 1)[0]
    return head.endswith(
        (
            "national",
            "provinces",
            "municipalities",
            "regions",
            "demographics",
            "urbanity",
            "budgets",
            "strategies",
        )
    )


# --------------------------------------------------------------------------- operations
def _op_set_national(d: dict, e: Mapping[str, Any]) -> str:
    env = d.setdefault("environment", {}).setdefault("national", {})
    values = e.get("values")
    if values is None:
        party, value = _req(e, "party", "value")
        values = {party: value}
    for party, value in values.items():
        _find(d["parties"], "code", party, "party")
        if value is None:
            env.pop(party, None)
        else:
            env[party] = float(value)
    return f"national environment: {values}"


def _op_set_province_env(d: dict, e: Mapping[str, Any]) -> str:
    prov, party, value = _req(e, "province", "party", "value")
    _find(d["parties"], "code", party, "party")
    block = d.setdefault("environment", {}).setdefault("provinces", {}).setdefault(prov, {})
    if value is None:
        block.pop(party, None)
    else:
        block[party] = float(value)
    return f"environment {prov}/{party} = {value}"


def _op_candidate_quality(d: dict, e: Mapping[str, Any]) -> str:
    key, value = _req(e, "candidate", "value")
    _find(d.get("candidates", []), "key", key, "candidate")["quality"] = float(value)
    return f"candidate {key} quality = {value}"


def _op_base_share(d: dict, e: Mapping[str, Any]) -> str:
    party, value = _req(e, "party", "value")
    _find(d["parties"], "code", party, "party")["base_share"] = float(value)
    return f"party {party} base_share = {value}"


def _op_party_field(d: dict, e: Mapping[str, Any]) -> str:
    party, fld, value = _req(e, "party", "field", "value")
    if fld == "code":
        raise ScenarioError("a party code is its stable identifier and cannot be edited")
    _find(d["parties"], "code", party, "party")[fld] = value
    return f"party {party}.{fld} = {value!r}"


def _op_add_ticket(d: dict, e: Mapping[str, Any]) -> str:
    party, pres, vp = _req(e, "party", "president", "vice_president")
    tickets = d.setdefault("president", {}).setdefault("tickets", [])
    existing = [t for t in tickets if t.get("party") == party]
    if existing and party is not None and not e.get("replace", False):
        raise ScenarioError(f"party {party} already has a ticket (use replace: true)")
    tickets[:] = [t for t in tickets if t.get("party") != party or party is None]
    tickets.append(
        {
            "party": party,
            "president": pres,
            "vice_president": vp,
            "incumbent": bool(e.get("incumbent", False)),
            "withdrawn": bool(e.get("withdrawn", False)),
        }
    )
    return f"ticket added: {party} {pres}/{vp}"


def _match_ticket(t: dict, e: Mapping[str, Any]) -> bool:
    if "president" in e:
        return t.get("president") == e["president"]
    if "party" in e:
        return t.get("party") == e["party"]
    raise ScenarioError("ticket edits need 'party' or 'president'")


def _op_remove_ticket(d: dict, e: Mapping[str, Any]) -> str:
    tickets = d.setdefault("president", {}).setdefault("tickets", [])
    keep = [t for t in tickets if not _match_ticket(t, e)]
    if len(keep) == len(tickets):
        raise ScenarioError(f"no matching ticket for {dict(e)}")
    tickets[:] = keep
    return f"ticket removed: {e.get('party', e.get('president'))}"


def _op_withdraw_ticket(d: dict, e: Mapping[str, Any]) -> str:
    tickets = d.setdefault("president", {}).setdefault("tickets", [])
    hits = [t for t in tickets if _match_ticket(t, e)]
    if not hits:
        raise ScenarioError(f"no matching ticket for {dict(e)}")
    for t in hits:
        t["withdrawn"] = bool(e.get("withdrawn", True))
    return f"ticket {e.get('party', e.get('president'))} withdrawn = {bool(e.get('withdrawn', True))}"


def _op_ev_allocation(d: dict, e: Mapping[str, Any]) -> str:
    (value,) = _req(e, "value")
    try:
        method = EVAllocationMethod(value)
    except ValueError as exc:
        raise ScenarioError(
            f"unknown EV allocation {value!r}; expected {[m.value for m in EVAllocationMethod]}"
        ) from exc
    d.setdefault("electoral_college", {})["allocation"] = method.value
    return f"EV allocation = {method.value}"


def _op_seed(d: dict, e: Mapping[str, Any]) -> str:
    (value,) = _req(e, "value")
    d["scenario"]["seed"] = int(value)
    return f"seed = {int(value)}"


def _op_geo_seed(d: dict, e: Mapping[str, Any]) -> str:
    (value,) = _req(e, "value")
    d["scenario"]["political_geography_seed"] = int(value)
    return f"political_geography_seed = {int(value)}"


def _op_add_candidate(d: dict, e: Mapping[str, Any]) -> str:
    (cand,) = _req(e, "candidate")
    if not isinstance(cand, Mapping) or "key" not in cand:
        raise ScenarioError("add_candidate needs a candidate object with a 'key'")
    cands = d.setdefault("candidates", [])
    if any(c.get("key") == cand["key"] for c in cands):
        raise ScenarioError(f"candidate {cand['key']} already exists")
    cands.append(dict(cand))
    return f"candidate added: {cand['key']}"


def _op_remove_candidate(d: dict, e: Mapping[str, Any]) -> str:
    (key,) = _req(e, "candidate")
    for t in d.get("president", {}).get("tickets", []):
        if key in (t.get("president"), t.get("vice_president")):
            raise ScenarioError(f"candidate {key} is on a presidential ticket; remove the ticket first")
    cands = d.setdefault("candidates", [])
    _find(cands, "key", key, "candidate")
    cands[:] = [c for c in cands if c.get("key") != key]
    return f"candidate removed: {key}"


def _op_set(d: dict, e: Mapping[str, Any]) -> str:
    path, value = _req(e, "path", "value")
    if not isinstance(path, str):
        raise ScenarioError(f"path must be a dotted string, got {path!r}")
    if path.split(".")[0] == "scenario" and path.endswith("fictional"):
        raise ScenarioError("scenarios are always fictional")
    _set_path(d, str(path), copy.deepcopy(value))
    return f"{path} = {value!r}"


OPERATIONS: dict[str, Callable[[dict, Mapping[str, Any]], str]] = {
    "set_national_environment": _op_set_national,
    "set_province_environment": _op_set_province_env,
    "set_candidate_quality": _op_candidate_quality,
    "set_party_base_share": _op_base_share,
    "set_party_field": _op_party_field,
    "add_ticket": _op_add_ticket,
    "remove_ticket": _op_remove_ticket,
    "withdraw_ticket": _op_withdraw_ticket,
    "set_ev_allocation": _op_ev_allocation,
    "set_seed": _op_seed,
    "set_political_geography_seed": _op_geo_seed,
    "add_candidate": _op_add_candidate,
    "remove_candidate": _op_remove_candidate,
    "set": _op_set,
}


def apply_edits(
    doc: ScenarioDocument,
    edits: Sequence[Mapping[str, Any]],
    *,
    frame: GeographyFrame | None = None,
    regions: Any = None,
) -> EditResult:
    """Apply ``edits`` in order to a copy of ``doc`` and re-validate.

    Raises :class:`ScenarioError` for unknown operations, bad references or a document that no
    longer satisfies the schema.  With ``frame`` the result's ``problems`` lists geography
    problems (see :func:`app.scenarios.loader.validate_scenario`) without raising.
    """
    data = doc.model_dump(mode="json")
    changes: list[str] = []
    for i, e in enumerate(edits):
        op = e.get("op") if isinstance(e, Mapping) else None
        fn = OPERATIONS.get(str(op))
        if fn is None:
            raise ScenarioError(f"edit #{i}: unknown operation {op!r}; expected one of {sorted(OPERATIONS)}")
        try:
            changes.append(fn(data, e))
        except ScenarioError as exc:
            raise ScenarioError(f"edit #{i} ({op}): {exc}") from exc
        except (TypeError, ValueError, AttributeError, KeyError, IndexError) as exc:
            # malformed edit payloads (e.g. from the API) are user errors, not crashes
            raise ScenarioError(f"edit #{i} ({op}): malformed edit {dict(e)!r}: {exc}") from exc
    try:
        new = ScenarioDocument.model_validate(data)
    except PydanticValidationError as exc:
        raise ScenarioError(f"edited scenario is invalid:\n{exc}") from exc
    problems = validate_scenario(new, frame, regions) if frame is not None else []
    return EditResult(document=new, changes=changes, problems=problems)


def available_operations() -> list[str]:
    """Names of the supported edit operations."""
    return sorted(OPERATIONS)
