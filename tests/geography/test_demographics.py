"""DERIVED quantities and imputation helpers."""

from __future__ import annotations

import numpy as np
import pytest

from app.geography.cbs import (
    MISSING_THRESHOLD,
    clean_missing,
    parse_supplement,
    urbanity_class_from_address_density,
)
from app.geography.demographics import (
    distribute_missing_population,
    estimate_eligible_voters,
    fill_from_parents,
    group_weighted_mean,
    hierarchical_weighted_fill,
    join_imputed_flags,
    largest_remainder_by_group,
    resolve_urbanity_class,
)


def test_clean_missing_codes() -> None:
    raw = np.array([12, -99997, -99999999, 0, -99990, -5, np.nan])
    out = clean_missing(raw)
    assert np.isnan(out[[1, 2, 4, 6]]).all()
    assert out[0] == 12 and out[3] == 0 and out[5] == -5
    assert MISSING_THRESHOLD == -99990


def test_eligible_voter_formula() -> None:
    pop = np.array([1000, 1000, 0, 10, 1000])
    p0 = np.array([15.0, 0.0, 20.0, 0.0, 100.0])
    p15 = np.array([10.0, 0.0, 10.0, 0.0, 0.0])
    est = estimate_eligible_voters(pop, p0, p15, citizenship_factor=0.93, adult_share_15_25=0.7)
    # 1000 × ((100 − 15 − 10)/100 + 0.7 × 10/100) × 0.93 = 1000 × 0.82 × 0.93 = 762.6 → 763
    assert est[0] == 763
    assert est[1] == 930  # everyone adult
    assert est[2] == 0 and est[4] == 0
    assert est.dtype == np.int64 and (est <= pop).all() and (est >= 0).all()
    with pytest.raises(ValueError):
        estimate_eligible_voters(pop, np.full(5, np.nan), p15)


def test_distribute_missing_population_exact_and_flagged() -> None:
    pop = np.array([100.0, np.nan, np.nan, 50.0, np.nan, 30.0])
    muni = np.array([0, 0, 0, 1, 1, 2])
    official = np.array([401.0, 40.0, 30.0])  # residual 301 for muni 0, none for muni 1 (50 > 40)
    area = np.array([1.0, 1.0, 2.0, 1.0, 1.0, 1.0])
    out, imputed = distribute_missing_population(pop, muni, official, area)
    assert imputed.tolist() == [False, True, True, False, True, False]
    assert out.dtype == np.int64
    assert out[1] + out[2] == 301 and out[2] in (200, 201)  # proportional to land area, integral
    assert out[4] == 0 and out[0] == 100 and out[5] == 30
    # address density × area weighting when every missing unit has an address density
    ad = np.array([np.nan, 3000.0, 500.0, np.nan, np.nan, np.nan])
    out2, _ = distribute_missing_population(pop, muni, official, area, ad)
    assert out2[1] > out2[2] and out2[1] + out2[2] == 301


def test_largest_remainder_by_group() -> None:
    alloc = largest_remainder_by_group(
        np.array([10.0, 3.0]), np.array([1 / 3, 1 / 3, 1 / 3, 0.5, 0.5]), np.array([0, 0, 0, 1, 1])
    )
    assert alloc[:3].sum() == 10 and alloc[3:].sum() == 3
    assert sorted(alloc[:3].tolist()) == [3, 3, 4]


def test_hierarchical_fill_levels_and_flags() -> None:
    values = np.array([10.0, np.nan, 30.0, np.nan, np.nan, 50.0])
    weights = np.array([1.0, 1.0, 3.0, 1.0, 1.0, 1.0])
    muni = np.array([0, 0, 0, 1, 2, 2])
    prov = np.array([0, 0, 0, 0, 1, 1])
    out, imputed = hierarchical_weighted_fill(values, weights, [muni, prov])
    assert out[1] == pytest.approx((10 + 90) / 4)  # municipality weighted mean
    assert out[3] == pytest.approx((10 + 90) / 4)  # province mean (muni 1 has no data)
    assert out[4] == pytest.approx(50.0)
    assert imputed.tolist() == [False, True, False, True, True, False]
    # national fallback
    out2, imp2 = hierarchical_weighted_fill(np.array([np.nan, 4.0]), np.array([1.0, 1.0]), [np.array([0, 1])])
    assert out2[0] == 4.0 and imp2.tolist() == [True, False]


