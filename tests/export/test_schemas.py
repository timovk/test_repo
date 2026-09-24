"""Schema stability (exact column order, versions, fingerprints), conform and validate."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.core.constitution import DataCategory
from app.core.errors import NotFoundError
from app.export import schemas as S

_RESULTS = [
    "election_id",
    "year",
    "race_code",
    "race_type",
    "level",
    "geo_code",
    "geo_name",
    "province_code",
    "line_key",
    "candidate",
    "party_code",
    "votes",
    "share",
    "valid_votes",
    "eligible",
    "ballots_cast",
    "turnout",
    "winner",
]
_SUMMARY = [
    "winner_line_key",
    "winner_candidate",
    "winner_party",
    "winner_votes",
    "winner_share",
    "runner_up_candidate",
    "runner_up_party",
    "runner_up_votes",
    "margin_votes",
    "margin_pp",
    "tied",
    "valid_votes",
    "eligible",
    "ballots_cast",
    "turnout",
    "previous_winner_party",
    "flip_status",
    "flipped",
    "incumbent_candidate",
    "incumbent_party",
    "is_open_seat",
    "incumbent_won",
]

#: Golden column lists.  Changing any of these requires bumping the schema version.
EXPECTED_COLUMNS: dict[str, list[str]] = {
    "national_results": _RESULTS,
    "province_results": _RESULTS,
    "municipality_results": _RESULTS,
    "unit_results": [*_RESULTS[:8], "municipality_code", *_RESULTS[8:]],
    "district_results": _RESULTS,
    "house_results": [
        "election_id",
        "year",
        "race_code",
        "district_code",
        "district_name",
        "province_code",
        *_SUMMARY,
    ],
    "senate_results": [
        "election_id",
        "year",
        "race_code",
        "province_code",
        "province_name",
        "seat_number",
        "senate_class",
        "is_special",
        *_SUMMARY,
    ],
    "governor_results": ["election_id", "year", "race_code", "province_code", "province_name", *_SUMMARY],
    "electoral_votes": [
        "election_id",
        "year",
        "race_code",
        "province_code",
        "province_name",
        "electoral_votes",
        "winner_line_key",
        "winner_candidate",
        "winner_party",
        "winner_votes",
        "winner_share",
        "runner_up_party",
        "margin_votes",
        "margin_pp",
        "decided_by",
    ],
    "reporting_timeline": [
        "election_id",
        "seq",
        "sim_time_s",
        "time",
        "municipality_code",
        "municipality_name",
        "province_code",
        "kind",
        "ballots_in_batch",
        "cumulative_ballots",
        "municipality_fraction_after",
        "national_fraction",
    ],
    "race_calls": [
        "election_id",
        "seq",
        "sim_time_s",
        "called_at",
        "race_code",
        "race_type",
        "status",
        "line_key",
        "candidate",
        "party_code",
        "reporting_pct",
        "leader_margin_pct",
        "win_probability",
        "is_manual",
        "superseded",
    ],
    "montecarlo_summary": [
        "run_id",
        "election_id",
        "seed",
        "n_simulations",
        "race_code",
        "race_type",
        "line_key",
        "candidate",
        "party_code",
        "win_probability",
        "mean_share",
        "p05_share",
        "p50_share",
        "p95_share",
        "mean_votes",
    ],
    "montecarlo_distribution": ["run_id", "election_id", "subject", "key", "value", "probability"],
    "polling_averages": [
        "election_id",
        "as_of",
        "poll_type",
        "geo_code",
        "label",
        "party_code",
        "average_pct",
        "lower_pct",
        "upper_pct",
        "n_polls",
        "effective_sample_size",
        "trend_pct_per_week",
    ],
    "polls": [
        "poll_id",
        "election_id",
        "pollster",
        "poll_type",
        "geo_code",
        "district_code",
        "start_date",
        "end_date",
        "sample_size",
        "population",
        "method",
        "margin_of_error",
        "undecided_pct",
        "label",
        "party_code",
        "value_pct",
        "is_fictional",
    ],
    "districts": [
        "plan_id",
        "district_code",
        "province_code",
        "number",
        "name",
        "population",
        "eligible_voters_est",
        "target_population",
        "deviation_pct",
        "area_km2",
        "polsby_popper",
        "reock",
        "convex_hull_ratio",
        "n_units",
        "n_municipalities",
        "n_split_municipalities",
        "urban_share",
        "rural_share",
        "is_contiguous",
        "n_components",
        "centroid_lon",
        "centroid_lat",
    ],
    "apportionment": [
        "apportionment_id",
        "method",
        "province_code",
        "province_name",
        "population",
        "quota",
        "seats",
        "senators",
        "electoral_votes",
        "persons_per_seat",
    ],
    "swing": [
        "election_id_prev",
        "year_prev",
        "election_id_curr",
        "year_curr",
        "race_code",
        "race_family",
        "level",
        "geo_code",
        "geo_name",
        "province_code",
        "party",
        "votes_prev",
        "votes_curr",
        "vote_change",
        "vote_change_pct",
        "share_prev",
        "share_curr",
        "swing_pp",
        "status",
        "winner_prev",
        "winner_curr",
        "flip_status",
        "turnout_prev",
        "turnout_curr",
        "turnout_change_pp",
    ],
}

#: Pinned fingerprints (name, version, ordered names, types, nullability).
EXPECTED_FINGERPRINTS: dict[str, str] = {
    "national_results": "c8e6355ad48fddb4",
    "province_results": "144b26c7a6d6efa3",
    "municipality_results": "144112fda081d0e7",
    "unit_results": "c810ea8a1b320416",
    "district_results": "055e8c5543f33b9b",
    "house_results": "723c9eab543eefa3",
    "senate_results": "6bbeb93a1b00e0c3",
    "governor_results": "8cda429a05c11886",
    "electoral_votes": "c8e4cf9706eb817e",
    "reporting_timeline": "4afc85a31af4f437",
    "race_calls": "695c134a420e1e3b",
    "montecarlo_summary": "14d24c00a0cb2c18",
    "montecarlo_distribution": "31482a73deb874b6",
    "polling_averages": "ae0d3c80927bd2a5",
    "polls": "5aebf733c0ccf47e",
    "districts": "d2108fffe3341000",
    "apportionment": "389ec5ca0414ffa8",
    "swing": "4fc00f1324914823",
}

API_DATASETS = [
    "national",
    "provinces",
    "municipalities",
    "units",
    "house",
    "senate",
    "governors",
    "electoral_votes",
    "timeline",
    "calls",
    "montecarlo_summary",
    "montecarlo_distribution",
    "polling_averages",
    "polls",
]


def test_registry_is_complete_and_ordered() -> None:
    assert S.list_schemas() == list(EXPECTED_COLUMNS)


@pytest.mark.parametrize("name", list(EXPECTED_COLUMNS))
def test_exact_column_order_version_and_fingerprint(name: str) -> None:
    sch = S.get_schema(name)
    assert list(sch.names) == EXPECTED_COLUMNS[name]
    assert sch.version == 1
    assert sch.fingerprint() == EXPECTED_FINGERPRINTS[name]
    assert set(sch.key) <= set(sch.names)
    assert all(c.description for c in sch.columns)


def test_api_dataset_names_resolve() -> None:
    for ds in API_DATASETS:
        assert S.get_schema(ds).name in S.SCHEMAS
    assert S.get_schema("calls").name == "race_calls"
    with pytest.raises(NotFoundError):
        S.get_schema("nope")
    sch = S.get_schema("house_results")
    assert S.get_schema(sch) is sch


def test_data_categories_and_per_column_provenance() -> None:
    cats = S.schemas_by_category()
    assert set(cats["FICTIONAL"]) == {"districts", "apportionment"}
    assert "FICTIONAL" not in {
        s.data_category.value for s in S.SCHEMAS.values() if s.name.endswith("_results")
    }
    muni = S.get_schema("municipality_results")
    assert muni.data_category is DataCategory.SIMULATED
    assert muni.column("geo_code").data_category is DataCategory.REAL
    assert muni.column("eligible").data_category is DataCategory.DERIVED
    assert S.get_schema("district_results").column("geo_code").data_category is DataCategory.FICTIONAL
    desc = muni.column("share").to_dict(muni.data_category)
    assert desc == {
        "name": "share",
        "type": "float",
        "nullable": False,
        "description": "Share of valid votes at this level (0–1).",
        "data_category": "SIMULATED",
        "decimals": 6,
    }
    d = S.describe_all()
    assert d["registry_version"] == S.REGISTRY_VERSION and len(d["schemas"]) == len(S.SCHEMAS)
    assert list(S.schema_table("polls")["name"]) == EXPECTED_COLUMNS["polls"]


def test_schema_definition_errors() -> None:
    col = S.Column("a", S.ColumnType.INT, "x")
    with pytest.raises(ValueError):
        S.ExportSchema("bad", 1, DataCategory.SIMULATED, "", (col, col), ("a",))
    with pytest.raises(ValueError):
        S.ExportSchema("bad", 1, DataCategory.SIMULATED, "", (col,), ("b",))


def test_conform_orders_casts_and_adds_missing(polls_df: pd.DataFrame) -> None:
    shuffled = polls_df[list(reversed(polls_df.columns))].drop(columns=["district_code", "undecided_pct"])
    shuffled["sample_size"] = shuffled["sample_size"].astype(str)
    shuffled["is_fictional"] = ["true", "1", "yes"]
    out = S.conform(shuffled, "polls")
    assert list(out.columns) == EXPECTED_COLUMNS["polls"]
    assert str(out["sample_size"].dtype) == "Int64" and out["sample_size"].tolist() == [1200, 1200, 800]
    assert out["district_code"].isna().all() and str(out["district_code"].dtype) == "string"
    assert out["is_fictional"].tolist() == [True, True, True]
    assert pd.api.types.is_datetime64_any_dtype(out["end_date"])
    assert pd.isna(out["election_id"].iloc[2])
    assert S.validate(out, "polls") == []


def test_conform_rejects_unknown_and_missing(polls_df: pd.DataFrame) -> None:
    with pytest.raises(S.SchemaError, match="unknown columns"):
        S.conform(polls_df.assign(secret=1), "polls")
    out = S.conform(polls_df.assign(secret=1), "polls", allow_unknown=True)
    assert "secret" not in out.columns
    with pytest.raises(S.SchemaError, match="missing required"):
        S.conform(polls_df.drop(columns=["poll_id"]), "polls")


def test_conform_cast_errors(polls_df: pd.DataFrame) -> None:
    with pytest.raises(S.SchemaError) as exc:
        S.conform(polls_df.assign(sample_size=[1.5, 2, 3], is_fictional=["maybe", "yes", "no"]), "polls")
    assert any("sample_size" in p for p in exc.value.problems)
    assert any("is_fictional" in p for p in exc.value.problems)
    with pytest.raises(S.SchemaError):
        S.conform(polls_df.assign(end_date=[datetime(2032, 1, 1, 12)] * 3), "polls")


def test_validate_reports_violations(polls_df: pd.DataFrame) -> None:
    good = S.conform(polls_df, "polls")
    assert S.validate(good[list(reversed(good.columns))], "polls")[0].startswith("columns")
    bad = good.copy()
    bad["poll_id"] = bad["poll_id"].astype("float64")
    assert any("dtype" in p for p in S.validate(bad, "polls"))
    bad = good.copy()
    bad.loc[0, "label"] = pd.NA
    assert any("null values" in p for p in S.validate(bad, "polls"))
    bad = good.copy()
    bad.loc[0, "population"] = "XX"
    assert any("not in" in p for p in S.validate(bad, "polls"))
    bad = good.copy()
    bad.loc[0, "value_pct"] = 101.0
    assert any("above maximum" in p for p in S.validate(bad, "polls"))
    bad = good.copy()
    bad.loc[2, ["poll_id", "label"]] = [7, "A"]
    assert any("duplicate key" in p for p in S.validate(bad, "polls"))
    with pytest.raises(S.SchemaError):
        S.assert_valid(bad, "polls")


def test_sort_rows_by_key_nulls_last(timeline_df: pd.DataFrame) -> None:
    out = S.sort_rows(S.conform(timeline_df, "timeline"), "timeline")
    assert out["seq"].tolist() == [1, 2, 3]
    df = S.conform(
        pd.DataFrame(
            {
                "run_id": [1, 1],
                "election_id": [None, 2],
                "subject": ["ev", "ev"],
                "key": ["A", "A"],
                "value": [np.nan, 1.0],
                "probability": [0.5, 0.5],
            }
        ),
        "montecarlo_distribution",
    )
    assert S.sort_rows(df, "montecarlo_distribution")["value"].isna().tolist() == [False, True]


def test_docs_schema_reference_is_current() -> None:
    """docs/ANALYTICS.md §5.4 is generated from the registry; it must list every current fingerprint."""
    from app.core.settings import PROJECT_ROOT

    doc = (PROJECT_ROOT / "docs" / "ANALYTICS.md").read_text(encoding="utf-8")
    for sch in S.SCHEMAS.values():
        assert f"| `{sch.name}` | {sch.version} | {sch.data_category.value} |" in doc
        assert sch.fingerprint() in doc, f"regenerate the schema reference for {sch.name}"
