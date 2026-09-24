"""Municipality lineage across vintages (merger between election cycles)."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import box

from app.geography.lineage import compute_lineage, remap_values
from app.geography.synthetic import SyntheticGeography


def _old_new() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Old vintage: A (2 units), B (2 units), C (2 units), E (2 units).
    New vintage: A + B merged into M with NEW neighbourhood codes; C unchanged; E renamed to F
    (same neighbourhood codes); one unit of C moved to M by a boundary correction."""
    old = pd.DataFrame(
        {
            "code": ["BUA1", "BUA2", "BUB1", "BUB2", "BUC1", "BUC2", "BUE1", "BUE2"],
            "municipality_code": ["GMA", "GMA", "GMB", "GMB", "GMC", "GMC", "GME", "GME"],
            "population": [100, 300, 200, 200, 970, 30, 50, 50],
            "centroid_x": [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 8.5, 9.5],
            "centroid_y": [0.5] * 8,
        }
    )
    new = pd.DataFrame(
        {
            "code": ["BUM1", "BUM2", "BUM3", "BUM4", "BUC1", "BUC2", "BUE1", "BUE2"],
            "municipality_code": ["GMM", "GMM", "GMM", "GMM", "GMC", "GMM", "GMF", "GMF"],
            "population": [110, 290, 210, 190, 980, 30, 55, 45],
            "centroid_x": [0.5, 1.5, 2.5, 3.5, 4.5, 5.5, 8.5, 9.5],
            "centroid_y": [0.5] * 8,
        }
    )
    return old, new


def test_merger_rename_and_boundary_change() -> None:
    old, new = _old_new()
    lin = compute_lineage(old, new)
    assert list(lin.columns[:4]) == ["from_code", "to_code", "population_weight", "event"]
    rows = {(r.from_code, r.to_code): (r.population_weight, r.event) for r in lin.itertuples()}
    assert rows[("GMA", "GMM")] == (1.0, "merger")
    assert rows[("GMB", "GMM")] == (1.0, "merger")
    assert rows[("GME", "GMF")] == (1.0, "rename")
    assert rows[("GMC", "GMC")][0] == pytest.approx(0.97)
    assert rows[("GMC", "GMC")][1] == "unchanged"
    assert rows[("GMC", "GMM")] == (pytest.approx(0.03), "boundary_change")
    # weights of each old municipality sum to one
    assert lin.groupby("from_code")["population_weight"].sum().round(6).eq(1.0).all()


def test_spatial_matching_with_geometry() -> None:
    old, new = _old_new()
    new_gdf = gpd.GeoDataFrame(
        new, geometry=[box(x - 0.5, 0, x + 0.5, 1) for x in new["centroid_x"]], crs=28992
    )
    lin = compute_lineage(old, new_gdf)
    assert set(lin.loc[lin["from_code"] == "GMA", "to_code"]) == {"GMM"}


def test_split() -> None:
    old = pd.DataFrame(
        {"code": ["BU1", "BU2"], "municipality_code": ["GMS", "GMS"], "population": [600, 400]}
    )
    new = pd.DataFrame(
        {"code": ["BU1", "BU2"], "municipality_code": ["GMX", "GMY"], "population": [600, 400]}
    )
    lin = compute_lineage(old, new)
    assert set(lin["event"]) == {"split"}
    assert dict(zip(lin["to_code"], lin["population_weight"], strict=True)) == {"GMX": 0.6, "GMY": 0.4}


def test_remap_values() -> None:
    old, new = _old_new()
    lin = compute_lineage(old, new)
    votes = {"GMA": 400.0, "GMB": 400.0, "GMC": 1000.0, "GME": 100.0}
    summed = remap_values(votes, lin, how="sum")
    assert summed["GMM"] == pytest.approx(400 + 400 + 30)
    assert summed["GMC"] == pytest.approx(970)
    assert summed["GMF"] == pytest.approx(100)
    assert summed.sum() == pytest.approx(sum(votes.values()))
    shares = remap_values({"GMA": 0.2, "GMB": 0.6, "GMC": 0.5}, lin, how="mean")
    # population-weighted: A (400 people, 0.2), B (400, 0.6), C→M (30, 0.5)
    assert shares["GMM"] == pytest.approx((400 * 0.2 + 400 * 0.6 + 30 * 0.5) / 830)
    with pytest.raises(ValueError):
        remap_values(votes, lin, how="median")


def test_identity_on_same_vintage(synthetic: SyntheticGeography) -> None:
    units = pd.DataFrame(synthetic.units.drop(columns="geometry"))
    lin = compute_lineage(units, units)
    assert (lin["event"] == "unchanged").all()
    assert (lin["from_code"] == lin["to_code"]).all()
    assert len(lin) == units["municipality_code"].nunique()


# --------------------------------------------------------------------------- review regressions
def test_remap_mean_does_not_mix_population_and_share_weights() -> None:
    lin = pd.DataFrame(
        {
            "from_code": ["GMA", "GMB", "GMC", "GMD"],
            "to_code": ["GMM", "GMM", "GMN", "GMN"],
            "population_weight": [1.0, 1.0, 0.5, 1.0],
            "event": ["merger", "merger", "merger", "merger"],
            "population": [1000, 0, 0, 0],  # GMB is unpopulated; GMN received nobody at all
            "units": [3, 1, 1, 1],
        }
    )
    out = remap_values({"GMA": 0.2, "GMB": 0.9, "GMC": 0.4, "GMD": 0.1}, lin, how="mean")
    assert out["GMM"] == pytest.approx(0.2)  # the empty municipality carries no weight
    assert out["GMN"] == pytest.approx((0.5 * 0.4 + 1.0 * 0.1) / 1.5)  # share weights as fallback


def test_unlocatable_units_and_municipality_code_fallback() -> None:
    old = pd.DataFrame(
        {
            "code": ["BU1", "BU2", "BU3", "BU4"],
            "municipality_code": ["GMA", "GMA", "GMB", "GMB"],
            "population": [100, 100, 50, 150],
        }
    )
    # BU2 and BU4 vanish (no coordinates to locate them); GMA still exists, GMB does not
    new = pd.DataFrame({"code": ["BU1", "BU3"], "municipality_code": ["GMA", "GMC"], "population": [100, 50]})
    lin = compute_lineage(old, new)
    rows = {(r.from_code, r.to_code): r.population_weight for r in lin.itertuples()}
    assert rows[("GMA", "GMA")] == 1.0  # BU2 stays in its surviving municipality
    assert rows[("GMB", "GMC")] == pytest.approx(0.25)  # BU4's population is lost, not reassigned
    assert set(lin["event"]) <= {"unchanged", "rename", "merger", "split", "boundary_change"}
