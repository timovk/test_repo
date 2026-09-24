# Districting

How the (FICTIONAL) federal system turns the (REAL) population of the Netherlands into House
seats, electoral votes, 150 single-member House districts and 24 staggered Senate seats.

Code: `src/app/districts/` · configuration: `config/districts.yaml`,
`config/districts/overrides.yaml`, `config/senate.yaml` · tests: `tests/districts/`.

| Step | Module | Output |
|---|---|---|
| Apportionment | `apportionment.apportion` | seats + electoral votes per province |
| District generation | `generator.generate_plan` | `GeneratedPlan` (every buurt → one district) |
| Manual overrides | `overrides.apply_overrides` | plan with pinned units |
| Validation | `validation.validate_plan` | `PlanValidation(errors, warnings)` |
| Statistics / geometry | `stats.*` | per-district table, polygons, adjacency, fragments |
| Senate classes | `senate.assign_senate_classes` | `{province: (class seat 1, class seat 2)}` |
| Persistence | `service.*` | `apportionment`, `district_plan`, `house_district`, … rows |

## 1. Apportionment

The constitution (`app.core.constitution`) fixes 150 House seats, a minimum of one seat per
province and two senators per province.  Seats are apportioned by population; each province's
electoral votes are its seats plus its senators, so the 12 provinces hold 150 + 24 = 174 EV
(88 to win).  `apportion(populations, seats, method, min_seats)` validates that the seats sum to
the requested total and the electoral votes to House + Senate seats.

Every divisor method starts from the minimum and hands out the remaining seats one by one to the
province with the highest *priority* `P / d(n)` (`n` = seats it already has):

| method (aliases) | divisor `d(n)` | character |
|---|---|---|
| `huntington_hill` (default, U.S. since 1941) | `√(n(n+1))` | equal proportions |
| `webster` (`sainte_lague`) | `n + ½` | major fractions, unbiased |
| `jefferson` (`dhondt`) | `n + 1` | favours large provinces |
| `adams` | `n` (needs `min_seats ≥ 1`) | favours small provinces |
| `hamilton` (`largest_remainder`) | — | Hare quota, largest remainders; provinces below the minimum are fixed at it and the rest re-apportioned |

Ties are broken deterministically by population (descending), then province code.  The result
records exact quotas (`P·S/ΣP`), persons per seat, the priority order of every seat above the
minimum and the next five "first provinces out".  Divisor methods are house-monotone; Hamilton is
not (the Alabama paradox is demonstrated in `tests/districts/test_apportionment.py`).

With the CBS 2025 neighbourhood population (18.04 M) Huntington-Hill gives
ZH 32 · NH 25 · NB 22 · GE 18 · UT 12 · OV 10 · LI 9 · FR 6 · GR 5 · DR 4 · FL 4 · ZE 3
(Limburg would receive seat 151).

## 2. The district generator

Requirements: districts lie entirely within one province, are contiguous, have a population
within the target tolerance of the provincial target `T = province population / seats`
(default ±2 %, hard maximum ±5 %), preserve municipal boundaries wherever possible, are compact,
and are exactly reproducible from (data, seed, configuration).

Provinces are independent problems.  Each is solved by `partition.partition_province`, a pure
NumPy/SciPy engine (optionally in `spawn` worker processes — results are identical to a serial
run because every random stream is `make_rng(seed, "districts", <step>, <province>, <node>)`).

### 2.1 Unit graph, contiguity and water links

Units are the CBS buurten (land only, including zero-population buurten so districts cover all
territory).  The graph is the unit adjacency of the processed store (`border` edges with the
shared border length, `water_link` edges joining islands and other disconnected pieces to the
nearest unit of the same province).  Contiguity means *graph* contiguity over this adjacency,
so Texel, the Wadden islands or Zeeuws-Vlaanderen are contiguous with the district across the
water.  Only edges inside a province are used; if a province graph is nevertheless disconnected
the generator adds a synthetic water link between the nearest unit centroids (smallest component
first) and records a warning.  Edge weights are shared border lengths in km (water links count
`water_link_border_m`, default 250 m) and drive compactness.

### 2.2 Multi-resolution recursive bisection

