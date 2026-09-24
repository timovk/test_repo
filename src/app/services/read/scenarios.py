"""Scenario editor service: list, read (YAML + parsed JSON), validate, create / import, update
(whole document or editor operations), duplicate, export and delete scenario documents.

Scenarios are FICTIONAL political assumptions.  Built-in scenarios (``config/scenarios/*.yaml``)
are read-only; user scenarios live in ``config/scenarios/user/<slug>.yaml``.  Saving a user
scenario writes the file **and** the ``scenario`` table: the row with the slug is updated in place
unless an election was created from it — elections must keep the exact document they were built
from — in which case the new version is stored as a variant ``<slug>--<hash8>`` (the convention of
:mod:`app.services.elections`).  Slugs are validated (``[a-z0-9][a-z0-9-_]*``), so a request can
never write outside the user directory.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import (
    DataNotPreparedError,
    ElectionError,
    NotFoundError,
    ScenarioError,
)
from app.core.logging import get_logger
from app.core.rng import config_hash
from app.core.settings import get_settings
from app.models import Election, Scenario, utcnow
from app.scenarios import loader as scenario_loader
from app.scenarios.editor import apply_edits, available_operations
from app.scenarios.loader import (
    dump_scenario,
    duplicate_scenario,
    list_scenarios,
    load_scenario_text,
    scenario_path,
    scenario_to_dict,
    validate_scenario,
)
from app.scenarios.schema import ScenarioDocument
from app.services.read._base import FICTIONAL, iso

log = get_logger(__name__)

SOURCE_BUILTIN = "builtin"
SOURCE_USER = "user"
SOURCE_DATABASE = "database"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]*$")


class ScenarioConflictError(ElectionError):
    """A scenario operation conflicts with existing state (HTTP 409)."""


def default_user_dir() -> Path:
    """``config/scenarios/user``: user scenarios of the API's editor and of the CLI."""
    return scenario_loader.scenarios_dir() / "user"


def _check_slug(slug: str) -> str:
    if not _SLUG_RE.match(slug or "") or len(slug) > 80:
        raise ScenarioError(f"invalid scenario slug {slug!r} (lower-case letters, digits, '-', '_')")
    return slug


def _user_path(user_dir: Path, slug: str) -> Path:
    return user_dir / f"{_check_slug(slug)}.yaml"


def _builtin_path(slug: str) -> Path | None:
    try:
        p = scenario_path(slug)
    except ScenarioError:
        return None
    return p if p.parent.resolve() == scenario_loader.scenarios_dir().resolve() else None


def _locate(slug: str, user_dir: Path) -> tuple[str, Path]:
    _check_slug(slug)
    up = _user_path(user_dir, slug)
    if up.exists():
        return SOURCE_USER, up
    bp = _builtin_path(slug)
    if bp is not None:
        return SOURCE_BUILTIN, bp
    raise NotFoundError(f"scenario {slug!r} not found")


def locate(slug: str, user_dir: Path | None = None) -> tuple[str, Path]:
    """``(source, path)`` of a scenario slug: a user scenario (``user_dir``, default
    :func:`default_user_dir`) shadows a built-in one; :class:`NotFoundError` otherwise."""
    return _locate(slug, user_dir or default_user_dir())


def load_document(scenario: str | Path, user_dir: Path | None = None) -> tuple[str, ScenarioDocument]:
    """``(source, document)`` of a user or built-in scenario slug, or of a YAML file path
    (source ``file``) — how the API and the CLI resolve a scenario."""
    text = str(scenario)
    if isinstance(scenario, Path) or text.endswith((".yaml", ".yml")):
        from app.scenarios.loader import load_scenario

        return "file", load_scenario(scenario)
    source, path = locate(text, user_dir)
    base = path.parent if source == SOURCE_USER else scenario_loader.scenarios_dir()
    return source, load_scenario_text(path.read_text(encoding="utf-8"), base_dir=base)


