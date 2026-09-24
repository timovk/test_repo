"""Named political regions: a FICTIONAL assumption layer over REAL places.

``config/regions.yaml`` defines regions such as ``bible_belt`` or ``catholic_south`` by lists of
REAL municipality *names* (with a province code for disambiguation), province codes and/or CBS
codes.  Party specs attach utility shifts to these names (``PartySpec.regions``).  The regions
themselves are modelling assumptions about political culture, not official statistics.

Municipality names change with municipal mergers, so the resolver matches names leniently
(case, diacritics, punctuation, CBS disambiguation suffixes such as ``Bergen (NH.)``, known
aliases like *Den Haag* → *'s-Gravenhage*) against the current :class:`GeographyFrame`, and warns
about names it cannot resolve instead of failing.

    regions = resolve_regions(frame)                 # config/regions.yaml
    regions.muni_mask("bible_belt")                  # (M,) bool
    regions.resolution_rate                          # share of listed names that resolved
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import load_config, parse_config
from app.core.logging import get_logger
from app.geography.frame import GeographyFrame

log = get_logger(__name__)

#: Default location of the region definitions (relative to ``config/``).
REGIONS_CONFIG_FILE = "regions.yaml"

#: Built-in aliases (normalised name → normalised official CBS name).
BUILTIN_ALIASES: dict[str, str] = {
    "den haag": "s gravenhage",
    "the hague": "s gravenhage",
    "den bosch": "s hertogenbosch",
    "nuenen": "nuenen gerwen en nederwetten",
    "sudwest friesland": "sudwest fryslan",
    "noordoost friesland": "noardeast fryslan",
}

_SUFFIX_RE = re.compile(r"\s*\((?:[a-z]{1,3}\.?)\)\s*$", flags=re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_name(name: str) -> str:
    """Canonical matching key of a municipality name.

    Lower-case, diacritics removed, CBS disambiguation suffixes (``"(NH.)"``, ``"(L.)"``, ``"(O.)"``)
    dropped, punctuation collapsed to single spaces: ``"'s-Gravenhage"`` → ``"s gravenhage"``,
    ``"Súdwest-Fryslân"`` → ``"sudwest fryslan"``, ``"Bergen (NH.)"`` → ``"bergen"``.
    """
    s = unicodedata.normalize("NFKD", str(name))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _SUFFIX_RE.sub("", s.strip())
    s = s.lower().replace("’", "'")
    s = _NON_ALNUM_RE.sub(" ", s).strip()
    return s


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MunicipalityRef(_Model):
    """A municipality referenced by (REAL) name, optionally disambiguated by province code."""

    name: str
    province: str | None = None


class RegionSpec(_Model):
    """One named region (union of provinces, named municipalities and CBS codes, minus exclusions)."""

    label: str
    description: str | None = None
    provinces: list[str] = Field(default_factory=list)
    municipalities: list[MunicipalityRef | str] = Field(default_factory=list)
    codes: list[str] = Field(default_factory=list)
    exclude: list[MunicipalityRef | str] = Field(default_factory=list)


class RegionsConfig(_Model):
    """Root of ``config/regions.yaml``."""

    version: int = 1
    description: str | None = None
    #: Extra aliases: any spelling → official CBS name.
    aliases: dict[str, str] = Field(default_factory=dict)
    regions: dict[str, RegionSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _names(self) -> RegionsConfig:
        bad = [k for k in self.regions if not re.fullmatch(r"[a-z][a-z0-9_]*", k)]
        if bad:
            raise ValueError(f"region ids must be snake_case identifiers: {bad}")
        return self


@dataclass
class ResolvedRegions:
    """Region membership resolved against a frame."""

    names: list[str]
    labels: dict[str, str]
    #: (M, R) boolean membership of municipalities.
    membership: np.ndarray
    unresolved: dict[str, list[str]] = field(default_factory=dict)
    listed_count: int = 0
    resolved_count: int = 0

    @property
    def resolution_rate(self) -> float:
        """Share of explicitly listed municipality names that resolved (1.0 when none listed)."""
        return self.resolved_count / self.listed_count if self.listed_count else 1.0

    def index(self, name: str) -> int:
        return self.names.index(name)

    def muni_mask(self, name: str) -> np.ndarray:
        """(M,) boolean mask of the region's municipalities."""
        return self.membership[:, self.index(name)]

    def muni_indices(self, name: str) -> np.ndarray:
        return np.flatnonzero(self.muni_mask(name))

    def unit_mask(self, frame: GeographyFrame, name: str) -> np.ndarray:
        """(U,) boolean mask of the region's units."""
        return self.muni_mask(name)[frame.unit_muni]

    def __contains__(self, name: object) -> bool:
        return name in self.names

    @classmethod
    def empty(cls, frame: GeographyFrame) -> ResolvedRegions:
        return cls(names=[], labels={}, membership=np.zeros((frame.n_munis, 0), dtype=bool))


