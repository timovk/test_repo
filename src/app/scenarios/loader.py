"""Loading, saving, listing and validating scenario documents (FICTIONAL political assumptions).

Scenario YAML files live in ``config/scenarios``.  Besides the fields of
:class:`~app.scenarios.schema.ScenarioDocument` a file may use three *loader directives*, merged
before validation:

* ``parties_file: parties/fictional.yaml`` (or a list) — include a shared party list
  (``parties:`` key); entries of the scenario's own ``parties:`` with the same code are merged
  over the included ones, new codes are appended;
* ``party_overrides: {CODE: {field: value}}`` — deep-merged into the included parties (e.g. a
  renamed party or a different ``base_share``);
* ``candidates_file: …`` (or a list) — include shared candidates (``candidates:`` key), merged by
  key the same way.

Include paths are resolved relative to ``config/`` first, then to the scenario file's directory.
:func:`dump_scenario` writes the fully merged document, which loads back identically.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError

from app.core.config import dump_yaml, get_constitution
from app.core.errors import ScenarioError
from app.core.logging import get_logger
from app.core.rng import derive_seed
from app.core.settings import get_settings
from app.geography.frame import GeographyFrame
from app.scenarios.schema import ScenarioDocument

log = get_logger(__name__)

LOADER_DIRECTIVES: tuple[str, ...] = ("parties_file", "candidates_file", "party_overrides")
_RACE_CODE_RE = re.compile(
    r"^(PRES(-[A-Z]{2})?|HOUSE-[A-Z]{2}-\d{2}|SEN-[A-Z]{2}-[12]|GOV-[A-Z]{2}|MAYOR-GM\d{4}|COUNCIL-GM\d{4}|PROVLEG-[A-Z]{2})$"
)


def scenarios_dir() -> Path:
    """``config/scenarios`` of the active settings."""
    return get_settings().config_dir / "scenarios"


# --------------------------------------------------------------------------- merging
def _deep_merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _deep_merge(base[k], v) if k in base else copy.deepcopy(v)
        return out
    return copy.deepcopy(over)


def _merge_by(items: list[dict], extra: list[dict], key: str, what: str) -> list[dict]:
    out = [copy.deepcopy(x) for x in items]
    pos = {x.get(key): i for i, x in enumerate(out)}
    for x in extra:
        if not isinstance(x, dict) or key not in x:
            raise ScenarioError(f"{what}: every entry needs a '{key}'")
        if x[key] in pos:
            out[pos[x[key]]] = _deep_merge(out[pos[x[key]]], x)
        else:
            pos[x[key]] = len(out)
            out.append(copy.deepcopy(x))
    return out


def _resolve_include(ref: str, base_dir: Path | None) -> Path:
    p = Path(ref)
    candidates = [p] if p.is_absolute() else [get_settings().config_dir / p]
    if base_dir is not None and not p.is_absolute():
        candidates.append(base_dir / p)
    for c in candidates:
        if c.exists():
            return c
    raise ScenarioError(f"included file not found: {ref} (looked in {', '.join(str(c) for c in candidates)})")


def _read_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ScenarioError(f"invalid YAML in {path}: {exc}") from exc


def _include_list(data: dict, directive: str, key: str, base_dir: Path | None) -> list[dict]:
    refs = data.get(directive)
    if refs is None:
        return []
    refs = [refs] if isinstance(refs, str) else list(refs)
    out: list[dict] = []
    for ref in refs:
        inc = _read_yaml(_resolve_include(str(ref), base_dir))
        items = inc.get(key) if isinstance(inc, dict) else inc
        if not isinstance(items, list):
            raise ScenarioError(f"{ref}: expected a '{key}:' list")
        out = _merge_by(out, items, "code" if key == "parties" else "key", str(ref))
    return out


def merge_includes(data: Mapping[str, Any], base_dir: Path | None = None) -> dict[str, Any]:
    """Apply the loader directives of a raw scenario mapping and return a plain document dict."""
    if not isinstance(data, Mapping):
        raise ScenarioError("a scenario document must be a mapping")
    doc = {k: copy.deepcopy(v) for k, v in data.items() if k not in LOADER_DIRECTIVES}
    parties = _include_list(dict(data), "parties_file", "parties", base_dir)
    if parties or "parties" in doc:
        parties = _merge_by(parties, list(doc.get("parties") or []), "code", "parties")
    overrides = data.get("party_overrides") or {}
    if overrides:
        pos = {p.get("code"): i for i, p in enumerate(parties)}
        for code, patch in overrides.items():
            if code not in pos:
                raise ScenarioError(f"party_overrides: unknown party {code}")
            parties[pos[code]] = _deep_merge(parties[pos[code]], patch)
    if parties:
        doc["parties"] = parties
    cands = _include_list(dict(data), "candidates_file", "candidates", base_dir)
    if cands or "candidates" in doc:
        doc["candidates"] = _merge_by(cands, list(doc.get("candidates") or []), "key", "candidates")
    # keep a stable, readable key order (schema order)
    order = list(ScenarioDocument.model_fields)
    return {k: doc[k] for k in sorted(doc, key=lambda k: order.index(k) if k in order else len(order))}


def _validate(data: Mapping[str, Any], source: str) -> ScenarioDocument:
    try:
        return ScenarioDocument.model_validate(data)
    except PydanticValidationError as exc:
        raise ScenarioError(f"invalid scenario {source}:\n{exc}") from exc


# --------------------------------------------------------------------------- public API
def scenario_path(path_or_slug: str | Path) -> Path:
    """Resolve a file path or a slug (``config/scenarios/<slug>.yaml`` or ``scenario.slug`` match)."""
    p = Path(path_or_slug)
    if p.suffix in (".yaml", ".yml"):
        for c in (p, get_settings().config_dir / p, scenarios_dir() / p.name):
            if c.exists():
                return c
        raise ScenarioError(f"scenario file not found: {path_or_slug}")
    slug = str(path_or_slug)
    d = scenarios_dir()
    for name in (f"{slug}.yaml", f"{slug.replace('-', '_')}.yaml", f"{slug}.yml"):
        if (d / name).exists():
            return d / name
    for f in sorted(d.glob("*.y*ml")):
        try:
            meta = (_read_yaml(f) or {}).get("scenario") or {}
        except ScenarioError:
            continue
        if isinstance(meta, dict) and meta.get("slug") == slug:
            return f
    raise ScenarioError(f"no scenario with slug or file {slug!r} in {d}")


def load_scenario(path_or_slug: str | Path) -> ScenarioDocument:
    """Load, merge includes and validate a scenario by file path or slug."""
    path = scenario_path(path_or_slug)
    data = _read_yaml(path)
    return _validate(merge_includes(data, path.parent), str(path))


def load_scenario_text(text: str, base_dir: Path | None = None) -> ScenarioDocument:
    """Load a scenario from YAML text (e.g. uploaded in the editor)."""
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ScenarioError(f"invalid YAML: {exc}") from exc
    return _validate(merge_includes(data, base_dir), "<text>")


def scenario_to_dict(doc: ScenarioDocument) -> dict[str, Any]:
    """JSON-compatible dict of the full document (defaults omitted, required fields kept)."""
    return doc.model_dump(mode="json", exclude_defaults=True)


def dump_scenario(doc: ScenarioDocument) -> str:
    """Human-readable YAML of the fully merged document (round-trips through :func:`load_scenario_text`)."""
    header = (
        f"# Scenario '{doc.scenario.slug}' — FICTIONAL political assumptions over REAL Dutch geography.\n"
        "# Exported by app.scenarios.loader.dump_scenario (includes merged).\n"
    )
    return header + dump_yaml(scenario_to_dict(doc))


def save_scenario(doc: ScenarioDocument, path: str | Path) -> Path:
    """Write :func:`dump_scenario` output to ``path``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_scenario(doc), encoding="utf-8")
    return p


