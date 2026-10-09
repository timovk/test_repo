"""Water boards (REAL areas): GML parsing, unit assignment, the store build, the DB loader and the
synthetic water boards — all offline."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import pytest
import shapely
from shapely.geometry import Point, Polygon, box
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.geography import store
from app.geography.build import build_geography
from app.geography.loader_db import load_into_db, load_tables_into_db, sync_water_boards
from app.geography.synthetic import SyntheticGeography, synthetic_geography
from app.geography.water_boards import (
    BOARD_COLUMNS,
    NEAREST,
    NONE,
    WITHIN,
    assign_units,
    assignment_counts,
    boundary_overlap,
    read_water_boards_gml,
    representative_points,
    summarize_water_boards,
    water_board_code,
)
from app.models import DataSource, GeoUnit, GeoVintage, Province, WaterBoard

#: Published ETRS89 position of the RD (EPSG:28992) origin, the Amersfoort tower: x 155 000, y 463 000.
AMERSFOORT_LAT, AMERSFOORT_LON = "52.15517440", "5.38720621"

# Two water boards in the exact layout of the Het Waterschapshuis file (EPSG:4258, lat lon h):
#   WS07 — the RD square x 150–155 km, y 458–463 km with a 1 km² hole (x 151.5–152.5, y 460.5–461.5 km);
#   WS33 — the square x 155–160 km (sharing WS07's east edge) plus a detached 1 km² island at x 161 km.
# Both share the corner at the RD origin, written with its published ETRS89 coordinates.
GML = f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2"
    xmlns:base="http://inspire.ec.europa.eu/schemas/base/3.3" xmlns:au="http://inspire.ec.europa.eu/schemas/au/3.0"
    numberMatched="unknown" numberReturned="0">
  <wfs:member>
    <au:AdministrativeUnit gml:id="NL.WBHCODE.33.Admingrenswaterschap.1">
      <au:geometry><gml:MultiSurface gml:id="ms33" srsName="urn:ogc:def:crs:EPSG::4258">
        <gml:surfaceMember><gml:Polygon gml:id="p33a"><gml:exterior><gml:LinearRing>
          <gml:posList srsDimension="3">52.110232487 5.387203340 0.0 52.110209788 5.460192724 0.0
            52.155149565 5.460266377 0.0 {AMERSFOORT_LAT} {AMERSFOORT_LON} 0.0 52.110232487 5.387203340 0.0</gml:posList>
        </gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
        <gml:surfaceMember><gml:Polygon gml:id="p33b"><gml:exterior><gml:LinearRing>
          <gml:posList srsDimension="3">52.110199824 5.474790585 0.0 52.110188053 5.489388439 0.0
            52.119176030 5.489409030 0.0 52.119187804 5.474808239 0.0 52.110199824 5.474790585 0.0</gml:posList>
        </gml:LinearRing></gml:exterior></gml:Polygon></gml:surfaceMember>
      </gml:MultiSurface></au:geometry>
      <au:nationalCode>33</au:nationalCode>
      <au:inspireId><base:Identifier><base:localId>Admingrenswaterschap.1</base:localId>
        <base:namespace>NL.WBHCODE.33</base:namespace></base:Identifier></au:inspireId>
      <au:nationalLevel>4thOrder</au:nationalLevel>
      <au:nationalLevelName>Waterschap</au:nationalLevelName>
      <au:country>NL</au:country>
      <gml:name>Waterschap Proef  Oost</gml:name>
    </au:AdministrativeUnit>
  </wfs:member>
  <wfs:member>
    <au:AdministrativeUnit gml:id="NL.WBHCODE.7.Admingrenswaterschap.2">
      <au:geometry><gml:MultiSurface gml:id="ms7" srsName="urn:ogc:def:crs:EPSG::4258">
        <gml:surfaceMember><gml:Polygon gml:id="p7">
          <gml:exterior><gml:LinearRing>
            <gml:posList srsDimension="3">52.110209990 5.314213955 0.0 52.110232487 5.387203340 0.0
              {AMERSFOORT_LAT} {AMERSFOORT_LON} 0.0 52.155149768 5.314140631 0.0 52.110209990 5.314213955 0.0</gml:posList>
          </gml:LinearRing></gml:exterior>
          <gml:interior><gml:LinearRing>
            <gml:posList srsDimension="3">52.132691423 5.336085137 0.0 52.141679375 5.336074879 0.0
              52.141684783 5.350683040 0.0 52.132696830 5.350690358 0.0 52.132691423 5.336085137 0.0</gml:posList>
          </gml:LinearRing></gml:interior>
        </gml:Polygon></gml:surfaceMember>
      </gml:MultiSurface></au:geometry>
      <au:nationalCode>7</au:nationalCode>
      <au:country>NL</au:country>
      <gml:name>Hoogheemraadschap Proef West</gml:name>
    </au:AdministrativeUnit>
  </wfs:member>
</wfs:FeatureCollection>
"""


