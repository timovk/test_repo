"""Composable row filters for standard results frames.

Each constructor returns a :class:`Filter` — a named, vectorised row predicate.  Filters compose
with ``&`` (and), ``|`` (or) and ``~`` (not) and are applied by calling them::

    from app.analytics import filters as F

    f = F.years(since=2028) & F.parties("PA", "SAP") & F.levels("municipality") & F.provinces("NB")
    tilburg = (F.municipalities("GM0855") & F.race_types("PRESIDENT", family=True))(frame)

:func:`filter_results` offers the same through keyword arguments.  Every value argument accepts a
scalar or an iterable; enum members (``RaceType.HOUSE``) are accepted wherever strings are.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from app.analytics.results import (
    INDEPENDENT_KEY,
    LEVELS,
    ResultsFrameError,
    family_series,
    normalize_values,
    party_key_series,
    race_family,
)
from app.core.constitution import ElectionType, RaceType
from app.core.logging import get_logger

log = get_logger(__name__)

Predicate = Callable[[pd.DataFrame], np.ndarray]


@dataclass(frozen=True)
class Filter:
    """A composable row predicate over a results frame."""

    predicate: Predicate
    label: str

    def mask(self, frame: pd.DataFrame) -> np.ndarray:
        """Boolean row mask (length ``len(frame)``)."""
        m = np.asarray(self.predicate(frame), dtype=bool)
        if m.shape != (len(frame),):
            raise ResultsFrameError(f"filter {self.label} produced a mask of shape {m.shape}")
        return m

    def apply(self, frame: pd.DataFrame) -> pd.DataFrame:
        """Rows of ``frame`` matching the filter (index preserved)."""
        return frame[self.mask(frame)]

    def __call__(self, frame: pd.DataFrame) -> pd.DataFrame:
        return self.apply(frame)

    def __and__(self, other: Filter) -> Filter:
        return Filter(lambda f: self.mask(f) & other.mask(f), f"({self.label} & {other.label})")

    def __or__(self, other: Filter) -> Filter:
        return Filter(lambda f: self.mask(f) | other.mask(f), f"({self.label} | {other.label})")

    def __invert__(self) -> Filter:
        return Filter(lambda f: ~self.mask(f), f"~{self.label}")

    def __repr__(self) -> str:
        return f"Filter({self.label})"


#: The identity filter (matches every row).
ALL = Filter(lambda f: np.ones(len(f), dtype=bool), "all")


def _flatten(values: tuple[Any, ...]) -> list[Any]:
    out: list[Any] = []
    for v in values:
        if isinstance(v, str | RaceType | ElectionType) or not isinstance(v, Iterable):
            out.append(v)
        else:
            out.extend(v)
    return out


def _strings(values: tuple[Any, ...]) -> list[str]:
    return normalize_values(_flatten(values))


def _col(frame: pd.DataFrame, name: str) -> pd.Series:
    if name not in frame.columns:
        raise ResultsFrameError(f"filter needs column {name!r}")
    return frame[name]


# --------------------------------------------------------------------------- constructors
def years(*values: int | Iterable[int], since: int | None = None, until: int | None = None) -> Filter:
    """Rows of the given election years and/or within ``[since, until]`` (inclusive)."""
    ys = [int(v) for v in _flatten(values)]

    def pred(f: pd.DataFrame) -> np.ndarray:
        y = _col(f, "year").to_numpy()
        m = np.isin(y, ys) if ys else np.ones(len(f), dtype=bool)
        if since is not None:
            m &= y >= since
        if until is not None:
            m &= y <= until
        return m

    label = ", ".join(
        [
            *(str(y) for y in ys),
            *([f"since={since}"] if since is not None else []),
            *([f"until={until}"] if until is not None else []),
        ]
    )
    return Filter(pred, f"years({label})")


def elections(*ids: int | Iterable[int]) -> Filter:
    """Rows of the given election ids."""
    vals = [int(v) for v in _flatten(ids)]
    return Filter(lambda f: np.isin(_col(f, "election_id").to_numpy(), vals), f"elections({vals})")


def parties(*codes: str | Iterable[str]) -> Filter:
    """Lines of the given party codes (``_IND`` matches independents)."""
    vals = _strings(codes)
    return Filter(lambda f: party_key_series(f).isin(vals).to_numpy(), f"parties({vals})")


def candidates(*names: str | Iterable[str], contains: bool = False, case_sensitive: bool = False) -> Filter:
    """Lines whose ballot name equals (or, with ``contains``, contains) one of ``names``."""
    vals = _strings(names)
    needles = vals if case_sensitive else [v.casefold() for v in vals]

    def pred(f: pd.DataFrame) -> np.ndarray:
        s = _col(f, "candidate").astype(object).where(f["candidate"].notna(), "").astype(str)
        if not case_sensitive:
            s = s.str.casefold()
        if contains:
            m = np.zeros(len(f), dtype=bool)
            for n in needles:
                m |= s.str.contains(n, regex=False).to_numpy()
            return m
        return s.isin(needles).to_numpy()

    return Filter(pred, f"candidates({vals}{', contains' if contains else ''})")


def provinces(*codes: str | Iterable[str]) -> Filter:
    """Rows inside the given provinces (``province_code``) or the province rows themselves."""
    vals = _strings(codes)

    def pred(f: pd.DataFrame) -> np.ndarray:
        inside = _col(f, "province_code").isin(vals).to_numpy()
        own = ((_col(f, "level") == "province") & _col(f, "geo_code").isin(vals)).to_numpy()
        return inside | own

    return Filter(pred, f"provinces({vals})")


def municipalities(
    *codes: str | Iterable[str], include_units: bool = False, include_races: bool = False
) -> Filter:
    """Municipality-level rows of the given CBS codes (``GM0855``).

    ``include_units`` adds unit rows whose CBS buurt code belongs to the municipality (CBS
    convention ``BU`` + the municipality's four digits + wijk + buurt); ``include_races`` adds
    every row of races held *for* the municipality (``MAYOR-GM0855``, ``COUNCIL-GM0855``).
    """
    vals = _strings(codes)
    digits = {v[2:] for v in vals if v.upper().startswith("GM")}

    def pred(f: pd.DataFrame) -> np.ndarray:
        level = _col(f, "level")
        geo = _col(f, "geo_code").astype(str)
        m = ((level == "municipality") & geo.isin(vals)).to_numpy()
        if include_units:
            m |= ((level == "unit") & geo.str.startswith("BU") & geo.str[2:6].isin(digits)).to_numpy()
        if include_races:
            m |= _col(f, "race_code").astype(str).str.rsplit("-", n=1).str[-1].isin(vals).to_numpy()
        return m

    return Filter(pred, f"municipalities({vals})")


def districts(*codes: str | Iterable[str]) -> Filter:
    """Rows of the House races of the given district codes (``NB-07`` → ``HOUSE-NB-07`` at every
    level) plus district-level rows with those codes."""
    vals = _strings(codes)
    races = [f"HOUSE-{v}" for v in vals]

    def pred(f: pd.DataFrame) -> np.ndarray:
        by_race = _col(f, "race_code").isin(races).to_numpy()
        own = ((_col(f, "level") == "district") & _col(f, "geo_code").isin(vals)).to_numpy()
        return by_race | own

    return Filter(pred, f"districts({vals})")


def race_types(*types: str | RaceType | Iterable[str], family: bool = False) -> Filter:
    """Rows of the given race types; with ``family`` a type matches its whole family
    (``PRESIDENT`` then includes the ``PRESIDENT_PROVINCE`` contests)."""
    vals = _strings(types)
    unknown = sorted(set(vals) - {rt.value for rt in RaceType})
    if unknown:
        raise ValueError(f"unknown race types {unknown}")
    if family:
        fams = sorted({race_family(v) for v in vals})
        return Filter(
            lambda f: family_series(_col(f, "race_type")).isin(fams).to_numpy(), f"race_families({fams})"
        )
    return Filter(lambda f: _col(f, "race_type").astype(str).isin(vals).to_numpy(), f"race_types({vals})")


def race_codes(*codes: str | Iterable[str], prefix: bool = False) -> Filter:
    """Rows of the given race codes (or, with ``prefix``, codes starting with any of them,
    e.g. ``race_codes("HOUSE-NB", prefix=True)``)."""
    vals = _strings(codes)

    def pred(f: pd.DataFrame) -> np.ndarray:
        rc = _col(f, "race_code").astype(str)
        if prefix:
            return rc.str.startswith(tuple(vals)).to_numpy() if vals else np.zeros(len(f), dtype=bool)
        return rc.isin(vals).to_numpy()

    return Filter(pred, f"race_codes({vals}{', prefix' if prefix else ''})")


def levels(*values: str | Iterable[str]) -> Filter:
    """Rows at the given geographic levels (``unit, municipality, district, province, national``)."""
    vals = _strings(values)
    unknown = sorted(set(vals) - set(LEVELS))
    if unknown:
        raise ValueError(f"unknown levels {unknown}; expected {LEVELS}")
    return Filter(lambda f: _col(f, "level").isin(vals).to_numpy(), f"levels({vals})")


def geos(*codes: str | Iterable[str]) -> Filter:
    """Rows whose ``geo_code`` is one of ``codes`` (any level)."""
    vals = _strings(codes)
    return Filter(lambda f: _col(f, "geo_code").isin(vals).to_numpy(), f"geos({vals})")


def winners_only() -> Filter:
    """Winning lines only (``winner`` true)."""
    return Filter(lambda f: _col(f, "winner").astype("boolean").fillna(False).to_numpy(dtype=bool), "winners")


def infer_election_types(frame: pd.DataFrame) -> pd.Series:
    """Election type per row: the ``election_type`` column when present, otherwise inferred per
    election from the races it contains (heuristic): presidential races → ``general``; House →
    ``midterm``; only governor / provincial legislature → ``provincial``; only mayor / council →
    ``municipal``; anything else (e.g. only a special Senate race) → ``special``."""
    if "election_type" in frame.columns:
        return frame["election_type"].astype(str)
    types = frame.groupby("election_id")["race_type"].agg(lambda s: set(s.astype(str)))
    pres = {RaceType.PRESIDENT.value, RaceType.PRESIDENT_PROVINCE.value}
    prov = {RaceType.GOVERNOR.value, RaceType.PROVINCIAL_LEGISLATURE.value}
    muni = {RaceType.MAYOR.value, RaceType.MUNICIPAL_COUNCIL.value}

    def classify(ts: set[str]) -> str:
        if ts & pres:
            return ElectionType.GENERAL.value
        if RaceType.HOUSE.value in ts:
            return ElectionType.MIDTERM.value
        if ts and ts <= prov | muni and ts & prov:
            return ElectionType.PROVINCIAL.value
        if ts and ts <= muni:
            return ElectionType.MUNICIPAL.value
        return ElectionType.SPECIAL.value

    mapping = {eid: classify(ts) for eid, ts in types.items()}
    return frame["election_id"].map(mapping).astype(str)


def election_types(*types: str | ElectionType | Iterable[str]) -> Filter:
    """Rows of elections of the given types (see :func:`infer_election_types`)."""
    vals = _strings(types)
    unknown = sorted(set(vals) - {t.value for t in ElectionType})
    if unknown:
        raise ValueError(f"unknown election types {unknown}")
    return Filter(lambda f: infer_election_types(f).isin(vals).to_numpy(), f"election_types({vals})")


# --------------------------------------------------------------------------- composition
def all_of(*filters: Filter) -> Filter:
    """Conjunction of ``filters`` (``ALL`` when empty)."""
    out = ALL
    for flt in filters:
        out = flt if out is ALL else out & flt
    return out


def any_of(*filters: Filter) -> Filter:
    """Disjunction of ``filters`` (matches nothing when empty)."""
    if not filters:
        return Filter(lambda f: np.zeros(len(f), dtype=bool), "none")
    out = filters[0]
    for flt in filters[1:]:
        out = out | flt
    return out


def build_filter(
    *,
    year: int | Iterable[int] | None = None,
    since_year: int | None = None,
    until_year: int | None = None,
    election_id: int | Iterable[int] | None = None,
    party: str | Iterable[str] | None = None,
    candidate: str | Iterable[str] | None = None,
    province: str | Iterable[str] | None = None,
    municipality: str | Iterable[str] | None = None,
    district: str | Iterable[str] | None = None,
    race_type: str | RaceType | Iterable[str] | None = None,
    family: bool = False,
    race_code: str | Iterable[str] | None = None,
    level: str | Iterable[str] | None = None,
    geo_code: str | Iterable[str] | None = None,
    election_type: str | ElectionType | Iterable[str] | None = None,
    winners: bool = False,
) -> Filter:
    """Conjunction of the filters for every criterion given (None = no restriction)."""
    parts: list[Filter] = []
    if year is not None or since_year is not None or until_year is not None:
        parts.append(years(*(() if year is None else (year,)), since=since_year, until=until_year))
    if election_id is not None:
        parts.append(elections(election_id))
    if party is not None:
        parts.append(parties(party))
    if candidate is not None:
        parts.append(candidates(candidate))
    if province is not None:
        parts.append(provinces(province))
    if municipality is not None:
        parts.append(municipalities(municipality))
    if district is not None:
        parts.append(districts(district))
    if race_type is not None:
        parts.append(race_types(race_type, family=family))
    if race_code is not None:
        parts.append(race_codes(race_code))
    if level is not None:
        parts.append(levels(level))
    if geo_code is not None:
        parts.append(geos(geo_code))
    if election_type is not None:
        parts.append(election_types(election_type))
    if winners:
        parts.append(winners_only())
    return all_of(*parts)


def filter_results(frame: pd.DataFrame, *filters: Filter, **criteria: Any) -> pd.DataFrame:
    """Apply ``filters`` and keyword ``criteria`` (see :func:`build_filter`) conjunctively."""
    flt = all_of(*filters, build_filter(**criteria))
    out = flt(frame)
    log.debug("filter_results %s: %d → %d rows", flt.label, len(frame), len(out))
    return out


__all__ = [
    "ALL",
    "INDEPENDENT_KEY",
    "Filter",
    "all_of",
    "any_of",
    "build_filter",
    "candidates",
    "districts",
    "election_types",
    "elections",
    "filter_results",
    "geos",
    "infer_election_types",
    "levels",
    "municipalities",
    "parties",
    "provinces",
    "race_codes",
    "race_types",
    "winners_only",
    "years",
]