A region that must hold `k` districts is cut into two connected sides holding `⌊k/2⌋` and
`⌈k/2⌉` districts (`split_slack` widens the choice), recursively, until every region holds one
district.

**Atoms.**  The cut is searched on an atom graph.  Initially every atom is a *whole municipality*
(precisely: a connected piece of a municipality inside the region, so exclaves and enclaves
behave).  Atoms are refined on demand, one level per round — municipality → CBS wijken →
buurten — only when no balanced, contiguous cut through whole atoms exists: the atoms at which
the orderings cross the balance point, and atoms too populous for either side, are refined.  A
big city is therefore only broken into wijken/buurten when a cut must pass through it.

**Candidate cuts** are prefixes of many atom orderings, all evaluated at once with prefix sums:

* `sweep_angles` straight sweep lines over atom centroids (seeded rotation);
* `geodesic_orderings` graph-distance orderings grown from extreme atoms (prefixes of a
  shortest-path ordering are always connected; they follow curved provinces);
* `centre_orderings` graph-distance orderings grown from the most populous atoms, which can carve
  a city (plus suburbs) out of the middle of a region;
* the spectral (Fiedler vector) ordering of the atom Laplacian (sparse shift-invert Lanczos).

For every ordering, prefix position and side assignment the engine computes the population of
both sides, the cut length (difference arrays over the atom edges) and the municipal-split
penalty (difference arrays over the rank range of every municipality's atoms).  The best
`candidates_per_ordering` positions per ordering are then checked exactly: connected components
of both sides; a disconnected side keeps its most populous component and the stray pieces move to
the other side (contiguity repair).

**Tolerance funnel.**  A side that will still be split into `k_s` districts must have an average
deviation within `target / k_s^tolerance_exponent` (exponent 0.5: ±2 % for a single district,
±1 % for a side of four, ±0.5 % for sixteen).  Loose upper cuts would force impossible windows
further down; absolute windows would force splits at every level.

**Score** (lower is better, among feasible candidates):
`deviation·(dev/tol) + Σ split penalties + cut_length·(cut km / √region area) + unbalanced·|k_A−k_B|`,
where a municipality split for the first time costs `new_split + small_split·(1 − pop/T)` (so
villages are avoided and cities preferred) and cutting an already split municipality again costs
`existing_split`.

**Exact search.**  For small regions (`k ≤ exact_search_max_k`, at most
`exact_search_max_atoms` atoms) where the orderings find no balanced whole-municipality cut, all
connected atom subsets with a fitting population are enumerated (ESU algorithm, bounded by
`exact_search_max_subsets`) before any municipality is refined.

**Balancing fallback.**  If even the finest resolution yields no cut within the funnel, the best
candidate is rebalanced by a two-part local search at buurt level (§2.3 with per-side targets);
only if that fails is the best-effort cut kept and a warning recorded.  A degenerate region
(e.g. a star-shaped graph) falls back to a geodesic prefix that leaves every side at least as
many units as districts, so no district can ever end up empty.

### 2.3 Local search

After the bisection, seeded passes move

* whole municipalities / municipal fragments,
* CBS-wijk fragments, and
* single boundary buurten

between adjacent districts of the province whenever the objective decreases:

```
1000 × Σ %-points beyond the hard maximum
 + 100 × Σ %-points beyond the target tolerance + 50 × districts beyond the target tolerance
 + 10 × split municipalities + 2 × extra municipal fragments
 + 1 × Σ (deviation in %)² + 0.2 × km of internal district boundary
```

(weights `local_search.weight_*`).  Moves never disconnect the source district (articulation
check by early-stopping BFS), only add pieces that touch the destination, never empty a district
and **never create a new municipal split unless the move reduces the excess over the
tolerances**.  When no single move helps, *ejection chains* are tried: move a small municipal
fragment into the district holding the rest of its municipality (un-splitting it), then rebalance
greedily for up to `chain_length` moves — the chain is kept only if the total objective improves;
districts still beyond the target tolerance get the same treatment starting from each of their
boundary buurten.

### 2.4 Restarts

`restarts` independent attempts per province (restart 0: the root seed and the configured
weights; restart r: a seed derived from (seed, province, r) and cut weights / funnel exponent
jittered by ±`restart_jitter`) are compared with the objective above; the best wins (ties: the
lowest restart).  Restarts are the main lever for fewer split municipalities.

### 2.5 Numbering and names

Districts are numbered per province in a serpentine order of their population-weighted
centroids: rows from north to south (row count ≈ √(k·height/width)), the first row west → east,
the next east → west, …  Codes are `<PV>-<NN>` (`NB-07`).

Names (`naming` in `config/districts.yaml`):

* a district with ≥ 95 % of its population in one municipality is named after it; when the
  municipality is split its significant parts (≥ 5 % of its population) get *distinct* compass
  suffixes assigned jointly (minimum angular mismatch; four directions for two parts, eight plus
  `Centrum` from four parts on): `Tilburg-Oost`, `Amsterdam-Zuidoost`, `Groningen-Noord`;
* otherwise, when the largest municipality holds < 50 % and a configured region holds ≥ 60 %, the
  most specific such region: `Zeeuws-Vlaanderen – Terneuzen`, `Twente – Almelo-Noord` (region
  first when the largest municipality belongs to it, e.g. `Deventer-Oost – Twente` otherwise);
* otherwise `<largest> – <second>` when the second municipality holds ≥ 20 %
  (`Middelburg (Z.) – Vlissingen`), else `<largest> e.o.` (*en omstreken*);
* remaining duplicates get Roman numerals.

Region names are a curated naming aid over REAL CBS municipality codes (2025 vintage); unknown
codes are ignored.  Municipality display names come from `municipality_names=` (the store's
`municipalities.parquet`) or a `municipality_name` column; without either the CBS code is used.

### 2.6 Parameters

All parameters live in `config/districts.yaml` (schema `app.districts.config.DistrictConfig`).
The most important:

| parameter | default | meaning |
|---|---|---|
| `target_deviation_pct` / `max_deviation_pct` | 2 / 5 | tolerance and hard maximum |
| `tolerance_exponent` | 0.5 | bisection funnel |
| `sweep_angles`, `geodesic_orderings`, `centre_orderings`, `spectral_ordering` | 18, 8, 3, true | candidate orderings |
| `candidates_per_ordering` | 3 | exactly checked prefixes per ordering |
| `max_refine_rounds`, `use_wijk_level` | 6, true | multi-resolution refinement |
| `exact_search_*` | k ≤ 4, ≤ 48 atoms, 6000 subsets | exhaustive whole-municipality search |
| `restarts`, `restart_jitter` | 12, 0.25 | multi-start |
| `cut.*` | see file | cut score weights |
| `local_search.*` | see file | move types, chains, objective weights |
| `water_link_border_m` | 250 | nominal border of a water link |
| `workers` | 1 | worker processes (0 = auto); not part of the hash |

`workers > 1` uses `spawn` processes (single-threaded BLAS inside the workers): scripts must
guard their entry point with `if __name__ == "__main__":`; a broken pool falls back to serial.

### 2.7 Reproducibility

The plan records the seed, `config_json` — canonical JSON (sorted keys) of the configuration
without `workers` plus the canonical override list — and `config_hash`
(`app.core.rng.config_hash`, 16 hex digits).  Same store + apportionment + seed + configuration
⇒ bit-identical assignment, names and numbers, independent of the unit table's row order and the
worker count (tested).  Timings, warnings and per-province diagnostics (cuts, refinements,
fallbacks, restarts and their objectives, local-search moves) are metadata only.  No wall-clock
limits are used anywhere, so results never depend on machine speed.

### 2.8 Results on the CBS 2025 store

Seed 2028, default configuration, 14 729 buurten, 342 municipalities: 150 districts, all
contiguous, maximum deviation 1.97 %, mean absolute deviation ≈ 0.6 %, 65 split municipalities
(23 of them are larger than a district and must be split), Polsby-Popper median ≈ 0.27.
Generation takes ≈ 25 s serially and ≈ 10 s with four workers (whole country, 12 restarts).

## 3. Split policy

* Small municipalities are grouped whole; a municipality is split only when no balanced
  contiguous cut through whole municipalities exists (after the exact search), or when it alone
  exceeds what a side can hold.
* Splits prefer municipalities that are already split and large ones (`small_split` penalty),
  and follow wijk boundaries before buurt boundaries.
* The local search removes splits whenever the deviations allow it (ejection chains) and never
  introduces one unless that improves feasibility.
* `stats.district_municipality_fragments` lists every district × municipality fragment
  (population, units, share of municipality, share of district); `HouseDistrict` stores
  `n_municipalities` and `n_split_municipalities`.

## 4. Statistics and geometry

`stats.district_stats(plan, units_gdf)` returns per district: population, eligible voters,
target, deviation %, land area km², Polsby-Popper `4πA/P²`, Reock `A / (π r²)` (minimum bounding
circle radius), convex-hull ratio, units, municipalities, split municipalities, urban share
(population in CBS urbanity class 1–2), rural share (4–5), `is_contiguous` / `n_components`
(graph components) and a label point (centroid, or a point on the surface when the centroid falls
outside) in lon/lat.

`stats.district_geometries` dissolves the buurten (fast coverage union, with a robust
`union_all` fallback for imperfect source coverages); `geometries_wkb` / `write_districts_geojson`
produce coverage-simplified (`shapely.coverage_simplify`, shared borders stay shared) EPSG:4326
output.  `stats.district_adjacency` lists adjacent district pairs with the shared border (km) and
`via_water_link` when they only touch through water links (pairs across province borders
included).

## 5. Manual overrides

`config/districts/overrides.yaml`:

```yaml
version: 1
overrides:
  - {unit: BU03630000, district: NH-03, note: "why"}
  - {municipality: GM0363, district: NH-03}
```

Applied after generation (`generate_plan(..., overrides=load_overrides())` or
`apply_overrides(plan, entries)`): municipality entries first, then unit entries; unknown codes,
cross-province moves and emptied districts are hard errors, resulting non-contiguity or deviation
problems are warnings.  Overridden units are stored with `DistrictAssignment.source = 'override'`
and the overrides are part of the configuration hash.  District codes depend on the generated
plan, so re-check overrides after changing the seed or configuration.

## 6. Validation

`validate_plan(plan, units_df, seats_by_province)` checks: 150 districts (the constitution's
`house_seats`), each province exactly its apportioned number, every unit assigned exactly once
(no missing, unknown or duplicate units), no district crossing a province boundary, no empty or
zero-population district, contiguity, and deviation ≤ the hard maximum (beyond the target
tolerance: warning).

## 7. Senate classes

Every province has two seats (`SEN-<PV>-1`, `SEN-<PV>-2`) in different classes; each class
holds 8 seats and is elected every two years.  `config/senate.yaml` partitions the provinces into
three groups of four with seat pairs {1,2}, {2,3} and {1,3}, drawn snake-style from the population
ranking so each group mixes large and small provinces and each class election covers 11.4–12.4 M
inhabitants.  If the configuration does not cover exactly the provinces a deterministic
round-robin assignment is generated (seat `j` of the `i`-th province → class `(2i+j) mod 3 + 1`).
`service.ensure_senate_seats` creates the 24 `SenateSeat` rows and their `Office` rows.

## 8. Persistence

`service.create_apportionment` (idempotent per vintage + method), `service.store_plan`
(plan + districts with statistics and WKB + bulk assignments + fragments + adjacency; the
previously active plan of the chamber is deactivated), `service.active_plan`,
`service.load_plan_mapping(session, plan_id, frame)` (unit → district index aligned to a
`GeographyFrame`) and `service.ensure_senate_seats`.

## 9. Limitations

* Recursive bisection fixes the population of large regions early; restarts, the exact search,
  the balancing fallback and ejection chains mitigate but do not guarantee the minimum number of
  split municipalities.
* Compactness is optimised through cut lengths (not directly through Polsby-Popper); coastal and
  island districts (Zeeland, Fryslân) have low Polsby-Popper values because of their coastlines.
* Buurten are indivisible; a buurt larger than the tolerance window (the largest has ≈ 30 000
  inhabitants) limits achievable equality in its surroundings.
* Region names are a hand-curated naming aid, not an official classification.
* With `workers > 1` the caller's main module must be import-safe (`spawn`).