def test_group_weighted_mean_zero_weights() -> None:
    m = group_weighted_mean(np.array([2.0, 4.0, np.nan]), np.array([0.0, 0.0, 5.0]), np.array([0, 0, 1]), 2)
    assert m[0] == 3.0 and np.isnan(m[1])


def test_fill_from_parents_order() -> None:
    own = np.array([1.0, np.nan, np.nan, np.nan])
    wijk = np.array([9.0, 2.0, np.nan, np.nan])
    gem = np.array([9.0, 9.0, 3.0, np.nan])
    out, imputed = fill_from_parents(own, wijk, gem)
    assert out[:3].tolist() == [1.0, 2.0, 3.0] and np.isnan(out[3])
    assert imputed.tolist() == [False, True, True, False]


def test_urbanity_class_thresholds() -> None:
    ad = np.array([2600, 2500, 2499, 1500, 1200, 1000, 700, 500, 499, 0, np.nan])
    cls = urbanity_class_from_address_density(ad)
    assert cls[:-1].tolist() == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    assert np.isnan(cls[-1])
    resolved, imputed = resolve_urbanity_class(
        np.array([2.0, np.nan, np.nan, -99997.0]),
        np.array([np.nan, 1800.0, np.nan, np.nan]),
        np.array([4.0, 4.0, 4.0, 5.0]),
    )
    assert resolved.tolist() == [2.0, 2.0, 4.0, 5.0]
    assert imputed.tolist() == [False, False, True, True]


def test_join_flags() -> None:
    flags = {"a": np.array([True, False, True]), "b": np.array([True, False, False])}
    out = join_imputed_flags(flags, 3, ["b", "a"])
    assert out.tolist() == ["b,a", "", "a"]


def test_parse_supplement_education_shares() -> None:
    rows = [
        {
            "Codering_3": "BU00140000  ",
            "SoortRegio_2": "Buurt     ",
            "AantalInwoners_5": 100,
            "Koopwoningen_40": 60,
            "OpleidingsniveauLaag_64": 20,
            "OpleidingsniveauMiddelbaar_65": 30,
            "OpleidingsniveauHoog_66": 50,
            "GemiddeldInkomenPerInwoner_72": 31.5,
        },
        {
            "Codering_3": "WK001400  ",
            "SoortRegio_2": "Wijk      ",
            "AantalInwoners_5": 5,
            "Koopwoningen_40": None,
            "OpleidingsniveauLaag_64": None,
            "OpleidingsniveauMiddelbaar_65": 10,
            "OpleidingsniveauHoog_66": 5,
            "GemiddeldInkomenPerInwoner_72": -99997,
        },
    ]
    df = parse_supplement(rows).set_index("code")
    assert df.loc["BU00140000", "pct_education_high"] == pytest.approx(50.0)
    assert df.loc["BU00140000", "pct_education_low"] == pytest.approx(20.0)
    assert df.loc["BU00140000", "level"] == "BU" and df.loc["WK001400", "level"] == "WK"
    assert np.isnan(df.loc["WK001400", "pct_education_high"])  # incomplete counts → missing
    assert np.isnan(df.loc["WK001400", "income_per_capita_keur"])


