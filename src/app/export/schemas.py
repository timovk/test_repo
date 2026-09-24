"""Versioned, stable export schemas (ordered columns, logical types, descriptions, provenance).

Every dataset the application exports (CSV/JSON files, ``/api/export/{id}/{dataset}``) has an
:class:`ExportSchema` in :data:`SCHEMAS`.  A schema fixes the exact column order, each column's
logical type (:class:`ColumnType`), nullability, output precision, allowed values and range, a
human description, and the dataset's :class:`~app.core.constitution.DataCategory` (with optional
per-column overrides, e.g. REAL geographic identifiers inside a SIMULATED results table).

Stability contract: a schema's column list never changes without incrementing its ``version``;
:meth:`ExportSchema.fingerprint` changes whenever names/types/order change, and the test-suite
pins both.  Consumers can rely on (name, version) → identical columns.

* :func:`conform` — order columns, cast to the schema dtypes, add missing nullable columns,
  reject (or drop, with ``allow_unknown``) unknown columns;
* :func:`validate` — list every violation (order, dtype, nulls, allowed values, ranges, key
  uniqueness); :func:`assert_valid` raises :class:`SchemaError`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd

from app.core.constitution import DataCategory, RaceStatus, RaceType
from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger

log = get_logger(__name__)

#: Version of the schema registry as a whole (bumped when a schema is added or removed).
REGISTRY_VERSION: int = 1


class SchemaError(ValidationError):
    """A DataFrame cannot be conformed to / does not validate against an export schema."""


class ColumnType(StrEnum):
    """Logical column types and their pandas representation (see :data:`PANDAS_DTYPES`)."""

    INT = "int"
    FLOAT = "float"
    STR = "str"
    BOOL = "bool"
    DATE = "date"
    DATETIME = "datetime"


#: pandas dtype used for each logical type after :func:`conform` (nullable extension dtypes).
PANDAS_DTYPES: dict[ColumnType, str] = {
    ColumnType.INT: "Int64",
    ColumnType.FLOAT: "float64",
    ColumnType.STR: "string",
    ColumnType.BOOL: "boolean",
    ColumnType.DATE: "datetime64[ns]",
    ColumnType.DATETIME: "datetime64[ns]",
}

DEFAULT_DECIMALS: int = 6


@dataclass(frozen=True)
class Column:
    """One export column."""

    name: str
    type: ColumnType
    description: str
    nullable: bool = True
    #: Floats are rounded to this many decimals on output (default :data:`DEFAULT_DECIMALS`).
    decimals: int | None = None
    allowed: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    #: Per-column provenance when it differs from the schema's data category.
    data_category: DataCategory | None = None

    def to_dict(self, default_category: DataCategory) -> dict[str, Any]:
        """JSON-ready column descriptor (used in the JSON envelope and the docs)."""
        out: dict[str, Any] = {
            "name": self.name,
            "type": self.type.value,
            "nullable": self.nullable,
            "description": self.description,
            "data_category": (self.data_category or default_category).value,
        }
        if self.type is ColumnType.FLOAT:
            out["decimals"] = self.decimals if self.decimals is not None else DEFAULT_DECIMALS
        if self.allowed is not None:
            out["allowed"] = list(self.allowed)
        return out


@dataclass(frozen=True)
class ExportSchema:
    """A versioned export schema.  ``key`` columns define the deterministic row order and must
    be unique per row."""

    name: str
    version: int
    data_category: DataCategory
    description: str
    columns: tuple[Column, ...]
    key: tuple[str, ...]
    aliases: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"schema {self.name}: duplicate column names")
        bad = [k for k in self.key if k not in names]
        if bad:
            raise ValueError(f"schema {self.name}: key columns {bad} not in schema")

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        raise KeyError(f"schema {self.name} has no column {name!r}")

    def fingerprint(self) -> str:
        """Short hash of (name, version, ordered column names and types)."""
        payload = json.dumps(
            [self.name, self.version, [[c.name, c.type.value, c.nullable] for c in self.columns]]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def describe(self) -> dict[str, Any]:
        """JSON-ready description of the schema."""
        return {
            "schema": self.name,
            "schema_version": self.version,
            "data_category": self.data_category.value,
            "description": self.description,
            "key": list(self.key),
            "fingerprint": self.fingerprint(),
            "columns": [c.to_dict(self.data_category) for c in self.columns],
        }


# =========================================================================== column library
_INT, _FLOAT, _STR, _BOOL = ColumnType.INT, ColumnType.FLOAT, ColumnType.STR, ColumnType.BOOL
_DATE, _DATETIME = ColumnType.DATE, ColumnType.DATETIME
REAL, DERIVED, FICTIONAL = DataCategory.REAL, DataCategory.DERIVED, DataCategory.FICTIONAL

_RACE_TYPES = tuple(rt.value for rt in RaceType)
_RACE_STATUSES = tuple(s.value for s in RaceStatus)
_FLIP_STATUSES = ("hold", "flip", "new", "undecided")


def _share(name: str, desc: str, nullable: bool = True) -> Column:
    return Column(name, _FLOAT, desc, nullable=nullable, decimals=6, minimum=0.0, maximum=1.0)


def _pp(name: str, desc: str) -> Column:
    return Column(name, _FLOAT, desc, decimals=4)


def _count(name: str, desc: str, nullable: bool = True, category: DataCategory | None = None) -> Column:
    return Column(name, _INT, desc, nullable=nullable, minimum=0, data_category=category)


ELECTION_ID = Column(
    "election_id", _INT, "Election id (database key of the simulated election).", nullable=False
)
YEAR = Column("year", _INT, "Election year.", nullable=False)
RACE_CODE = Column(
    "race_code", _STR, "Race code, e.g. PRES-NB, HOUSE-NB-07, SEN-NB-1, GOV-NB.", nullable=False
)
RACE_TYPE = Column("race_type", _STR, "Race type (RaceType).", nullable=False, allowed=_RACE_TYPES)
PROVINCE_CODE = Column("province_code", _STR, "Province code (null for national rows).", data_category=REAL)
PROVINCE_NAME = Column("province_name", _STR, "Province name.", data_category=REAL)
LINE_KEY = Column("line_key", _STR, "Stable key of the ballot line within the race.", nullable=False)
CANDIDATE = Column("candidate", _STR, "Ballot name (FICTIONAL candidate or ticket).", data_category=FICTIONAL)
PARTY_CODE = Column(
    "party_code", _STR, "FICTIONAL party code (null for independents).", data_category=FICTIONAL
)


def _results_columns(
    level: str, geo_desc: str, geo_category: DataCategory, extra_geo: tuple[Column, ...] = ()
) -> tuple[Column, ...]:
    return (
        ELECTION_ID,
        YEAR,
        RACE_CODE,
        RACE_TYPE,
        Column("level", _STR, "Geographic level of the row.", nullable=False, allowed=(level,)),
        Column("geo_code", _STR, geo_desc, nullable=False, data_category=geo_category),
        Column("geo_name", _STR, "Name of the geographic unit.", data_category=geo_category),
        PROVINCE_CODE,
        *extra_geo,
        LINE_KEY,
        CANDIDATE,
        PARTY_CODE,
        _count("votes", "Valid votes for the line at this level.", nullable=False),
        _share("share", "Share of valid votes at this level (0–1).", nullable=False),
        _count("valid_votes", "Valid votes cast in the race at this level.", nullable=False),
        _count("eligible", "Eligible voters (DERIVED estimate from REAL CBS data).", category=DERIVED),
        _count("ballots_cast", "Ballots cast in the race at this level (valid + blank + invalid)."),
        _share("turnout", "ballots_cast / eligible."),
        Column("winner", _BOOL, "True if the line won at this level (plurality).", nullable=False),
    )


_RESULTS_KEY = ("election_id", "race_code", "geo_code", "line_key")

_SUMMARY_COLUMNS: tuple[Column, ...] = (
    Column("winner_line_key", _STR, "Line key of the winner (null without votes)."),
    Column("winner_candidate", _STR, "Winner's ballot name.", data_category=FICTIONAL),
    Column("winner_party", _STR, "Winner's party code (null for an independent).", data_category=FICTIONAL),
    _count("winner_votes", "Winner's votes."),
    _share("winner_share", "Winner's share of valid votes."),
    Column("runner_up_candidate", _STR, "Runner-up's ballot name.", data_category=FICTIONAL),
    Column("runner_up_party", _STR, "Runner-up's party code.", data_category=FICTIONAL),
    _count("runner_up_votes", "Runner-up's votes (0 when uncontested)."),
    Column("margin_votes", _INT, "Winner votes − runner-up votes.", minimum=0),
    _pp("margin_pp", "Top-two margin in percentage points of valid votes."),
    Column("tied", _BOOL, "Exact tie between the top two (winner decided by lot/recount)."),
    _count("valid_votes", "Valid votes in the race."),
    _count("eligible", "Eligible voters (DERIVED estimate).", category=DERIVED),
    _count("ballots_cast", "Ballots cast in the race."),
    _share("turnout", "ballots_cast / eligible."),
    Column(
        "previous_winner_party",
        _STR,
        "Party that held the seat before (previous election or incumbent).",
        data_category=FICTIONAL,
    ),
    Column(
        "flip_status", _STR, "hold | flip | new (no previous holder) | undecided.", allowed=_FLIP_STATUSES
    ),
    Column("flipped", _BOOL, "True when the seat changed party."),
    Column("incumbent_candidate", _STR, "Incumbent's ballot name if running.", data_category=FICTIONAL),
    Column("incumbent_party", _STR, "Incumbent's party code.", data_category=FICTIONAL),
    Column("is_open_seat", _BOOL, "No incumbent on the ballot."),
    Column("incumbent_won", _BOOL, "Incumbent re-elected (null when unknown or open seat)."),
)


def _schema(
    name: str,
    description: str,
    columns: Iterable[Column],
    key: Iterable[str],
    *,
    category: DataCategory = DataCategory.SIMULATED,
    version: int = 1,
    aliases: Iterable[str] = (),
) -> ExportSchema:
    return ExportSchema(
        name=name,
        version=version,
        data_category=category,
        description=description,
        columns=tuple(columns),
        key=tuple(key),
        aliases=tuple(aliases),
    )


_SCHEMA_LIST: tuple[ExportSchema, ...] = (
    _schema(
        "national_results",
        "National results per race × ballot line (SIMULATED).",
        _results_columns("national", "National code 'NL'.", REAL),
        _RESULTS_KEY,
        aliases=("national",),
    ),
    _schema(
        "province_results",
        "Province-level results per race × ballot line (SIMULATED).",
        _results_columns("province", "Province code (e.g. NB).", REAL),
        _RESULTS_KEY,
        aliases=("provinces",),
    ),
    _schema(
        "municipality_results",
        "Municipality-level results per race × ballot line (SIMULATED; REAL CBS municipality codes).",
        _results_columns("municipality", "CBS municipality code (e.g. GM0855).", REAL),
        _RESULTS_KEY,
        aliases=("municipalities",),
    ),
    _schema(
        "unit_results",
        "Precinct (CBS buurt) results per race × ballot line (SIMULATED; REAL CBS buurt codes).",
        _results_columns(
            "unit",
            "CBS buurt code (e.g. BU08550101).",
            REAL,
            (Column("municipality_code", _STR, "CBS municipality code of the buurt.", data_category=REAL),),
        ),
        _RESULTS_KEY,
        aliases=("units", "precinct_results"),
    ),
    _schema(
        "district_results",
        "House district results per race × ballot line (SIMULATED; FICTIONAL district codes).",
        _results_columns("district", "House district code (e.g. NB-07).", FICTIONAL),
        _RESULTS_KEY,
        aliases=("district_lines",),
    ),
    _schema(
        "house_results",
        "One row per House district race: winner, margin, flip and incumbency (SIMULATED).",
        (
            ELECTION_ID,
            YEAR,
            RACE_CODE,
            Column(
                "district_code",
                _STR,
                "House district code (e.g. NB-07).",
                nullable=False,
                data_category=FICTIONAL,
            ),
            Column("district_name", _STR, "Descriptive district name.", data_category=FICTIONAL),
            PROVINCE_CODE,
            *_SUMMARY_COLUMNS,
        ),
        ("election_id", "district_code"),
        aliases=("house",),
    ),
    _schema(
        "senate_results",
        "One row per Senate race (seat): class, winner, margin, flip and incumbency (SIMULATED).",
        (
            ELECTION_ID,
            YEAR,
            RACE_CODE,
            PROVINCE_CODE,
            PROVINCE_NAME,
            Column(
                "seat_number",
                _INT,
                "Seat number within the province (1 or 2).",
                minimum=1,
                data_category=FICTIONAL,
            ),
            Column("senate_class", _INT, "Senate class (1, 2 or 3).", minimum=1, data_category=FICTIONAL),
            Column("is_special", _BOOL, "Special election for an unexpired term."),
            *_SUMMARY_COLUMNS,
        ),
        ("election_id", "race_code"),
        aliases=("senate",),
    ),
    _schema(
        "governor_results",
        "One row per governor race: winner, margin, flip and incumbency (SIMULATED).",
        (ELECTION_ID, YEAR, RACE_CODE, PROVINCE_CODE, PROVINCE_NAME, *_SUMMARY_COLUMNS),
        ("election_id", "race_code"),
        aliases=("governors",),
    ),
    _schema(
        "electoral_votes",
        "Electoral votes per province: EV, winner and margin of the province contest (SIMULATED).",
        (
            ELECTION_ID,
            YEAR,
            RACE_CODE,
            Column("province_code", _STR, "Province code.", nullable=False, data_category=REAL),
            PROVINCE_NAME,
            Column(
                "electoral_votes",
                _INT,
                "Electoral votes of the province (House seats + senators).",
                nullable=False,
                minimum=0,
                data_category=FICTIONAL,
            ),
            Column("winner_line_key", _STR, "Line key of the ticket that carried the province."),
            Column("winner_candidate", _STR, "Ticket that carried the province.", data_category=FICTIONAL),
            Column("winner_party", _STR, "Party of that ticket.", data_category=FICTIONAL),
            _count("winner_votes", "Winner's votes in the province."),
            _share("winner_share", "Winner's share of valid votes."),
            Column("runner_up_party", _STR, "Runner-up's party code.", data_category=FICTIONAL),
            Column("margin_votes", _INT, "Winner votes − runner-up votes.", minimum=0),
            _pp("margin_pp", "Top-two margin in percentage points."),
            Column(
                "decided_by",
                _STR,
                "popular_vote | lot | recount | contingent (null when unknown).",
                allowed=("popular_vote", "lot", "recount", "contingent"),
            ),
        ),
        ("election_id", "province_code"),
        aliases=("ev", "electoral_college"),
    ),
    _schema(
        "reporting_timeline",
        "Election-night reporting batches in order (SIMULATED).",
        (
            ELECTION_ID,
            Column("seq", _INT, "Sequence number of the batch (1-based).", nullable=False, minimum=0),
            Column(
                "sim_time_s",
                _FLOAT,
                "Seconds after the first poll closing.",
                nullable=False,
                decimals=3,
                minimum=0.0,
            ),
            Column("time", _DATETIME, "Simulated local wall-clock time of the batch."),
            Column(
                "municipality_code",
                _STR,
                "CBS municipality code reporting.",
                nullable=False,
                data_category=REAL,
            ),
            Column("municipality_name", _STR, "Municipality name.", data_category=REAL),
            PROVINCE_CODE,
            Column("kind", _STR, "batch | final | correction.", allowed=("batch", "final", "correction")),
            _count("ballots_in_batch", "Ballots counted in this batch.", nullable=False),
            _count("cumulative_ballots", "Ballots counted nationally after this batch."),
            _share(
                "municipality_fraction_after",
                "Fraction of the municipality's ballots counted after the batch.",
            ),
            _share("national_fraction", "Fraction of all ballots counted nationally after the batch."),
        ),
        ("election_id", "seq"),
        aliases=("timeline",),
    ),
    _schema(
        "race_calls",
        "Race-call log: every change of a race's call state with its evidence (SIMULATED).",
        (
            ELECTION_ID,
            Column("seq", _INT, "Reporting-event sequence number at the call.", nullable=False, minimum=0),
            Column("sim_time_s", _FLOAT, "Seconds after the first poll closing.", decimals=3, minimum=0.0),
            Column("called_at", _DATETIME, "Simulated local time of the call."),
            RACE_CODE,
            Column("race_type", _STR, "Race type (RaceType).", allowed=_RACE_TYPES),
            Column("status", _STR, "Call state (RaceStatus).", nullable=False, allowed=_RACE_STATUSES),
            Column("line_key", _STR, "Line key of the projected/called candidate (null when none)."),
            CANDIDATE,
            PARTY_CODE,
            Column(
                "reporting_pct",
                _FLOAT,
                "Percent of expected ballots counted in the race.",
                decimals=4,
                minimum=0.0,
                maximum=100.0,
            ),
            Column(
                "leader_margin_pct", _FLOAT, "Leader's margin in percentage points at the call.", decimals=4
            ),
            Column(
                "win_probability",
                _FLOAT,
                "Calling engine's win probability for the leader.",
                decimals=6,
                minimum=0.0,
                maximum=1.0,
            ),
            Column("is_manual", _BOOL, "Manual override.", nullable=False),
            Column("superseded", _BOOL, "Superseded by a later call state."),
        ),
        ("election_id", "seq", "race_code", "status", "is_manual"),
        aliases=("calls",),
    ),
    _schema(
        "montecarlo_summary",
        "Monte Carlo forecast per race × line (SIMULATED model estimates, not predictions).",
        (
            Column("run_id", _INT, "Forecast run id.", nullable=False),
            ELECTION_ID,
            Column("seed", _INT, "Root seed of the run."),
            Column("n_simulations", _INT, "Number of simulated elections.", minimum=1),
            RACE_CODE,
            Column("race_type", _STR, "Race type (RaceType).", allowed=_RACE_TYPES),
            LINE_KEY,
            CANDIDATE,
            PARTY_CODE,
            _share("win_probability", "Share of simulations won.", nullable=False),
            _share("mean_share", "Mean vote share."),
            _share("p05_share", "5th percentile of vote share."),
            _share("p50_share", "Median vote share."),
            _share("p95_share", "95th percentile of vote share."),
            Column("mean_votes", _FLOAT, "Mean votes.", decimals=2, minimum=0.0),
        ),
        ("run_id", "race_code", "line_key"),
    ),
    _schema(
        "montecarlo_distribution",
        "Monte Carlo outcome distributions (EV, seats, popular vote …) as histograms (SIMULATED).",
        (
            Column("run_id", _INT, "Forecast run id.", nullable=False),
            ELECTION_ID,
            Column(
                "subject",
                _STR,
                "ev | house_seats | senate_seats | popular_vote_share | governor_wins.",
                nullable=False,
                allowed=("ev", "house_seats", "senate_seats", "popular_vote_share", "governor_wins"),
            ),
            Column("key", _STR, "Party code or ticket key.", nullable=False),
            Column("value", _FLOAT, "Outcome value (EV, seats, share …).", nullable=False, decimals=6),
            _share("probability", "Probability of the value.", nullable=False),
        ),
        ("run_id", "subject", "key", "value"),
    ),
    _schema(
        "polling_averages",
        "Polling averages of FICTIONAL polls (SIMULATED).",
        (
            ELECTION_ID,
            Column("as_of", _DATE, "Date of the average.", nullable=False),
            Column(
                "poll_type",
                _STR,
                "Poll type (national_president, province_president, house_district …).",
                nullable=False,
            ),
            Column("geo_code", _STR, "Province/district code (null = national)."),
            Column("label", _STR, "Party code or candidate label.", nullable=False),
            PARTY_CODE,
            Column(
                "average_pct",
                _FLOAT,
                "Weighted average (percent).",
                nullable=False,
                decimals=4,
                minimum=0.0,
                maximum=100.0,
            ),
            Column("lower_pct", _FLOAT, "Lower bound of the uncertainty band (percent).", decimals=4),
            Column("upper_pct", _FLOAT, "Upper bound of the uncertainty band (percent).", decimals=4),
            _count("n_polls", "Number of polls in the average."),
            Column(
                "effective_sample_size",
                _FLOAT,
                "Effective sample size after weighting.",
                decimals=1,
                minimum=0.0,
            ),
            Column("trend_pct_per_week", _FLOAT, "Estimated trend (percentage points per week).", decimals=4),
        ),
        ("election_id", "as_of", "poll_type", "geo_code", "label"),
    ),
    _schema(
        "polls",
        "Individual FICTIONAL polls, one row per poll × result line (SIMULATED).",
        (
            Column("poll_id", _INT, "Poll id.", nullable=False),
            Column("election_id", _INT, "Election id (null for polls not tied to an election)."),
            Column("pollster", _STR, "FICTIONAL pollster name.", data_category=FICTIONAL),
            Column("poll_type", _STR, "Poll type.", nullable=False),
            Column("geo_code", _STR, "Province code (null = national)."),
            Column("district_code", _STR, "House district code for district polls.", data_category=FICTIONAL),
            Column("start_date", _DATE, "Fieldwork start."),
            Column("end_date", _DATE, "Fieldwork end.", nullable=False),
            _count("sample_size", "Sample size."),
            Column("population", _STR, "LV | RV | A.", allowed=("LV", "RV", "A")),
            Column("method", _STR, "online | phone | mixed | ivr | panel."),
            Column(
                "margin_of_error",
                _FLOAT,
                "Reported margin of error (percentage points).",
                decimals=2,
                minimum=0.0,
            ),
            Column("undecided_pct", _FLOAT, "Undecided (percent).", decimals=2, minimum=0.0, maximum=100.0),
            Column("label", _STR, "Party code or candidate label.", nullable=False),
            PARTY_CODE,
            Column(
                "value_pct",
                _FLOAT,
                "Result (percent).",
                nullable=False,
                decimals=2,
                minimum=0.0,
                maximum=100.0,
            ),
            Column("is_fictional", _BOOL, "Always true: polls are generated."),
        ),
        ("poll_id", "label"),
    ),
    _schema(
        "districts",
        "House district plan statistics (FICTIONAL districts over REAL geography).",
        (
            Column("plan_id", _INT, "District plan id.", nullable=False),
            Column("district_code", _STR, "District code (e.g. NB-07).", nullable=False),
            Column("province_code", _STR, "Province code.", nullable=False, data_category=REAL),
            Column("number", _INT, "District number within the province.", minimum=1),
            Column("name", _STR, "Descriptive district name."),
            _count("population", "Population (REAL CBS counts summed over units).", category=REAL),
            _count("eligible_voters_est", "Estimated eligible voters.", category=DERIVED),
            Column(
                "target_population",
                _FLOAT,
                "Ideal district population in the province.",
                decimals=1,
                minimum=0.0,
                data_category=DERIVED,
            ),
            Column(
                "deviation_pct",
                _FLOAT,
                "(population − target) / target × 100.",
                decimals=4,
                data_category=DERIVED,
            ),
            Column("area_km2", _FLOAT, "Area in km².", decimals=4, minimum=0.0, data_category=DERIVED),
            _share("polsby_popper", "Polsby–Popper compactness (0–1)."),
            _share("reock", "Reock compactness (0–1)."),
            _share("convex_hull_ratio", "Area / convex-hull area (0–1)."),
            _count("n_units", "CBS buurten in the district."),
            _count("n_municipalities", "Municipalities touched."),
            _count("n_split_municipalities", "Municipalities split with other districts."),
            _share("urban_share", "Population share in CBS urbanity classes 1–2."),
            _share("rural_share", "Population share in CBS urbanity classes 4–5."),
            Column("is_contiguous", _BOOL, "District is one connected piece (water links allowed)."),
            _count("n_components", "Connected components."),
            Column(
                "centroid_lon", _FLOAT, "Centroid longitude (EPSG:4326).", decimals=6, data_category=DERIVED
            ),
            Column(
                "centroid_lat", _FLOAT, "Centroid latitude (EPSG:4326).", decimals=6, data_category=DERIVED
            ),
        ),
        ("plan_id", "district_code"),
        category=FICTIONAL,
        aliases=("district_plan",),
    ),
    _schema(
        "apportionment",
        "House seats and electoral votes per province (FICTIONAL constitution applied to REAL population).",
        (
            Column("apportionment_id", _INT, "Apportionment id.", nullable=False),
            Column("method", _STR, "Apportionment method (huntington_hill, hamilton, …).", nullable=False),
            Column("province_code", _STR, "Province code.", nullable=False, data_category=REAL),
            PROVINCE_NAME,
            _count("population", "Apportionment population (REAL CBS).", nullable=False, category=REAL),
            Column("quota", _FLOAT, "Exact proportional share of House seats.", decimals=6, minimum=0.0),
            _count("seats", "House seats.", nullable=False),
            _count("senators", "Senators (2 per province canonically)."),
            _count("electoral_votes", "Electoral votes = seats + senators.", nullable=False),
            Column("persons_per_seat", _FLOAT, "Population per House seat.", decimals=2, minimum=0.0),
        ),
        ("apportionment_id", "province_code"),
        category=FICTIONAL,
    ),
    _schema(
        "swing",
        "Swing between two elections per geo × party (lineage-aware; SIMULATED).",
        (
            Column("election_id_prev", _INT, "Previous election id.", nullable=False),
            Column("year_prev", _INT, "Previous election year.", nullable=False),
            Column("election_id_curr", _INT, "Current election id.", nullable=False),
            Column("year_curr", _INT, "Current election year.", nullable=False),
            Column("race_code", _STR, "Race code (null when races of a family are pooled)."),
            Column(
                "race_family", _STR, "Race family (PRESIDENT includes its province contests).", nullable=False
            ),
            Column(
                "level",
                _STR,
                "Geographic level.",
                nullable=False,
                allowed=("unit", "municipality", "district", "province", "national"),
            ),
            Column("geo_code", _STR, "Code of the geographic unit (current code set).", nullable=False),
            Column("geo_name", _STR, "Name of the geographic unit."),
            PROVINCE_CODE,
            Column(
                "party",
                _STR,
                "Party code (_IND = independents pooled).",
                nullable=False,
                data_category=FICTIONAL,
            ),
            _count("votes_prev", "Votes in the previous election."),
            _count("votes_curr", "Votes in the current election."),
            Column("vote_change", _INT, "votes_curr − votes_prev."),
            _pp("vote_change_pct", "vote_change / votes_prev × 100."),
            _share("share_prev", "Share in the previous election."),
            _share("share_curr", "Share in the current election."),
            _pp("swing_pp", "(share_curr − share_prev) × 100."),
            Column(
                "status",
                _STR,
                "both | new_party | dropped_party | new_geo | dropped_geo.",
                nullable=False,
                allowed=("both", "new_party", "dropped_party", "new_geo", "dropped_geo"),
            ),
            Column(
                "winner_prev",
                _STR,
                "Winning party at the geo in the previous election.",
                data_category=FICTIONAL,
            ),
            Column(
                "winner_curr",
                _STR,
                "Winning party at the geo in the current election.",
                data_category=FICTIONAL,
            ),
            Column(
                "flip_status",
                _STR,
                "hold | flip | new | dropped | undecided.",
                allowed=("hold", "flip", "new", "dropped", "undecided"),
            ),
            _share("turnout_prev", "Turnout in the previous election."),
            _share("turnout_curr", "Turnout in the current election."),
            _pp("turnout_change_pp", "(turnout_curr − turnout_prev) × 100."),
        ),
        ("election_id_prev", "election_id_curr", "race_family", "race_code", "level", "geo_code", "party"),
        aliases=("compare",),
    ),
)

#: Registry of export schemas by canonical name.
SCHEMAS: dict[str, ExportSchema] = {s.name: s for s in _SCHEMA_LIST}
#: Alias → canonical schema name (API dataset names such as ``provinces`` or ``calls``).
ALIASES: dict[str, str] = {alias: s.name for s in _SCHEMA_LIST for alias in s.aliases}


def list_schemas() -> list[str]:
    """Canonical schema names in registry order."""
    return list(SCHEMAS)


def get_schema(name: str | ExportSchema) -> ExportSchema:
    """Schema by canonical name or alias (an :class:`ExportSchema` passes through)."""
    if isinstance(name, ExportSchema):
        return name
    key = ALIASES.get(name, name)
    if key not in SCHEMAS:
        raise NotFoundError(f"unknown export schema {name!r}; known: {sorted(SCHEMAS)}")
    return SCHEMAS[key]


# =========================================================================== casting
_TRUE = {"true", "t", "1", "yes", "y"}
_FALSE = {"false", "f", "0", "no", "n"}


def _to_bool(value: Any) -> Any:
    if value is None or value is pd.NA or (isinstance(value, float) and np.isnan(value)):
        return pd.NA
    if isinstance(value, bool | np.bool_):
        return bool(value)
    if isinstance(value, int | np.integer | float | np.floating) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
        if v == "":
            return pd.NA
    raise ValueError(f"cannot interpret {value!r} as a boolean")


def _cast(series: pd.Series, col: Column) -> pd.Series:
    s = series.reset_index(drop=True)
    t = col.type
    if t is ColumnType.INT:
        num = pd.to_numeric(s.astype(object).where(s.notna(), None), errors="raise")
        vals = num.to_numpy(dtype=float)
        finite = ~np.isnan(vals)
        if (np.abs(vals[finite] - np.round(vals[finite])) > 1e-9).any():
            raise ValueError("non-integral values")
        return pd.Series(np.round(vals), dtype="float64").astype("Int64")
    if t is ColumnType.FLOAT:
        num = pd.to_numeric(s.astype(object).where(s.notna(), None), errors="raise")
        return num.astype("float64")
    if t is ColumnType.STR:
        obj = s.astype(object)
        obj = obj.where(obj.notna(), None)
        return pd.Series(
            [None if v is None else str(v.value if hasattr(v, "value") else v) for v in obj], dtype="string"
        )
    if t is ColumnType.BOOL:
        if s.dtype == bool or str(s.dtype) == "boolean":
            return s.astype("boolean")
        return pd.Series([_to_bool(v) for v in s.astype(object)], dtype="boolean")
    if t in (ColumnType.DATE, ColumnType.DATETIME):
        obj = s.astype(object).where(s.notna(), None)
        dt = pd.to_datetime(obj, errors="raise")
        if t is ColumnType.DATE:
            if getattr(dt.dt, "tz", None) is not None:
                dt = dt.dt.tz_localize(None)
            norm = dt.dt.normalize()
            if ((dt != norm) & dt.notna()).any():
                raise ValueError("date column contains times")
            return norm.astype("datetime64[ns]")
        return dt
    raise ValueError(f"unsupported column type {t}")  # pragma: no cover


def conform(df: pd.DataFrame, schema: str | ExportSchema, *, allow_unknown: bool = False) -> pd.DataFrame:
    """Return a new frame with exactly the schema's columns, in order, cast to its dtypes.

    Missing nullable columns are added as nulls; a missing non-nullable column raises
    :class:`SchemaError`.  Unknown columns raise unless ``allow_unknown`` (then they are dropped).
    The index is reset; row order is preserved (writers sort by the schema key).
    """
    sch = get_schema(schema)
    unknown = [c for c in df.columns if c not in sch.names]
    if unknown and not allow_unknown:
        raise SchemaError(f"{sch.name}: unknown columns {unknown} (pass allow_unknown=True to drop them)")
    if unknown:
        log.debug("conform %s: dropping unknown columns %s", sch.name, unknown)
    missing = [c.name for c in sch.columns if c.name not in df.columns and not c.nullable]
    if missing:
        raise SchemaError(f"{sch.name}: missing required columns {missing}")
    data: dict[str, pd.Series] = {}
    problems: list[str] = []
    for col in sch.columns:
        src = df[col.name] if col.name in df.columns else pd.Series([None] * len(df), dtype=object)
        try:
            data[col.name] = _cast(src, col)
        except (ValueError, TypeError) as exc:
            problems.append(f"{col.name}: cannot cast to {col.type.value} ({exc})")
    if problems:
        raise SchemaError(f"{sch.name}: conform failed", problems)
    return pd.DataFrame(data, index=pd.RangeIndex(len(df)))


def _dtype_ok(series: pd.Series, col: Column) -> bool:
    if col.type in (ColumnType.DATE, ColumnType.DATETIME):
        return pd.api.types.is_datetime64_any_dtype(series.dtype)
    return str(series.dtype) == PANDAS_DTYPES[col.type]


def validate(df: pd.DataFrame, schema: str | ExportSchema) -> list[str]:
    """Every violation of ``schema`` by ``df`` (empty list = valid).

    Checks exact column order, dtypes (as produced by :func:`conform`), nulls in non-nullable
    columns, allowed values, numeric ranges and uniqueness of the key columns.
    """
    sch = get_schema(schema)
    problems: list[str] = []
    if tuple(df.columns) != sch.names:
        problems.append(f"columns {list(df.columns)} differ from schema order {list(sch.names)}")
        return problems
    for col in sch.columns:
        s = df[col.name]
        if not _dtype_ok(s, col):
            problems.append(f"{col.name}: dtype {s.dtype} (expected {PANDAS_DTYPES[col.type]})")
            continue
        nulls = s.isna().to_numpy()
        if not col.nullable and nulls.any():
            problems.append(f"{col.name}: {int(nulls.sum())} null values in a non-nullable column")
        present = s[~nulls]
        if col.allowed is not None and len(present):
            bad = sorted(set(present.astype(str)) - set(col.allowed))
            if bad:
                problems.append(f"{col.name}: values {bad[:5]} not in {list(col.allowed)}")
        if col.type in (ColumnType.INT, ColumnType.FLOAT) and len(present):
            vals = present.to_numpy(dtype=float)
            vals = vals[np.isfinite(vals)]
            tol = 1e-9
            if col.minimum is not None and (vals < col.minimum - tol).any():
                problems.append(f"{col.name}: values below minimum {col.minimum}")
            if col.maximum is not None and (vals > col.maximum + tol).any():
                problems.append(f"{col.name}: values above maximum {col.maximum}")
    if sch.key and len(df) and df.duplicated(list(sch.key)).any():
        problems.append(
            f"duplicate key {list(sch.key)} values: {int(df.duplicated(list(sch.key)).sum())} rows"
        )
    return problems


def assert_valid(df: pd.DataFrame, schema: str | ExportSchema) -> None:
    """Raise :class:`SchemaError` listing every violation of ``schema``."""
    sch = get_schema(schema)
    problems = validate(df, sch)
    if problems:
        raise SchemaError(f"{sch.name}: validation failed", problems)


def sort_rows(df: pd.DataFrame, schema: str | ExportSchema) -> pd.DataFrame:
    """Rows sorted by the schema key (nulls last, stable) with a fresh index."""
    sch = get_schema(schema)
    if not sch.key or df.empty:
        return df.reset_index(drop=True)
    return df.sort_values(list(sch.key), na_position="last", kind="mergesort").reset_index(drop=True)


def describe_all() -> dict[str, Any]:
    """JSON-ready description of every schema (for docs and ``/api/export`` discovery)."""
    return {"registry_version": REGISTRY_VERSION, "schemas": [s.describe() for s in SCHEMAS.values()]}


def schema_table(schema: str | ExportSchema) -> pd.DataFrame:
    """Column table of a schema (name, type, nullable, category, description)."""
    sch = get_schema(schema)
    return pd.DataFrame([c.to_dict(sch.data_category) for c in sch.columns])


def column_names(schema: str | ExportSchema) -> tuple[str, ...]:
    """Ordered column names of a schema."""
    return get_schema(schema).names


def schemas_by_category() -> Mapping[str, list[str]]:
    """Schema names grouped by data category."""
    out: dict[str, list[str]] = {}
    for s in SCHEMAS.values():
        out.setdefault(s.data_category.value, []).append(s.name)
    return out
