"""Deterministic geographic numbering and descriptive names of House districts.

Numbering: within a province, districts are numbered in a *serpentine* order — rows from north to
south, the first row west → east, the next east → west, and so on — so that consecutive numbers
are neighbours on the map.  Codes are ``<PV>-<NN>`` (``NB-07``).

Names (rules in order, see docs/DISTRICTING.md §Names):

* a district (≥ ``single_min_share`` of its population) inside one municipality is named after it —
  with a compass suffix when the municipality is split (``Tilburg-Noord``, ``Amsterdam-Centrum``);
* otherwise, when the largest municipality holds less than ``region_max_main_share``, the most
  specific configured region holding ≥ ``region_min_share`` of the population gives
  ``<Region> – <largest municipality>``;
* otherwise ``<largest> – <second>`` when the second municipality holds ≥ ``second_min_share``;
* otherwise ``<largest> e.o.`` (*en omstreken*, "and surroundings").

Duplicate names within the plan receive Roman numerals (``… I``, ``… II``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from app.districts.config import NamingConfig

COMPASS_8: tuple[str, ...] = (
    "Oost",
    "Noordoost",
    "Noord",
    "Noordwest",
    "West",
    "Zuidwest",
    "Zuid",
    "Zuidoost",
)
COMPASS_4: tuple[str, ...] = ("Oost", "Noord", "West", "Zuid")
CENTRE = "Centrum"


def serpentine_order(xy: np.ndarray) -> np.ndarray:
    """Order of points (rows of ``xy``, metres; y = north) in serpentine north→south rows.

    The number of rows is ``round(sqrt(k × height / width))`` so rows are roughly square cells;
    ties are broken by index so the order is deterministic.
    """
    k = len(xy)
    if k <= 1:
        return np.arange(k)
    span_x = float(np.ptp(xy[:, 0])) or 1.0
    span_y = float(np.ptp(xy[:, 1])) or 1.0
    n_rows = int(np.clip(round(math.sqrt(k * span_y / span_x)), 1, k))
    by_y = np.lexsort((np.arange(k), xy[:, 0], -xy[:, 1]))  # north first
    order: list[int] = []
    for r, row in enumerate(np.array_split(by_y, n_rows)):
        xs = xy[row, 0]
        idx = row[np.lexsort((row, xs))] if r % 2 == 0 else row[np.lexsort((row, -xs))]
        order.extend(idx.tolist())
    return np.asarray(order, dtype=np.int64)


def compass_label(dx: float, dy: float, eight: bool = True) -> str:
    """Dutch compass word for the direction (dx east, dy north)."""
    ang = math.atan2(dy, dx)
    if eight:
        return COMPASS_8[round(ang / (math.pi / 4)) % 8]
    return COMPASS_4[round(ang / (math.pi / 2)) % 4]


def _unique_compass(dx: np.ndarray, dy: np.ndarray, radius: float, config: NamingConfig) -> list[str]:
    """Distinct compass labels for the significant parts of one split municipality.

    Two parts use the four main directions; more parts use eight directions plus "Centrum" (from
    ``centre_min_parts`` parts on).  Labels are assigned jointly (minimum total angular mismatch,
    :func:`scipy.optimize.linear_sum_assignment`), so parts of one municipality never share a
    label while there are enough labels; beyond nine parts Roman numerals disambiguate later.
    """
    from scipy.optimize import linear_sum_assignment

    n = len(dx)
    eight = n > 2
    names = COMPASS_8 if eight else COMPASS_4
    step = math.pi / 4 if eight else math.pi / 2
    ang = np.arctan2(dy, dx)
    cand_ang = np.arange(len(names)) * step
    diff = np.abs((ang[:, None] - cand_ang[None, :] + math.pi) % (2 * math.pi) - math.pi)
    cols = list(names)
    cost = diff
    if n >= config.centre_min_parts:
        r = np.hypot(dx, dy) / max(config.centre_radius_fraction * radius, 1e-9)
        cost = np.column_stack([cost, math.pi * np.clip(r, 0.0, 2.0) - 1e-6])
        cols.append(CENTRE)
    if n > len(cols):
        return [names[int(np.argmin(row))] for row in diff]
    rows, picks = linear_sum_assignment(cost)
    out = [""] * n
    for r_, c_ in zip(rows.tolist(), picks.tolist(), strict=True):
        out[r_] = cols[c_]
    return out


def roman(n: int) -> str:
    """Roman numeral (1 → I)."""
    vals = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
            (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))  # fmt: skip
    out = []
    for v, s in vals:
        while n >= v:
            out.append(s)
            n -= v
    return "".join(out)


def name_districts(
    unit_district: np.ndarray,
    unit_municipality: Sequence[str],
    unit_population: np.ndarray,
    unit_xy: np.ndarray,
    n_districts: int,
    municipality_names: Mapping[str, str],
    config: NamingConfig,
) -> list[str]:
    """Descriptive, unique names for ``n_districts`` districts (index order)."""
    df = pd.DataFrame(
        {
            "d": np.asarray(unit_district, dtype=np.int64),
            "m": np.asarray(unit_municipality, dtype=object),
            "p": np.asarray(unit_population, dtype=float),
            "x": unit_xy[:, 0],
            "y": unit_xy[:, 1],
        }
    )
    df["w"] = df["p"] + 1e-3  # zero-population units still locate parts
    df["wx"], df["wy"] = df["w"] * df["x"], df["w"] * df["y"]
    muni = df.groupby("m", sort=True).agg(w=("w", "sum"), wx=("wx", "sum"), wy=("wy", "sum"))
    muni["cx"], muni["cy"] = muni["wx"] / muni["w"], muni["wy"] / muni["w"]
    df = df.join(muni[["cx", "cy"]], on="m")
    df["r2"] = df["w"] * ((df["x"] - df["cx"]) ** 2 + (df["y"] - df["cy"]) ** 2)
    muni["radius"] = np.sqrt(df.groupby("m", sort=True)["r2"].sum() / muni["w"])
    parts = df.groupby(["d", "m"], sort=True).agg(
        p=("p", "sum"), w=("w", "sum"), wx=("wx", "sum"), wy=("wy", "sum")
    )
    parts["cx"], parts["cy"] = parts["wx"] / parts["w"], parts["wy"] / parts["w"]
    parts = parts.reset_index()

    def display(m: str) -> str:
        return str(municipality_names.get(m, m))

    # compass label of every (district, municipality) part of a split municipality
    part_label: dict[tuple[int, str], str] = {}
    for m, grp in parts.groupby("m", sort=True):
        if len(grp) < 2:
            continue
        mx, my = float(muni.at[m, "cx"]), float(muni.at[m, "cy"])
        radius = float(muni.at[m, "radius"]) or 1.0
        dx = grp["cx"].to_numpy() - mx
        dy = grp["cy"].to_numpy() - my
        share = grp["w"].to_numpy() / max(float(grp["w"].sum()), 1e-12)
        sig = share >= config.minor_part_share
        labels = [compass_label(float(a), float(b), True) for a, b in zip(dx, dy, strict=True)]
        if sig.sum() == 1:
            labels[int(np.flatnonzero(sig)[0])] = ""  # the municipality is essentially whole here
        elif sig.sum() > 1:
            idx = np.flatnonzero(sig)
            for i, lab in zip(idx, _unique_compass(dx[idx], dy[idx], radius, config), strict=True):
                labels[int(i)] = lab
        for d, lab in zip(grp["d"].tolist(), labels, strict=True):
            part_label[(int(d), str(m))] = f"{display(str(m))}-{lab}" if lab else display(str(m))

    def label(d: int, m: str) -> str:
        return part_label.get((d, m), display(m))

    region_of: dict[str, list[str]] = {}
    for region, codes in config.regions.items():
        for c in codes:
            region_of.setdefault(c, []).append(region)

    names: list[str] = []
    by_d = {int(d): g for d, g in parts.groupby("d", sort=True)}
    for d in range(n_districts):
        grp = by_d.get(d)
        if grp is None or grp.empty:
            names.append(f"District {d + 1}")
            continue
        total = float(grp["p"].sum())
        weights = grp["p"].to_numpy() if total > 0 else grp["w"].to_numpy()
        tot = float(weights.sum()) or 1.0
        order = np.lexsort((grp["m"].to_numpy().astype(str), -weights))
        ms = [str(x) for x in grp["m"].to_numpy()[order]]
        shares = weights[order] / tot
        main = ms[0]
        if shares[0] >= config.single_min_share or len(ms) == 1:
            names.append(label(d, main))
            continue
        region_share: dict[str, float] = {}
        for m, s in zip(ms, shares, strict=True):
            for r in region_of.get(m, []):
                region_share[r] = region_share.get(r, 0.0) + float(s)
        regions = sorted(
            (
                (len(config.regions[r]), -s, r)
                for r, s in region_share.items()
                if s >= config.region_min_share and r != display(main)
            ),
        )
        if regions and shares[0] < config.region_max_main_share:
            region = regions[0][2]
            if main in config.regions[region]:
                names.append(f"{region} – {label(d, main)}")
            else:
                names.append(f"{label(d, main)} – {region}")
        elif shares[1] >= config.second_min_share:
            names.append(f"{label(d, main)} – {display(ms[1])}")
        else:
            names.append(f"{label(d, main)}{config.surroundings_suffix}")
    return dedupe_names(names)


def dedupe_names(names: list[str]) -> list[str]:
    """Append Roman numerals to repeated names (in index order)."""
    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if counts[n] > 1:
            seen[n] = seen.get(n, 0) + 1
            out.append(f"{n} {roman(seen[n])}")
        else:
            out.append(n)
    return out