@pytest.fixture()
def boards(tmp_path: Path) -> gpd.GeoDataFrame:
    path = tmp_path / "boards.gml"
    path.write_text(GML, encoding="utf-8")
    return read_water_boards_gml(path)


def _board(boards: gpd.GeoDataFrame, code: str) -> Any:
    return boards.set_index("code").geometry[code]


# --------------------------------------------------------------------------- parsing
def test_water_board_code() -> None:
    assert water_board_code(33) == "WS33"
    assert water_board_code("7") == "WS07"
    assert water_board_code(" 059 ") == "WS59"


def test_parse_gml_codes_names_and_crs(boards: gpd.GeoDataFrame) -> None:
    assert list(boards["code"]) == ["WS07", "WS33"]  # sorted by code, not file order
    assert list(boards["national_code"]) == [7, 33]
    assert list(boards["name"]) == ["Hoogheemraadschap Proef West", "Waterschap Proef Oost"]
    assert boards.crs.to_epsg() == 28992
    assert shapely.is_valid(np.asarray(boards.geometry.values, dtype=object)).all()


def test_parse_gml_axis_order_and_transform(boards: gpd.GeoDataFrame) -> None:
    """lat/lon/h posLists become RD x/y: the square lands on its RD coordinates (not swapped)."""
    west = _board(boards, "WS07")
    x0, y0, x1, y1 = west.bounds
    assert x0 == pytest.approx(150_000, abs=0.05) and y0 == pytest.approx(458_000, abs=0.05)
    # the corner written with the published ETRS89 position of the RD origin
    assert x1 == pytest.approx(155_000, abs=1.0) and y1 == pytest.approx(463_000, abs=1.0)
    assert west.contains(Point(152_000, 459_000))
    corner = shapely.get_coordinates(west.exterior if west.geom_type == "Polygon" else west.geoms[0].exterior)
    assert (
        np.isclose(corner[:, 0], 155_000, atol=1.0).any()
        and np.isclose(corner[:, 1], 463_000, atol=1.0).any()
    )


def test_parse_gml_holes_and_multiple_surfaces(boards: gpd.GeoDataFrame) -> None:
    west, east = _board(boards, "WS07"), _board(boards, "WS33")
    assert west.area / 1e6 == pytest.approx(25.0 - 1.0, abs=0.01)  # exterior minus the hole
    assert not west.contains(Point(152_000, 461_000))  # inside the hole
    assert shapely.get_num_interior_rings(shapely.get_parts(west)).sum() == 1
    assert len(shapely.get_parts(east)) == 2  # main area + detached island
    assert east.area / 1e6 == pytest.approx(25.0 + 1.0, abs=0.01)
    assert east.contains(Point(161_500, 458_500))


def test_parse_gml_without_features_raises(tmp_path: Path) -> None:
    path = tmp_path / "empty.gml"
    path.write_text('<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"/>', encoding="utf-8")
    with pytest.raises(ValidationError, match="no au:AdministrativeUnit"):
        read_water_boards_gml(path)