@dataclass(frozen=True)
class ScenarioInfo:
    """Summary of a scenario file (for listings)."""

    slug: str
    name: str
    year: int
    election_type: str
    seed: int
    path: Path
    description: str | None = None
    valid: bool = True
    error: str | None = None


def list_scenarios(directory: str | Path | None = None) -> list[ScenarioInfo]:
    """Every scenario file in ``config/scenarios`` (invalid ones are listed with their error)."""
    d = Path(directory) if directory else scenarios_dir()
    out: list[ScenarioInfo] = []
    for f in sorted(d.glob("*.y*ml")):
        try:
            doc = _validate(merge_includes(_read_yaml(f), f.parent), str(f))
            m = doc.scenario
            out.append(ScenarioInfo(m.slug, m.name, m.year, m.election_type, m.seed, f, m.description))
        except ScenarioError as exc:
            raw = _read_yaml(f) if f.exists() else {}
            meta = raw.get("scenario", {}) if isinstance(raw, dict) else {}
            out.append(
                ScenarioInfo(
                    slug=str(meta.get("slug", f.stem)),
                    name=str(meta.get("name", f.stem)),
                    year=int(meta.get("year", 0) or 0),
                    election_type=str(meta.get("election_type", "general")),
                    seed=int(meta.get("seed", 0) or 0),
                    path=f,
                    valid=False,
                    error=str(exc).splitlines()[0],
                )
            )
    return sorted(out, key=lambda s: (s.year, s.slug))