def load_regions_config(path: str | Path | None = None) -> RegionsConfig:
    """Load ``config/regions.yaml`` (empty config when absent)."""
    return load_config(path or REGIONS_CONFIG_FILE, RegionsConfig, optional=True)


def regions_config_from_dict(data: dict) -> RegionsConfig:
    return parse_config(data, RegionsConfig, source="<regions>")


def _name_index(frame: GeographyFrame) -> dict[str, list[int]]:
    idx: dict[str, list[int]] = {}
    for i, n in enumerate(frame.muni_names):
        idx.setdefault(normalize_name(n), []).append(i)
    return idx


def resolve_municipality(
    frame: GeographyFrame,
    ref: MunicipalityRef | str,
    aliases: dict[str, str] | None = None,
    _index: dict[str, list[int]] | None = None,
) -> int | None:
    """Index of the municipality ``ref`` names in ``frame`` (``None`` when not found/ambiguous).

    ``ref`` may be a CBS code (``"GM0855"``), a name, or a :class:`MunicipalityRef`.
    """
    if isinstance(ref, str):
        ref = MunicipalityRef(name=ref)
    code = ref.name.strip()
    if re.fullmatch(r"GM\d{4}", code):
        return frame._muni_lookup.get(code)
    index = _index if _index is not None else _name_index(frame)
    all_aliases = {
        **BUILTIN_ALIASES,
        **{normalize_name(k): normalize_name(v) for k, v in (aliases or {}).items()},
    }
    key = normalize_name(code)
    candidates = index.get(key) or index.get(all_aliases.get(key, "\0"), [])
    if ref.province is not None:
        try:
            p = frame.province_index(ref.province)
        except ValueError:
            return None
        candidates = [i for i in candidates if int(frame.muni_province[i]) == p]
    if len(candidates) == 1:
        return candidates[0]
    return None


def resolve_regions(
    frame: GeographyFrame, config: RegionsConfig | None = None, *, log_warnings: bool = True
) -> ResolvedRegions:
    """Resolve every region of ``config`` (default: ``config/regions.yaml``) against ``frame``."""
    config = config if config is not None else load_regions_config()
    names = list(config.regions)
    M = frame.n_munis
    membership = np.zeros((M, len(names)), dtype=bool)
    index = _name_index(frame)
    unresolved: dict[str, list[str]] = {}
    listed = resolved = 0
    for r, (rid, spec) in enumerate(config.regions.items()):
        col = membership[:, r]
        for pcode in spec.provinces:
            if pcode not in frame.province_codes:
                unresolved.setdefault(rid, []).append(f"province {pcode}")
                continue
            col |= frame.muni_province == frame.province_index(pcode)
        for code in spec.codes:
            i = frame._muni_lookup.get(code)
            if i is None:
                unresolved.setdefault(rid, []).append(code)
            else:
                col[i] = True
        for ref in spec.municipalities:
            listed += 1
            i = resolve_municipality(frame, ref, config.aliases, index)
            if i is None:
                label = (
                    ref
                    if isinstance(ref, str)
                    else (f"{ref.name} ({ref.province})" if ref.province else ref.name)
                )
                unresolved.setdefault(rid, []).append(str(label))
            else:
                resolved += 1
                col[i] = True
        for ref in spec.exclude:
            i = resolve_municipality(frame, ref, config.aliases, index)
            if i is not None:
                col[i] = False
        membership[:, r] = col
    if unresolved and log_warnings:
        for rid, items in unresolved.items():
            log.warning("region %s: %d unresolved reference(s): %s", rid, len(items), ", ".join(items[:12]))
    return ResolvedRegions(
        names=names,
        labels={k: v.label for k, v in config.regions.items()},
        membership=membership,
        unresolved=unresolved,
        listed_count=listed,
        resolved_count=resolved,
    )
