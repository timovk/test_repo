"""CSV / JSON writers: exact format, round-trips, determinism and bundles."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.core.constitution import DataCategory
from app.core.rng import make_rng
from app.export import schemas as S
from app.export import writers as W

STAMP = "2032-11-04T06:00:00+00:00"


@pytest.fixture()
def dist_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "run_id": [1, 1],
            "election_id": [2, 2],
            "subject": ["ev", "ev"],
            "key": ["B", "A"],
            "value": [100.0, -1e-9],
            "probability": [1 / 3, 0.25],
        }
    )


def test_csv_exact_format(dist_df: pd.DataFrame) -> None:
    text = W.to_csv_text(dist_df, "montecarlo_distribution")
    assert (
        text
        == "run_id,election_id,subject,key,value,probability\n1,2,ev,A,0.0,0.25\n1,2,ev,B,100.0,0.333333\n"
    )


def test_csv_datetimes_bools_and_quoting(timeline_df: pd.DataFrame, polls_df: pd.DataFrame) -> None:
    lines = W.to_csv_text(timeline_df, "timeline").splitlines()
    assert lines[1] == "2,1,60.25,2032-11-03T21:01:00,GM0772,Eindhoven,NB,batch,500,500,0.25,5e-05"
    polls = polls_df.copy()
    polls.loc[0, "pollster"] = 'Bureau "Fictief", Ltd'
    text = W.to_csv_text(polls, "polls")
    assert '"Bureau ""Fictief"", Ltd"' in text
    first = next(csv.reader(io.StringIO(text.splitlines()[1])))
    assert first[6] == "2032-10-01" and first[-1] == "true"


def test_csv_round_trip(tmp_path: Path, timeline_df: pd.DataFrame, polls_df: pd.DataFrame) -> None:
    for df, name in ((timeline_df, "reporting_timeline"), (polls_df, "polls")):
        path = W.write_csv(df, name, tmp_path / f"{name}.csv")
        back = W.read_csv(path, name)
        pd.testing.assert_frame_equal(back, W.prepare(df, name))
        assert S.validate(back, name) == []


def test_csv_determinism_under_row_shuffles(tmp_path: Path, polls_df: pd.DataFrame) -> None:
    a = W.write_csv(polls_df, "polls", tmp_path / "a.csv").read_bytes()
    b = W.write_csv(
        polls_df.iloc[make_rng(0, "test", "shuffle").permutation(len(polls_df))], "polls", tmp_path / "b.csv"
    ).read_bytes()
    c = W.write_csv(polls_df[list(reversed(polls_df.columns))], "polls", tmp_path / "c.csv").read_bytes()
    assert a == b == c
    assert b"\r\n" not in a


def test_float_formatting_is_stable() -> None:
    df = pd.DataFrame(
        {
            "run_id": [1, 1, 1],
            "election_id": 2,
            "subject": ["ev"] * 3,
            "key": ["A", "B", "C"],
            "value": [0.1 + 0.2, 0.3, 1e-7],
            "probability": [0.5, 0.5, 0.0],
        }
    )
    rows = W.to_csv_text(df, "montecarlo_distribution").splitlines()[1:]
    assert [r.split(",")[4] for r in rows] == ["0.3", "0.3", "0.0"]


def test_json_envelope_structure(dist_df: pd.DataFrame) -> None:
    meta = {
        "seed": np.int64(42),
        "election": {"year": 2032, "name": "General"},
        "as_of": date(2032, 11, 3),
        "category": DataCategory.SIMULATED,
        "tags": {"b", "a"},
    }
    env = W.to_json_envelope(dist_df, "montecarlo_distribution", meta, generated_at=STAMP)
    assert list(env) == [
        "schema",
        "schema_version",
        "data_category",
        "generated_at",
        "metadata",
        "columns",
        "rows",
    ]
    assert env["schema"] == "montecarlo_distribution" and env["schema_version"] == 1
    assert env["data_category"] == "SIMULATED" and env["generated_at"] == STAMP
    assert env["metadata"] == {
        "as_of": "2032-11-03",
        "category": "SIMULATED",
        "election": {"name": "General", "year": 2032},
        "seed": 42,
        "tags": ["a", "b"],
    }
    assert [c["name"] for c in env["columns"]] == list(S.get_schema("montecarlo_distribution").names)
    assert env["rows"] == [[1, 2, "ev", "A", 0.0, 0.25], [1, 2, "ev", "B", 100.0, 0.333333]]
    with pytest.raises(TypeError):
        W.to_json_envelope(dist_df, "montecarlo_distribution", {"bad": object()})


def test_json_non_finite_floats_become_null() -> None:
    df = pd.DataFrame(
        {
            "poll_id": [1],
            "poll_type": ["x"],
            "end_date": ["2032-01-01"],
            "label": ["A"],
            "value_pct": [10.0],
            "margin_of_error": [np.inf],
        }
    )
    env = W.to_json_envelope(df, "polls", generated_at=STAMP)
    row = dict(zip([c["name"] for c in env["columns"]], env["rows"][0], strict=True))
    assert row["margin_of_error"] is None and row["end_date"] == "2032-01-01"
    json.loads(W.dumps_envelope(env))


def test_json_round_trip_and_determinism(tmp_path: Path, timeline_df: pd.DataFrame) -> None:
    p1 = W.write_json(
        timeline_df, "timeline", tmp_path / "t1.json", {"election": 2, "seed": 9}, generated_at=STAMP
    )
    p2 = W.write_json(
        timeline_df.iloc[::-1],
        "timeline",
        tmp_path / "t2.json",
        {"seed": 9, "election": 2},
        generated_at=STAMP,
    )
    assert p1.read_bytes() == p2.read_bytes()
    back, head = W.read_json(p1)
    pd.testing.assert_frame_equal(back, W.prepare(timeline_df, "timeline"))
    assert head["schema"] == "reporting_timeline" and head["metadata"] == {"election": 2, "seed": 9}
    assert "rows" not in head
    indented = W.write_json(timeline_df, "timeline", tmp_path / "t3.json", generated_at=STAMP, indent=2)
    assert W.read_json(indented)[0].equals(back)


def test_json_default_timestamp_and_version_check(tmp_path: Path, dist_df: pd.DataFrame) -> None:
    env = W.to_json_envelope(dist_df, "montecarlo_distribution")
    assert env["generated_at"].endswith("+00:00")
    p = W.write_json(dist_df, "montecarlo_distribution", tmp_path / "d.json", generated_at=STAMP)
    doc = json.loads(p.read_text())
    doc["schema_version"] = 99
    p.write_text(json.dumps(doc))
    with pytest.raises(S.SchemaError):
        W.read_json(p)


def test_empty_frames(tmp_path: Path) -> None:
    empty = pd.DataFrame({n: [] for n in S.get_schema("race_calls").names})
    assert W.to_csv_text(empty, "race_calls").count("\n") == 1
    p = W.write_json(empty, "race_calls", tmp_path / "e.json", generated_at=STAMP)
    back, _ = W.read_json(p)
    assert back.empty and list(back.columns) == list(S.get_schema("race_calls").names)


def test_writers_reject_invalid_data(tmp_path: Path, timeline_df: pd.DataFrame) -> None:
    with pytest.raises(S.SchemaError):
        W.write_csv(timeline_df.assign(extra=1), "timeline", tmp_path / "x.csv")
    W.write_csv(timeline_df.assign(extra=1), "timeline", tmp_path / "x.csv", allow_unknown=True)
    with pytest.raises(S.SchemaError):
        W.write_csv(timeline_df.assign(kind="party"), "timeline", tmp_path / "y.csv")
    with pytest.raises(S.SchemaError):
        W.write_json(timeline_df.assign(seq=1), "timeline", tmp_path / "z.json")  # duplicate keys


def test_export_bundle_and_manifest(
    tmp_path: Path, timeline_df: pd.DataFrame, polls_df: pd.DataFrame, dist_df: pd.DataFrame
) -> None:
    frames = {"timeline": timeline_df, "polls": polls_df, "montecarlo_distribution": dist_df}
    meta = {"election": 2, "seed": 20321103}
    paths = W.export_bundle(frames, tmp_path / "a", metadata=meta, generated_at=STAMP)
    names = [p.name for p in paths]
    assert names == [
        "montecarlo_distribution.csv",
        "montecarlo_distribution.json",
        "polls.csv",
        "polls.json",
        "reporting_timeline.csv",
        "reporting_timeline.json",
        "manifest.json",
    ]
    manifest = json.loads(paths[-1].read_text())
    assert manifest["bundle_version"] == W.BUNDLE_VERSION and manifest["metadata"] == meta
    assert manifest["data_categories"] == ["SIMULATED"]
    entry = next(e for e in manifest["files"] if e["file"] == "polls.csv")
    assert entry["rows"] == 3 and entry["fingerprint"] == S.get_schema("polls").fingerprint()
    assert entry["sha256"] == hashlib.sha256((tmp_path / "a" / "polls.csv").read_bytes()).hexdigest()
    again = W.export_bundle(frames, tmp_path / "b", metadata=meta, generated_at=STAMP)
    assert [p.read_bytes() for p in paths] == [p.read_bytes() for p in again]
    only_json = W.export_bundle(
        {"calls": pd.DataFrame(columns=list(S.get_schema("race_calls").names))},
        tmp_path / "c",
        formats=("json",),
        manifest=False,
    )
    assert [p.name for p in only_json] == ["race_calls.json"]


def test_export_bundle_argument_errors(tmp_path: Path, timeline_df: pd.DataFrame) -> None:
    with pytest.raises(ValueError):
        W.export_bundle({"timeline": timeline_df}, tmp_path, formats=("xlsx",))
    with pytest.raises(ValueError):
        W.export_bundle({"timeline": timeline_df, "reporting_timeline": timeline_df}, tmp_path)


def test_large_integers_round_trip_exactly(tmp_path: Path) -> None:
    seed = 2**62 + 1  # not representable as a float64
    df = pd.DataFrame(
        {
            "run_id": [1, 2],
            "election_id": [2, 2],
            "seed": pd.array([seed, None], dtype="Int64"),
            "race_code": ["PRES", "PRES"],
            "line_key": ["t-a", "t-a"],
            "win_probability": [0.5, 0.5],
        }
    )
    assert S.conform(df, "montecarlo_summary")["seed"].iloc[0] == seed
    assert W.to_csv_text(df, "montecarlo_summary").splitlines()[1].split(",")[2] == str(seed)
    back = W.read_csv(W.write_csv(df, "montecarlo_summary", tmp_path / "m.csv"), "montecarlo_summary")
    assert back["seed"].iloc[0] == seed and pd.isna(back["seed"].iloc[1])
    env = W.to_json_envelope(df, "montecarlo_summary", generated_at=STAMP)
    assert env["rows"][0][2] == seed
    back_json, _ = W.read_json(
        W.write_json(df, "montecarlo_summary", tmp_path / "m.json", generated_at=STAMP)
    )
    assert back_json["seed"].iloc[0] == seed
    from_text = S.conform(df.assign(seed=[str(seed), None]), "montecarlo_summary")
    assert from_text["seed"].iloc[0] == seed


def test_datetimes_of_mixed_precision_and_offsets_round_trip(
    tmp_path: Path, timeline_df: pd.DataFrame
) -> None:
    tl = timeline_df.copy()
    tl["time"] = [
        datetime(2032, 11, 3, 21, 2, 0, 250_000),  # sub-second batch times next to whole seconds
        datetime(2032, 11, 3, 21, 1, 0),
        datetime(2032, 11, 3, 22, 0, 0),
    ]
    for fmt in ("csv", "json"):
        path = tmp_path / f"t.{fmt}"
        if fmt == "csv":
            back = W.read_csv(W.write_csv(tl, "timeline", path), "timeline")
        else:
            back = W.read_json(W.write_json(tl, "timeline", path, generated_at=STAMP))[0]
        pd.testing.assert_frame_equal(back, W.prepare(tl, "timeline"))
    aware = tl.assign(time=pd.to_datetime(tl["time"]).dt.tz_localize("Europe/Amsterdam"))
    back = W.read_csv(W.write_csv(aware, "timeline", tmp_path / "a.csv"), "timeline")
    assert back["time"].dt.tz is not None
    assert (
        back["time"].dt.tz_convert("UTC") == W.prepare(aware, "timeline")["time"].dt.tz_convert("UTC")
    ).all()
    dst = pd.DataFrame(  # an election night across the end of daylight saving time: two UTC offsets
        {
            "election_id": [1, 1],
            "seq": [1, 2],
            "sim_time_s": [0.0, 3600.0],
            "time": ["2032-10-31T02:30:00+02:00", "2032-10-31T02:30:00+01:00"],
            "municipality_code": ["GM0001", "GM0002"],
            "ballots_in_batch": [1, 1],
        }
    )
    conformed = S.conform(dst, "timeline")
    assert str(conformed["time"].dt.tz) == "UTC"
    assert conformed["time"].tolist() == [
        pd.Timestamp("2032-10-31T00:30Z"),
        pd.Timestamp("2032-10-31T01:30Z"),
    ]


def test_metadata_conversions() -> None:
    meta = {
        "flag": np.bool_(True),
        "missing": pd.NA,
        "never": pd.NaT,
        "at": np.datetime64("2032-11-03T21:00"),
        "no_time": np.datetime64("NaT"),
        "window": pd.Timedelta(hours=2),
        "counts": pd.Series([1, 2], dtype="int64"),
        "matrix": np.array([[1.5, np.nan]]),
        "codes": pd.Index(["NB", "UT"]),
        DataCategory.SIMULATED: "enum key",
    }
    out = W._jsonable(meta)
    assert out == {
        "SIMULATED": "enum key",
        "at": "2032-11-03T21:00:00",
        "codes": ["NB", "UT"],
        "counts": [1, 2],
        "flag": True,
        "matrix": [1.5, None],
        "missing": None,
        "never": None,
        "no_time": None,
        "window": "P0DT2H0M0S",
    }
    json.dumps(out, allow_nan=False)
    with pytest.raises(TypeError):
        W._jsonable({"blob": b"raw bytes"})


def test_chunked_files_equal_the_in_memory_serialisation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, polls_df: pd.DataFrame
) -> None:
    big = pd.concat(
        [polls_df.assign(poll_id=polls_df["poll_id"] + 10 * i) for i in range(7)], ignore_index=True
    )
    monkeypatch.setattr(W, "CHUNK_ROWS", 4)
    csv_path = W.write_csv(big, "polls", tmp_path / "p.csv")
    assert csv_path.read_text(encoding="utf-8") == W.to_csv_text(big, "polls")
    meta = {"note": "Fictief — ünïcode"}
    json_path = W.write_json(big, "polls", tmp_path / "p.json", meta, generated_at=STAMP)
    expected = W.dumps_envelope(W.to_json_envelope(big, "polls", meta, generated_at=STAMP))
    assert json_path.read_text(encoding="utf-8") == expected
    empty = big.iloc[0:0]
    path = W.write_json(empty, "polls", tmp_path / "e.json", generated_at=STAMP)
    assert path.read_text(encoding="utf-8") == W.dumps_envelope(
        W.to_json_envelope(empty, "polls", generated_at=STAMP)
    )


def test_export_bundle_validates_everything_before_writing(tmp_path: Path, timeline_df, polls_df) -> None:
    out = tmp_path / "bundle"
    with pytest.raises(S.SchemaError):
        W.export_bundle({"polls": polls_df, "timeline": timeline_df.assign(kind="party")}, out)
    assert not out.exists() or not any(out.iterdir())