def duplicate_scenario(
    doc: ScenarioDocument, new_slug: str, new_seed: int | None = None, *, name: str | None = None
) -> ScenarioDocument:
    """Copy of ``doc`` with a new slug and seed (the political geography seed is kept, so the
    municipalities' persistent character is unchanged).  ``new_seed=None`` derives one."""
    data = doc.model_dump(mode="json")
    seed = (
        new_seed if new_seed is not None else derive_seed(doc.scenario.seed, "duplicate", new_slug) % (2**62)
    )
    data["scenario"]["slug"] = new_slug
    data["scenario"]["seed"] = int(seed)
    data["scenario"]["name"] = name or f"{doc.scenario.name} (copy)"
    return _validate(data, f"duplicate of {doc.scenario.slug}")


# --------------------------------------------------------------------------- validation against geography
def validate_scenario(
    doc: ScenarioDocument,
    frame: GeographyFrame,
    regions: Any = None,
) -> list[str]:
    """Problems of ``doc`` against a geography frame and the region definitions (empty = OK).

    Unknown province / municipality codes, undefined regions, unknown demographic variables,
    inconsistent candidate homes, malformed race codes, unknown ticket candidates, Senate classes
    outside the constitution and missing imported baselines are reported.
    """
    from app.geography.frame import DEMOGRAPHIC_VARIABLES
    from app.simulation.regions import RegionsConfig, ResolvedRegions, load_regions_config
    from app.simulation.structural import parse_events

    if regions is None:
        region_names = set(load_regions_config().regions)
    elif isinstance(regions, RegionsConfig):
        region_names = set(regions.regions)
    elif isinstance(regions, ResolvedRegions):
        region_names = set(regions.names)
    else:
        region_names = set(regions)
    provs = set(frame.province_codes)
    munis = set(frame.muni_codes)
    parties = {p.code for p in doc.parties}
    problems: list[str] = []

    def prov(where: str, code: str) -> None:
        if code not in provs:
            problems.append(f"{where}: unknown province code {code!r}")

    def muni(where: str, code: str) -> None:
        if code not in munis:
            problems.append(f"{where}: unknown municipality code {code!r}")

    def party(where: str, code: str) -> None:
        if code not in parties:
            problems.append(f"{where}: unknown party {code!r}")

    for p in doc.parties:
        for k in p.provinces:
            prov(f"party {p.code}.provinces", k)
        for k in p.municipalities:
            muni(f"party {p.code}.municipalities", k)
        for k in p.regions:
            if k not in region_names:
                problems.append(f"party {p.code}.regions: undefined region {k!r}")
        for k in p.demographics:
            if k not in DEMOGRAPHIC_VARIABLES and k not in frame.demo_names:
                problems.append(f"party {p.code}.demographics: unknown variable {k!r}")
        if p.successor is not None:
            party(f"party {p.code}.successor", p.successor)
    for pc, shifts in doc.environment.provinces.items():
        prov("environment.provinces", pc)
        for c in shifts:
            party(f"environment.provinces.{pc}", c)
    try:
        for ev in parse_events(doc.environment.events):
            for c in ev.national:
                party(f"event {ev.name}", c)
            for pc, shifts in ev.provinces.items():
                prov(f"event {ev.name}", pc)
                for c in shifts:
                    party(f"event {ev.name}.{pc}", c)
    except ScenarioError as exc:
        problems.append(str(exc))
    for pc, tgt in doc.calibration.provinces.items():
        prov("calibration.provinces", pc)
        for c in tgt:
            party(f"calibration.provinces.{pc}", c)
    for mc, tgt in doc.calibration.municipalities.items():
        muni("calibration.municipalities", mc)
        for c in tgt:
            party(f"calibration.municipalities.{mc}", c)
    if doc.calibration.imported_baseline:
        p = Path(doc.calibration.imported_baseline)
        if not (p if p.is_absolute() else get_settings().config_dir / p).exists():
            problems.append(
                f"calibration.imported_baseline: file not found {doc.calibration.imported_baseline!r}"
            )
    for c in doc.candidates:
        if c.home_municipality is not None:
            muni(f"candidate {c.key}.home_municipality", c.home_municipality)
        if c.home_province is not None:
            prov(f"candidate {c.key}.home_province", c.home_province)
        if c.home_municipality in munis and c.home_province is not None:
            m = frame.muni_index(c.home_municipality)
            actual = frame.province_codes[int(frame.muni_province[m])]
            if actual != c.home_province:
                problems.append(
                    f"candidate {c.key}: home_province {c.home_province} but {c.home_municipality} lies in {actual}"
                )
    cand = {c.key: c for c in doc.candidates}
    seen: set[str] = set()
    for t in doc.president.tickets:
        for role in (t.president, t.vice_president):
            if role in seen:
                problems.append(f"ticket: candidate {role} appears on more than one ticket")
            seen.add(role)
            if role in cand and cand[role].party != t.party:
                problems.append(f"ticket {t.party}: candidate {role} belongs to party {cand[role].party}")
    ticket_parties = [t.party for t in doc.president.tickets if t.party is not None]
    if len(set(ticket_parties)) != len(ticket_parties):
        problems.append("more than one presidential ticket for the same party")
    for section in ("house", "senate", "governors", "municipal"):
        spec = getattr(doc, section)
        for code, rule in spec.contest_rules.items():
            party(f"{section}.contest_rules", code)
            for pc in rule.provinces or []:
                prov(f"{section}.contest_rules.{code}", pc)
            for r in rule.regions or []:
                if r not in region_names:
                    problems.append(f"{section}.contest_rules.{code}: undefined region {r!r}")
        for race, keys in spec.explicit_candidates.items():
            if not _RACE_CODE_RE.match(race):
                problems.append(f"{section}.explicit_candidates: malformed race code {race!r}")
            for k in keys:
                if k not in cand:
                    problems.append(f"{section}.explicit_candidates.{race}: unknown candidate {k!r}")
    classes = get_constitution().senate_classes
    for c in doc.senate.classes_up or []:
        if not 1 <= c <= classes:
            problems.append(f"senate.classes_up: class {c} outside 1..{classes}")
    for ev in doc.party_events:
        party(f"party_events {ev.year}", ev.party)
        if ev.related_party is not None:
            party(f"party_events {ev.year}.related_party", ev.related_party)
    return problems


def validate_scenario_or_raise(doc: ScenarioDocument, frame: GeographyFrame, regions: Any = None) -> None:
    """Raise :class:`ScenarioError` listing every problem found by :func:`validate_scenario`."""
    problems = validate_scenario(doc, frame, regions)
    if problems:
        raise ScenarioError(
            f"scenario {doc.scenario.slug} does not fit the geography:\n  - " + "\n  - ".join(problems)
        )