# --------------------------------------------------------------------------- review regressions
def test_fill_composition_keeps_known_parts_and_sums_to_total() -> None:
    from app.geography.demographics import fill_composition

    nan = np.nan
    own = np.array(
        [
            [0.0, 69.0, 28.0, nan, nan],  # partial: remainder 3 split by the wijk ratio 2:1
            [nan, nan, nan, nan, nan],  # all missing: wijk composition (rescaled to 100)
            [10.0, 10.0, 30.0, 30.0, 20.0],  # complete: untouched
            [60.0, 50.0, nan, nan, nan],  # known parts already exceed 100: missing parts get 0
        ]
    )
    wijk = np.array(
        [
            [10.0, 10.0, 40.0, 26.0, 13.0],
            [20.0, 10.0, 30.0, 20.0, 19.0],  # sums to 99 (CBS rounding) → rescaled
            [nan] * 5,
            [20.0, 20.0, 20.0, 20.0, 20.0],
        ]
    )
    gem = np.full((4, 5), 20.0)
    out, imputed = fill_composition(own, [wijk, gem], np.ones(4), [np.zeros(4, dtype=np.int64)])
    assert out[0, :3].tolist() == [0.0, 69.0, 28.0]
    assert out[0, 3] == pytest.approx(2.0) and out[0, 4] == pytest.approx(1.0)
    assert out[1] == pytest.approx(wijk[1] * 100 / 99)
    assert out[2].tolist() == own[2].tolist()
    assert out[3, 2:].tolist() == [0.0, 0.0, 0.0]
    assert np.allclose(out[:3].sum(axis=1), 100.0)
    assert imputed.tolist() == np.isnan(own).tolist()  # exactly the filled cells are flagged


def test_fill_composition_reference_chain() -> None:
    from app.geography.demographics import fill_composition

    nan = np.nan
    own = np.array(
        [
            [50.0, 30.0, 20.0],  # complete rows of group 0 → group mean (weighted 3:1)
            [30.0, 30.0, 40.0],
            [80.0, nan, nan],  # group 0; incomplete parent → group mean ratio
            [nan, nan, nan],  # group 1 without complete rows → national mean
        ]
    )
    parent = np.array([[nan] * 3, [nan] * 3, [10.0, nan, 5.0], [nan] * 3])  # never complete
    weights = np.array([3.0, 1.0, 1.0, 1.0])
    groups = np.array([0, 0, 0, 1])
    out, imputed = fill_composition(own, [parent], weights, [groups])
    mean = (3 * own[0] + own[1]) / 4  # [45, 30, 25]
    assert out[2, 1] / out[2, 2] == pytest.approx(mean[1] / mean[2])
    assert out[2].sum() == pytest.approx(100.0) and out[2, 0] == 80.0
    assert out[3] == pytest.approx(mean)  # national mean of the complete rows
    assert imputed[:2].sum() == 0 and imputed[2].tolist() == [False, True, True]
    # no reference at all → equal split of the remainder
    lone, _ = fill_composition(np.array([[40.0, nan, nan]]), [], np.ones(1), [])
    assert lone[0].tolist() == [40.0, 30.0, 30.0]
    # nothing to do / empty input
    same, none = fill_composition(own[:2], [], weights[:2], [groups[:2]])
    assert np.array_equal(same, own[:2]) and not none.any()
    empty, _ = fill_composition(np.zeros((0, 3)), [], np.zeros(0), [np.zeros(0, dtype=np.int64)])
    assert empty.shape == (0, 3)


def test_distribute_missing_population_without_official_total() -> None:
    pop = np.array([np.nan, 10.0, np.nan])
    # municipality 0 has no official total (→ 0); municipality 1 has no known units (→ residual 5)
    out, imputed = distribute_missing_population(
        pop, np.array([0, 0, 1]), np.array([np.nan, 5.0]), np.ones(3)
    )
    assert out.tolist() == [0, 10, 5] and imputed.tolist() == [True, False, True]
    # zero land area everywhere → equal split, still exact
    out2, _ = distribute_missing_population(
        np.array([np.nan, np.nan, np.nan]), np.zeros(3, dtype=np.int64), np.array([10.0]), np.zeros(3)
    )
    assert out2.sum() == 10 and sorted(out2.tolist()) == [3, 3, 4]


def test_eligible_voters_inconsistent_shares_are_clipped() -> None:
    # CBS rounding can make 0–15 + 15–25 exceed 100: the estimate is clipped at 0, never negative
    # (100 − 95 − 20)/100 + 0.7 × 20/100 = −0.01 → 0
    est = estimate_eligible_voters(
        np.array([100, 100]), np.array([95.0, 0.0]), np.array([20.0, 0.0]), 1.0, 0.7
    )
    assert est.tolist() == [0, 100]