# --------------------------------------------------------------------------- assignment
def test_assign_within_nearest_none(boards: gpd.GeoDataFrame) -> None:
    units = gpd.GeoDataFrame(
        {"code": ["a", "b", "hole", "far", "off"]},
        geometry=[
            box(151_000, 459_000, 151_200, 459_200),  # in WS07
            box(157_000, 460_000, 157_300, 460_300),  # in WS33
            box(151_900, 460_900, 152_100, 461_100),  # centre of WS07's hole → nearest, 500 m
            box(170_000, 470_000, 170_100, 470_100),  # ~ 12 km away → none
            box(150_000, 455_000, 150_400, 455_400),  # 2.8 km south of WS07
        ],
        crs=28992,
        index=[10, 11, 12, 13, 14],
    )
    out = assign_units(units, boards, max_distance_m=2000.0)
    assert list(out.index) == [10, 11, 12, 13, 14]
    assert out["water_board_code"].tolist() == ["WS07", "WS33", "WS07", None, None]
    assert out["water_board_method"].tolist() == [WITHIN, WITHIN, NEAREST, NONE, NONE]
    assert out.loc[12, "water_board_distance_m"] == pytest.approx(500.0, abs=0.5)
    assert out.loc[10, "water_board_distance_m"] == 0.0 and np.isnan(out.loc[13, "water_board_distance_m"])
    assert assignment_counts(out) == {"within": 2, "nearest": 1, "none": 2}
    wider = assign_units(units, boards, max_distance_m=3000.0)
    assert wider.loc[14, "water_board_code"] == "WS07" and wider.loc[14, "water_board_method"] == NEAREST
    assert wider.loc[13, "water_board_method"] == NONE
    unlimited = assign_units(units, boards, max_distance_m=None)
    assert unlimited.loc[13, "water_board_code"] == "WS33"  # nearest at any distance
    no_fallback = assign_units(units, boards, max_distance_m=0)
    assert no_fallback["water_board_method"].tolist() == [WITHIN, WITHIN, NONE, NONE, NONE]


def test_assign_points_and_coordinates(boards: gpd.GeoDataFrame) -> None:
    xy = np.array([[151_000.0, 459_000.0], [161_500.0, 458_500.0], [152_000.0, 461_000.0]])
    out = assign_units(xy, boards)
    assert out["water_board_code"].tolist() == ["WS07", "WS33", "WS07"]
    assert out["water_board_method"].tolist() == [WITHIN, WITHIN, NEAREST]
    # inputs in another CRS are reprojected to the boards' CRS (distances stay in metres)
    wgs = gpd.GeoSeries([Point(151_000, 459_000), Point(152_000, 461_000)], crs=28992).to_crs(4326)
    out = assign_units(wgs, boards)
    assert out["water_board_code"].tolist() == ["WS07", "WS07"]
    assert out["water_board_distance_m"].iloc[1] == pytest.approx(500.0, abs=0.5)


def test_assign_representative_point_of_concave_unit(boards: gpd.GeoDataFrame) -> None:
    # a U-shaped unit whose centroid lies in its (empty) middle — inside WS33, the arms reach WS07
    u_shape = Polygon(
        [(154_000, 459_000), (158_000, 459_000), (158_000, 462_000), (157_500, 462_000),
         (157_500, 459_500), (154_500, 459_500), (154_500, 462_000), (154_000, 462_000)]
    )  # fmt: skip
    point = representative_points(np.array([u_shape], dtype=object))[0]
    assert u_shape.contains(point)
    assert not u_shape.contains(u_shape.centroid)


def test_overlapping_boards_prefer_largest_share() -> None:
    boards = gpd.GeoDataFrame(
        {"code": ["WS01", "WS02"]},
        geometry=[box(0, 0, 1000, 1000), box(900, 0, 2000, 1000)],  # 100 m overlap strip
        crs=28992,
    )
    unit = box(890, 400, 1100, 600)  # centroid at x 995 lies in both; most of its area in WS02
    out = assign_units(gpd.GeoSeries([unit], crs=28992), boards)
    assert out["water_board_code"].tolist() == ["WS02"]
    assert assign_units([Point(950, 500)], boards)["water_board_code"].tolist() == ["WS01"]


def test_boundary_overlap(boards: gpd.GeoDataFrame) -> None:
    units = [
        box(154_500, 459_000, 156_000, 460_000),  # straddles the border; centroid x 155 250 → WS33
        box(151_000, 459_000, 151_200, 459_200),
        box(170_000, 470_000, 170_100, 470_100),
    ]
    codes = assign_units(units, boards)["water_board_code"].to_numpy(dtype=object)
    assert codes.tolist() == ["WS33", "WS07", None]
    share = boundary_overlap(units, boards, codes)
    assert share[0] == pytest.approx(1 / 3, abs=1e-3) and share[1] == 0.0 and np.isnan(share[2])