def write_user_file(doc: ScenarioDocument, user_dir: Path | None = None) -> Path:
    """Write a user scenario file ``<user_dir>/<slug>.yaml`` (atomic replace); the ``scenario``
    table is written when an election is created from it (or by the API editor)."""
    path = _user_path(user_dir or default_user_dir(), doc.scenario.slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(dump_scenario(doc), encoding="utf-8")
    tmp.replace(path)
    return path


def slug_exists(slug: str, user_dir: Path | None = None) -> bool:
    """True when a user or built-in scenario has this slug."""
    try:
        locate(slug, user_dir)
    except (NotFoundError, ScenarioError):
        return False
    return True


def _elections_by_slug(session: Session) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for slug, eid in session.execute(
        select(Scenario.slug, Election.id)
        .join(Election, Election.scenario_id == Scenario.id)
        .order_by(Election.id)
    ).all():
        base = slug.split("--")[0]
        out.setdefault(base, []).append(int(eid))
    return out


def _frame_or_none(session: Session) -> Any:
    from app.services.runtime import get_frame

    try:
        return get_frame(session)
    except DataNotPreparedError:
        return None


def _problems(session: Session, doc: ScenarioDocument) -> list[str] | None:
    frame = _frame_or_none(session)
    return None if frame is None else validate_scenario(doc, frame)


# =========================================================================== read
def list_all(session: Session, user_dir: Path | None = None) -> dict[str, Any]:
    """``GET /api/scenarios`` — built-in and user scenario files (invalid files listed with their
    error) plus scenario documents only present in the database, with the elections using them."""
    udir = user_dir or default_user_dir()
    used = _elections_by_slug(session)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, infos in (
        (SOURCE_BUILTIN, list_scenarios()),
        (SOURCE_USER, list_scenarios(udir) if udir.exists() else []),
    ):
        for s in infos:
            seen.add(s.slug)
            items.append(
                {
                    "slug": s.slug,
                    "name": s.name,
                    "year": s.year,
                    "election_type": s.election_type,
                    "seed": s.seed,
                    "description": s.description,
                    "source": source,
                    "read_only": source == SOURCE_BUILTIN,
                    "file": s.path.name,
                    "valid": s.valid,
                    "error": s.error,
                    "elections": used.get(s.slug, []),
                }
            )
    for row in session.scalars(select(Scenario).order_by(Scenario.id)):
        if row.slug in seen or row.slug.split("--")[0] in seen:
            continue
        items.append(
            {
                "slug": row.slug,
                "name": row.name,
                "year": row.year,
                "election_type": None,
                "seed": int(row.seed),
                "description": row.description,
                "source": SOURCE_DATABASE,
                "read_only": True,
                "file": None,
                "valid": True,
                "error": None,
                "elections": used.get(row.slug, []),
            }
        )
    return {
        "data_category": FICTIONAL,
        "count": len(items),
        "operations": available_operations(),
        "scenarios": items,
    }


def _doc_payload(
    session: Session, source: str, text: str, doc: ScenarioDocument, *, path: Path | None
) -> dict[str, Any]:
    problems = _problems(session, doc)
    row = session.scalar(select(Scenario).where(Scenario.slug == doc.scenario.slug))
    return {
        "data_category": FICTIONAL,
        "slug": doc.scenario.slug,
        "name": doc.scenario.name,
        "year": doc.scenario.year,
        "election_type": doc.scenario.election_type,
        "seed": doc.scenario.seed,
        "source": source,
        "read_only": source != SOURCE_USER,
        "file": None if path is None else path.name,
        "yaml": text,
        "document": scenario_to_dict(doc),
        "hash": config_hash(text),
        "validation": {
            "ok": problems == [] if problems is not None else None,
            "problems": problems,
            "geography_checked": problems is not None,
        },
        "database": None
        if row is None
        else {"id": row.id, "hash": row.document_hash, "updated_at": iso(row.updated_at)},
        "elections": _elections_by_slug(session).get(doc.scenario.slug, []),
        "operations": available_operations(),
    }


def get(session: Session, slug: str, user_dir: Path | None = None) -> dict[str, Any]:
    """``GET /api/scenarios/{slug}`` — the document as YAML text and parsed JSON, validation
    against the loaded geography and the elections created from it."""
    udir = user_dir or default_user_dir()
    try:
        source, path = _locate(slug, udir)
    except NotFoundError:
        row = session.scalar(select(Scenario).where(Scenario.slug == _check_slug(slug)))
        if row is None:
            raise
        doc = load_scenario_text(row.document)
        return _doc_payload(session, SOURCE_DATABASE, row.document, doc, path=None)
    raw = path.read_text(encoding="utf-8")
    doc = load_scenario_text(raw, base_dir=path.parent)
    return _doc_payload(session, source, raw, doc, path=path)


def export_yaml(session: Session, slug: str, user_dir: Path | None = None) -> tuple[str, str]:
    """``GET /api/scenarios/{slug}/export`` → ``(filename, fully merged YAML)``."""
    data = get(session, slug, user_dir)
    doc = ScenarioDocument.model_validate(data["document"])
    return f"{doc.scenario.slug}.yaml", dump_scenario(doc)


# =========================================================================== parse / validate
def _parse(
    *,
    yaml_text: str | None = None,
    document: Mapping[str, Any] | None = None,
) -> ScenarioDocument:
    if (yaml_text is None) == (document is None):
        raise ScenarioError("pass exactly one of 'yaml' or 'document'")
    if yaml_text is not None:
        return load_scenario_text(yaml_text, base_dir=get_settings().config_dir)
    try:
        return ScenarioDocument.model_validate(dict(document or {}))
    except PydanticValidationError as exc:
        raise ScenarioError(f"invalid scenario document:\n{exc}") from exc


def validate(
    session: Session,
    *,
    yaml_text: str | None = None,
    document: Mapping[str, Any] | None = None,
    base: str | None = None,
    edits: Sequence[Mapping[str, Any]] | None = None,
    user_dir: Path | None = None,
) -> dict[str, Any]:
    """``POST /api/scenarios/validate`` — schema + geography validation of a document (YAML or
    JSON), or of ``edits`` applied to the scenario ``base``; never raises for invalid input."""
    try:
        if base is not None:
            data = get(session, base, user_dir)
            doc = ScenarioDocument.model_validate(data["document"])
            if yaml_text is not None or document is not None:
                doc = _parse(yaml_text=yaml_text, document=document)
        else:
            doc = _parse(yaml_text=yaml_text, document=document)
        changes: list[str] = []
        if edits:
            res = apply_edits(doc, list(edits))
            doc, changes = res.document, res.changes
    except (ScenarioError, yaml.YAMLError) as exc:
        return {
            "data_category": FICTIONAL,
            "ok": False,
            "schema_ok": False,
            "errors": [str(exc)],
            "problems": None,
            "changes": [],
            "document": None,
        }
    problems = _problems(session, doc)
    return {
        "data_category": FICTIONAL,
        "ok": not problems,
        "schema_ok": True,
        "errors": [],
        "problems": problems,
        "geography_checked": problems is not None,
        "changes": changes,
        "slug": doc.scenario.slug,
        "document": scenario_to_dict(doc),
        "yaml": dump_scenario(doc),
    }


# =========================================================================== write
def _save(session: Session, doc: ScenarioDocument, user_dir: Path) -> Path:
    """Write the user file and the ``scenario`` row (flushes)."""
    text = dump_scenario(doc)
    path = write_user_file(doc, user_dir)
    h = config_hash(text)
    slug = doc.scenario.slug
    row = session.scalar(select(Scenario).where(Scenario.slug == slug))
    in_use = (
        row is not None
        and (
            session.scalar(select(func.count()).select_from(Election).where(Election.scenario_id == row.id))
            or 0
        )
        > 0
    )
    values = {
        "name": doc.scenario.name[:160],
        "description": doc.scenario.description,
        "year": doc.scenario.year,
        "seed": doc.scenario.seed,
        "document": text,
        "document_hash": h,
        "is_fictional": True,
    }
    if row is None:
        session.add(Scenario(slug=slug, **values))
    elif row.document_hash == h:
        pass
    elif not in_use:
        for k, v in values.items():
            setattr(row, k, v)
        row.updated_at = utcnow()
    else:
        vslug = f"{slug}--{h[:8]}"[:80]
        if session.scalar(select(Scenario).where(Scenario.slug == vslug)) is None:
            session.add(Scenario(slug=vslug, parent_id=row.id, **values))
    session.flush()
    log.info("scenario saved", extra={"ctx": {"slug": slug, "hash": h, "file": str(path)}})
    return path


def create(
    session: Session,
    *,
    yaml_text: str | None = None,
    document: Mapping[str, Any] | None = None,
    slug: str | None = None,
    user_dir: Path | None = None,
) -> dict[str, Any]:
    """``POST /api/scenarios`` — create / import a user scenario (``slug`` overrides the
    document's).  Conflicts with an existing built-in or user slug → :class:`ScenarioConflictError`."""
    udir = user_dir or default_user_dir()
    doc = _parse(yaml_text=yaml_text, document=document)
    if slug is not None and slug != doc.scenario.slug:
        data = doc.model_dump(mode="json")
        data["scenario"]["slug"] = _check_slug(slug)
        doc = ScenarioDocument.model_validate(data)
    s = doc.scenario.slug
    if _builtin_path(s) is not None or _user_path(udir, s).exists():
        raise ScenarioConflictError(f"scenario {s!r} already exists (update it or choose another slug)")
    path = _save(session, doc, udir)
    session.commit()
    return get(session, s, udir) | {"file": path.name}


def update(
    session: Session,
    slug: str,
    *,
    yaml_text: str | None = None,
    document: Mapping[str, Any] | None = None,
    edits: Sequence[Mapping[str, Any]] | None = None,
    user_dir: Path | None = None,
) -> dict[str, Any]:
    """``PUT /api/scenarios/{slug}`` — replace a user scenario's document and/or apply editor
    operations (``edits``).  Built-in scenarios are read-only (duplicate them first)."""
    udir = user_dir or default_user_dir()
    source, path = _locate(slug, udir)
    if source != SOURCE_USER:
        raise ScenarioConflictError(f"scenario {slug!r} is built-in and read-only; duplicate it to edit")
    if yaml_text is not None or document is not None:
        doc = _parse(yaml_text=yaml_text, document=document)
    else:
        doc = load_scenario_text(path.read_text(encoding="utf-8"), base_dir=path.parent)
    changes: list[str] = []
    if edits:
        res = apply_edits(doc, list(edits))
        doc, changes = res.document, res.changes
    if doc.scenario.slug != slug:
        raise ScenarioError(
            f"the document's slug {doc.scenario.slug!r} differs from {slug!r} (use duplicate)"
        )
    _save(session, doc, udir)
    session.commit()
    return get(session, slug, udir) | {"changes": changes}


def duplicate(
    session: Session,
    slug: str,
    new_slug: str,
    *,
    seed: int | None = None,
    name: str | None = None,
    user_dir: Path | None = None,
) -> dict[str, Any]:
    """``POST /api/scenarios/{slug}/duplicate`` — copy a scenario to a new user scenario."""
    udir = user_dir or default_user_dir()
    data = get(session, slug, udir)
    doc = ScenarioDocument.model_validate(data["document"])
    new = duplicate_scenario(doc, _check_slug(new_slug), seed, name=name)
    if _builtin_path(new_slug) is not None or _user_path(udir, new_slug).exists():
        raise ScenarioConflictError(f"scenario {new_slug!r} already exists")
    _save(session, new, udir)
    session.commit()
    return get(session, new_slug, udir)


def delete(session: Session, slug: str, user_dir: Path | None = None) -> dict[str, Any]:
    """``DELETE /api/scenarios/{slug}`` — delete a user scenario file (and its unused database
    row); scenarios used by elections or built-in ones cannot be deleted."""
    udir = user_dir or default_user_dir()
    source, path = _locate(slug, udir)
    if source != SOURCE_USER:
        raise ScenarioConflictError(f"scenario {slug!r} is built-in and cannot be deleted")
    if _elections_by_slug(session).get(slug):
        raise ScenarioConflictError(f"scenario {slug!r} was used to create elections and cannot be deleted")
    row = session.scalar(select(Scenario).where(Scenario.slug == slug))
    if row is not None:
        session.delete(row)
    path.unlink()
    session.commit()
    return {"data_category": FICTIONAL, "slug": slug, "deleted": True}


def load_for_election(session: Session, slug: str, user_dir: Path | None = None) -> ScenarioDocument:
    """The document to create an election from: a user or built-in scenario by slug."""
    _check_slug(slug)
    return load_document(slug, user_dir)[1]