def test_summarize_water_boards(boards: gpd.GeoDataFrame) -> None:
    units = pd.DataFrame(
        {
            "water_board_code": ["WS07", "WS07", "WS33", "WS33", None],
            "province_code": ["UT", "GE", "GE", "GE", "UT"],
            "municipality_code": ["GM1", "GM2", "GM2", "GM3", "GM4"],
            "population": [100, 300, 50, 60, 1000],
            "eligible_voters_est": [90, 200, 40, 50, 800],
        }
    )
    table = summarize_water_boards(boards, units, ["GE", "UT"])
    assert list(table.columns) == [*BOARD_COLUMNS, "geometry"]
    row = table.set_index("code")
    assert row.loc["WS07", "province_code"] == "GE" and row.loc["WS07", "provinces"] == "GE,UT"
    assert row.loc["WS07", "population"] == 400 and row.loc["WS07", "eligible_voters_est"] == 290
    assert row.loc["WS07", "unit_count"] == 2 and row.loc["WS07", "municipality_count"] == 2
    assert row.loc["WS33", "provinces"] == "GE" and row.loc["WS33", "municipality_count"] == 2
    assert row.loc["WS07", "area_km2"] == pytest.approx(24.0, abs=0.01)
    empty = summarize_water_boards(boards, units.iloc[:2], ["GE", "UT"]).set_index("code")
    assert empty.loc["WS33", "province_code"] is None and empty.loc["WS33", "unit_count"] == 0


# --------------------------------------------------------------------------- synthetic
def test_synthetic_water_boards(synthetic: SyntheticGeography) -> None:
    boards = synthetic.water_boards
    assert boards is not None and list(boards["code"]) == ["WS01", "WS02", "WS03", "WS04"]
    assert list(boards["name"]) == [f"Synthetic Water Board {i}" for i in range(1, 5)]
    units = synthetic.units
    assert units["water_board_code"].notna().all()
    assert int(boards["population"].sum()) == int(units["population"].sum())
    # boards cross province borders and provinces are split between boards
    provinces_per_board = units.groupby("water_board_code")["province_code"].nunique()
    boards_per_province = units.groupby("province_code")["water_board_code"].nunique()
    assert (provinces_per_board > 1).all() and (boards_per_province > 1).any()
    # municipalities are never split; the election province holds most of the board's voters
    assert (units.groupby("municipality_code")["water_board_code"].nunique() == 1).all()
    for row in boards.itertuples():
        elig = (
            units[units["water_board_code"] == row.code].groupby("province_code")["eligible_voters_est"].sum()
        )
        assert row.province_code == elig.idxmax()
    frame = synthetic.frame
    assert frame.n_water_boards == 4 and frame.validate() == []
    assert frame.unit_water_board is not None and (frame.unit_water_board >= 0).all()
    assert np.array_equal(frame.to_water_boards(frame.unit_population), boards["population"].to_numpy())
    assert [frame.province_codes[p] for p in frame.water_board_province] == list(boards["province_code"])
    w = frame.water_board_index("WS02")
    assert set(frame.units_in_water_board(w)) == {
        frame.unit_index(c) for c in units.loc[units["water_board_code"] == "WS02", "code"]
    }
    again = synthetic_geography(seed=7)  # deterministic
    assert again.units["water_board_code"].tolist() == units["water_board_code"].tolist()


# --------------------------------------------------------------------------- database loader
def _board_rows(session: Session) -> dict[str, tuple[int, str, int]]:
    rows = session.execute(
        select(WaterBoard.code, WaterBoard.id, Province.code, WaterBoard.population).join(
            Province, Province.id == WaterBoard.province_id
        )
    ).all()
    return {code: (wid, prov, pop) for code, wid, prov, pop in rows}


def _unit_boards(session: Session) -> dict[str, str | None]:
    rows = session.execute(
        select(GeoUnit.cbs_code, WaterBoard.code).outerjoin(
            WaterBoard, WaterBoard.id == GeoUnit.water_board_id
        )
    ).all()
    return dict(rows)


def test_loader_inserts_water_boards_idempotently(db_session: Session, synthetic: SyntheticGeography) -> None:
    boards = synthetic.water_boards
    assert boards is not None
    load_tables_into_db(
        db_session, 2025, synthetic.provinces, synthetic.municipalities, synthetic.units, water_boards=boards
    )
    rows = _board_rows(db_session)
    assert set(rows) == {"WS01", "WS02", "WS03", "WS04"}
    assert {c: r[1] for c, r in rows.items()} == dict(
        zip(boards["code"], boards["province_code"], strict=True)
    )
    assert {c: r[2] for c, r in rows.items()} == dict(zip(boards["code"], boards["population"], strict=True))
    expected = dict(zip(synthetic.units["code"], synthetic.units["water_board_code"], strict=True))
    assert _unit_boards(db_session) == expected
    vintage = db_session.scalar(select(GeoVintage))
    assert all(wb.vintage_id == vintage.id for wb in db_session.scalars(select(WaterBoard)))

    # unchanged reload: same ids, nothing duplicated
    load_tables_into_db(
        db_session, 2025, synthetic.provinces, synthetic.municipalities, synthetic.units, water_boards=boards
    )
    assert _board_rows(db_session) == rows and _unit_boards(db_session) == expected

    # one unit moves to another board; a board disappears (its units are detached first)
    units = synthetic.units.copy()
    first = units["code"].iloc[0]
    units.loc[units["code"] == first, "water_board_code"] = "WS04"
    units.loc[units["water_board_code"] == "WS03", "water_board_code"] = None
    fewer = boards[boards["code"] != "WS03"]
    sync_water_boards(db_session, vintage, fewer, units)
    after = _board_rows(db_session)
    assert set(after) == {"WS01", "WS02", "WS04"} and after["WS01"][0] == rows["WS01"][0]
    moved = _unit_boards(db_session)
    assert moved[first] == "WS04"
    assert sum(v is None for v in moved.values()) == int(
        (synthetic.units["water_board_code"] == "WS03").sum()
    )


def test_loader_adds_water_boards_to_a_loaded_vintage(
    db_session: Session, synthetic: SyntheticGeography
) -> None:
    """A vintage loaded before water boards existed gets them on the next (otherwise skipped) load."""
    load_tables_into_db(db_session, 2025, synthetic.provinces, synthetic.municipalities, synthetic.units)
    assert db_session.scalar(select(func.count()).select_from(WaterBoard)) == 0
    assert db_session.scalar(select(func.count()).where(GeoUnit.water_board_id.is_not(None))) == 0
    load_tables_into_db(
        db_session,
        2025,
        synthetic.provinces,
        synthetic.municipalities,
        synthetic.units,
        water_boards=synthetic.water_boards,
    )
    assert db_session.scalar(select(func.count()).select_from(WaterBoard)) == 4
    assert db_session.scalar(select(func.count()).where(GeoUnit.water_board_id.is_(None))) == 0


def test_loader_rejects_unknown_board_codes(db_session: Session, synthetic: SyntheticGeography) -> None:
    units = synthetic.units.copy()
    units.loc[units.index[0], "water_board_code"] = "WS99"
    with pytest.raises(ValueError, match="WS99"):
        load_tables_into_db(
            db_session,
            2025,
            synthetic.provinces,
            synthetic.municipalities,
            units,
            water_boards=synthetic.water_boards,
        )


# --------------------------------------------------------------------------- store build (CBS formats)
@pytest.fixture()
def built(cbs_raw: dict[str, Any]) -> dict[str, Any]:
    return {"facts": cbs_raw, "report": build_geography(cbs_raw["year"])}


def test_build_assigns_every_unit(built: dict[str, Any]) -> None:
    facts, report = built["facts"], built["report"]
    counts = report.counts
    assert counts["water_boards"] == facts["water_boards"] == 4
    assert counts["units_water_board_nearest"] == 1  # the unit inside the 300 m hole
    assert counts["units_water_board_none"] == 1  # the island, 4 km offshore
    assert counts["units_water_board_within"] == facts["n_units"] - 2
    assert any("outside every water board" in w for w in report.warnings)

    units = store.load_units_attrs(2025)
    by_code = units.set_index("code")["water_board_code"]
    assert by_code[facts["island_code"]] is None
    assert by_code[facts["hole_unit"]] == facts["hole_board"]
    expected = facts["unit_water_board"]
    assert all(by_code[c] == b for c, b in expected.items())  # GML boards == the synthetic ones

    manifest = store.manifest(2025)
    wb = manifest["water_boards"]
    assert wb["counts"] == {"within": facts["n_units"] - 2, "nearest": 1, "none": 1}
    assert wb["unassigned"] == [facts["island_code"]]
    assert [n["code"] for n in wb["nearest"]] == [facts["hole_unit"]]
    assert wb["nearest"][0]["distance_m"] == pytest.approx(300.0, abs=1.0)
    assert [b["code"] for b in wb["boards"]] == ["WS01", "WS02", "WS03", "WS04"]
    assert "water_boards" in manifest["sources"]

    boards = store.load_water_boards(2025)
    assert boards is not None and list(boards.columns) == list(BOARD_COLUMNS)
    assert int(boards["population"].sum()) == int(
        units.loc[units["water_board_code"].notna(), "population"].sum()
    )
    assert boards["province_code"].notna().all() and (boards["unit_count"] > 0).all()
    polygons = store.load_water_boards(2025, geometry=True)
    assert polygons.crs.to_epsg() == 28992 and shapely.is_valid(np.asarray(polygons.geometry.values)).all()

    frame = store.load_frame(2025)
    assert frame.validate() == [] and frame.n_water_boards == 4
    assert frame.unit_water_board is not None
    assert frame.unit_water_board[frame.unit_index(facts["island_code"])] == -1
    assert (np.delete(frame.unit_water_board, frame.unit_index(facts["island_code"])) >= 0).all()


def test_build_water_board_web_layer(built: dict[str, Any]) -> None:
    web = pyogrio.read_dataframe(store.web_geojson_path(2025, "water_boards"))
    assert web.crs.to_epsg() == 4326 and len(web) == 4
    assert set(web.columns) >= {"code", "name", "province_code", "population"}
    geoms = np.asarray(web.geometry.values, dtype=object)
    assert shapely.is_valid(geoms).all() and not shapely.is_empty(geoms).any()


def test_build_rebuilds_when_the_water_board_file_changes(built: dict[str, Any]) -> None:
    first = built["report"]
    assert build_geography(2025).skipped
    path = built["facts"]["data_dir"] / "raw" / "waterschappen_administratieve_eenheden.gml"
    path.write_text(path.read_text(encoding="utf-8").replace("Synthetic Water Board 1", "Renamed Board 1"))
    again = build_geography(2025)
    assert not again.skipped and again.manifest["fingerprint"] != first.manifest["fingerprint"]
    assert store.load_water_boards(2025).set_index("code").loc["WS01", "name"] == "Renamed Board 1"


def test_load_store_into_db_with_water_boards(built: dict[str, Any], db_session: Session) -> None:
    facts = built["facts"]
    load_into_db(db_session, 2025)
    assert db_session.scalar(select(func.count()).select_from(WaterBoard)) == 4
    source = db_session.scalar(select(DataSource).where(DataSource.key == "water_boards_2025"))
    assert source is not None and source.license == "CC0 1.0" and source.data_category == "REAL"
    assert {wb.source_id for wb in db_session.scalars(select(WaterBoard))} == {source.id}
    boards = _unit_boards(db_session)
    assert boards[facts["island_code"]] is None and boards[facts["hole_unit"]] == facts["hole_board"]
    assert sum(v is not None for v in boards.values()) == facts["n_units"] - 1
    load_into_db(db_session, 2025)  # idempotent
    assert db_session.scalar(select(func.count()).select_from(WaterBoard)) == 4


def test_a_store_without_water_boards_is_rebuilt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A store built before water boards existed stays readable but is not current: ``demo`` and
    ``init --with-geography`` rebuild it, so an upgraded install gets its water boards."""
    for name in store._REQUIRED:
        (tmp_path / name).write_text("{}")
    monkeypatch.setattr(store, "store_dir", lambda year=None: tmp_path)
    assert store.is_prepared() and not store.has_water_boards()
    assert store.is_current() is not store.load_geography_config().has_water_boards
    (tmp_path / store.WATER_BOARDS_FILE).write_text("")
    assert store.is_current()
