# Data provenance

The simulator puts a **fictional** U.S.-style federal system on top of the **real** geography of the
Netherlands. This document lists every external dataset, what is taken from it unchanged (REAL),
what is computed from it (DERIVED), and the known limitations. The pipeline code is in
`src/app/geography/` (`download.py`, `build.py`, `store.py`, `loader_db.py`).

Data categories (`app.core.constitution.DataCategory`):

| Category | Meaning in the geography store |
|---|---|
| **REAL** | Official CBS/PDOK figures as published: codes, names, boundaries, population counts, CBS indicators |
| **DERIVED** | Computed deterministically from REAL data. Includes eligible voters, density transforms, imputed gaps, water links, dissolves and simplified display geometry |
| **FICTIONAL / SIMULATED** | Never stored in the geography store. Districts, parties, votes and so on live elsewhere |

## 1. Sources

All sources are open data under **CC BY 4.0**. Attribution: *Centraal Bureau voor de Statistiek
(CBS) / PDOK*. They are configured in `config/geography.yaml`, downloaded to `data/raw/` by
`app.geography.download.download_all()`, and recorded with URL, SHA-256, size, retrieval time,
publisher and license in `data/raw/sources_<year>.json`. The same records are copied into
`data/processed/geo_<year>/manifest.json` and the `data_source` table.

| Key | Dataset | Publisher | Vintage | URL | File |
|---|---|---|---|---|---|
| `wijkenbuurten` | CBS **Wijk- en Buurtkaart 2025** (GeoPackage, layers `buurten`, `wijken`, `gemeenten`, EPSG:28992) | CBS via PDOK | 2025 | `https://service.pdok.nl/cbs/wijkenbuurten/2025/atom/downloads/wijkenbuurten_2025.gpkg` | `wijkenbuurten_2025.gpkg` (219,668,480 bytes) |
| `provinces` | CBS **Gebiedsindelingen 2025**: `provincie_gegeneraliseerd` (WFS, GeoJSON) | CBS via PDOK | 2025 | `https://service.pdok.nl/cbs/gebiedsindelingen/2025/wfs/v1_0?…typeNames=gebiedsindelingen:provincie_gegeneraliseerd&outputFormat=application/json` | `provincies_gegeneraliseerd_2025.geojson` |
| `municipalities_generalized` | CBS **Gebiedsindelingen 2025**: `gemeente_gegeneraliseerd` (WFS, GeoJSON) | CBS via PDOK | 2025 | `https://service.pdok.nl/cbs/gebiedsindelingen/2025/wfs/v1_0?…typeNames=gebiedsindelingen:gemeente_gegeneraliseerd&outputFormat=application/json` | `gemeenten_gegeneraliseerd_2025.geojson` |
| `supplement` | CBS StatLine **Kerncijfers wijken en buurten 2022** (table `85318NED`, OData feed) | CBS StatLine | 2022 | `https://opendata.cbs.nl/ODataFeed/odata/85318NED/TypedDataSet?$format=json&$select=…` | `kerncijfers_wijken_buurten_2022.json` (all `odata.nextLink` pages merged, 18,003 rows) |

SHA-256 digests of the build this document describes (retrieved 2026-09-24):

```
wijkenbuurten               11894edc9d7602786a829c281f9bc3cad395c450b66b41af6ebedfecf0741e34
provinces                   462278b47aa4c726fc6e7b126ca2af3549a64326f6f2125264edf0891b05967d
municipalities_generalized  670f22e1e99d0c73d69bf5d88518bb07e7694cce5125f3ebc18cec314dd5728d
supplement                  15ae38c88e6de4b93dd9985cf63abc140efc520a82134b279fadaebdb04ef38a
```

The WFS and OData responses are generated on request, so their bytes (and hashes) can change
when PDOK or CBS re-serialise them, even if the content is the same. The GeoPackage is a static file.

### Why a 2022 supplement?

The 2025 Wijk- en Buurtkaart has no education, income or housing figures, because CBS publishes
those later than the core population figures. Three model variables therefore come from the
most recent complete *Kerncijfers* release (2022): education level (low / mid / high, counts of
people aged 15–75), average income per inhabitant (k€) and the share of owner-occupied homes. The
supplement is joined on CBS codes with a fallback chain: buurt code → wijk code → gemeente code.

## 2. What is REAL

* **Units** are the land neighbourhoods (*buurten*, `water == 'NEE'`) of the Wijk- en Buurtkaart,
  with their official code (`BU…`), name, wijk code, municipality code and polygon. Zero-population
  buurten are included (499 in 2025: industrial estates, nature, harbours), so districts cover all
  land territory. Water buurten (`'JA'`) and the "Buitenland" record (`'B'`) are excluded.
* **Population** per buurt (`aantal_inwoners`) and the official municipal totals
  (`population_official`, from the `gemeenten` layer).
* **CBS indicators**: age bands, single-person households, households with children, average
  household size, migration background (NL / Europe / outside Europe), address density
  (`omgevingsadressendichtheid`) and urbanity class (`stedelijkheid`). Supplement indicators are
  listed in §1.
* **Municipality set**: 342 municipalities in 2025. The count is derived from the data and
  checked against the generalised municipality layer (342 features).

## 3. What is DERIVED

Every derived value is deterministic. The build is reproducible to the byte (see §6).

| Quantity | Definition |
|---|---|
| `population` (municipality, province) | Sum of the unit populations. This is canonical for all aggregation, so municipal sums differ slightly from `population_official` because CBS rounds buurt figures (see §5) |
| `eligible_voters_est` | `round(pop × ((100 − pct_0_15 − pct_15_25)/100 + 0.7 × pct_15_25/100) × 0.93)`, clipped to `[0, pop]`. Adults are approximated from the CBS age bands (0.7 of the 15–24 band is 18+). `0.93` is a flat citizenship/registration factor. Both parameters are in `config/geography.yaml` (`eligible_voters`). National total for 2025 ≈ 13.66 M |
| `density`, `log_density` | Inhabitants per km² of land (`oppervlakte_land_in_ha`; geometric area when CBS reports 0 ha), and `ln(1 + density)` |
| `urbanity` | `6 − stedelijkheid class`, so 1 = not urban … 5 = very urban. A missing class is derived from the address density with the CBS thresholds: ≥ 2500 → 1, 1500–2500 → 2, 1000–1500 → 3, 500–1000 → 4, < 500 → 5. Because the class is defined this way, the derived class is not flagged as imputed |
| `pct_education_*` | `count / (low + mid + high) × 100` from the supplement counts |
| Imputed gaps | See §4. Always flagged in `imputed_fields` |
| Province of a municipality | Largest area overlap of the municipality polygon with the generalised province polygons (smallest overlap in 2025: 96.4 %, Terschelling, because of generalised coastlines). Units inherit their municipality's province |
| Municipality / province geometry | Coverage union of the member buurt polygons, so it is consistent with the units by construction |
| Coverage repair | 11 buurt polygons in Hoeksche Waard / Nissewaard / Goeree-Overflakkee have sub-millimetre overlaps with their neighbours. They are snapped onto the neighbours and the overlap is removed (largest area change 0.08 m² per polygon). The codes are listed in `manifest.json` (`coverage_repaired_units`) |
| Adjacency | Rook contiguity: the border shared within a 2 m snap buffer must be ≥ 20 m, so point touches are excluded |
| **Water links** | Edges (`kind = 'water_link'`, `gap_m` = nearest-polygon distance) that make each province's unit graph connected. For 2025: Ameland ↔ mainland Fryslân (3.8 km), Ameland ↔ Terschelling (1.4 km), Texel ↔ Den Helder (1.9 km), two IJburg island links in Amsterdam (Zeeburgereiland ↔ Buiteneiland 186 m, Diemerpark ↔ Rieteiland-Oost 45 m), Vogeleiland (Enkhuizen) ↔ Andijk (Medemblik, 4.4 km), and the IJmeer buurt ↔ Noordpolder (Gooise Meren, 0.5 km). Vlieland (which touches Terschelling) and Schiermonnikoog (which touches Noardeast-Fryslân) are connected through the Wadden-sea parts of their land-buurt polygons, so they need no link of their own. Every link is logged at build time and listed in the manifest |
| Web GeoJSON | Coverage-simplified (`shapely.coverage_simplify`; tolerances in `config/geography.yaml`), reprojected to EPSG:4326 and snapped to 5 decimals (≈ 1 m) with the GEOS precision reducer (`shapely.set_precision`). Plain rounding produced self-intersecting or collapsed rings in 2 provinces, 6 municipalities and several units; the precision reducer keeps every polygon valid. Shared borders stay shared. For display only: all computation uses the full-resolution EPSG:28992 store |

## 4. Missing values and imputation

CBS marks suppressed or unavailable values with large negative codes (`-99997`, `-99999999`, …).
The pipeline treats **every value ≤ −99990 as missing** (`app.geography.cbs.clean_missing`).
Gaps are filled in this order, and **every value that is not the unit's own figure is flagged**
in the comma-separated `imputed_fields` column (units and municipalities, the database
`*_demographics.imputed_fields`, and `GeographyFrame.unit_demo_imputed`):

1. **Population.** For each municipality, the residual (official total − sum of known buurt
   figures, if positive) is spread over its buurten with a missing figure. The spread is
   proportional to address density × land area, or to land area alone when an address density
   is missing. Integers are allocated by largest remainder. Flag: `population` (and
   `log_density`). No buurt population is missing in 2025.
2. **Compositional core indicators (2025): age bands and migration background.** CBS
   suppresses individual cells, so a buurt often has some of its five age bands (or three origin
   shares) published and others missing. Filling each band on its own from a parent area mixes two
   compositions. Before this was fixed, 1,119 buurten had age bands summing to 53–176 %.
   Instead, the published parts are kept, and only the remainder `max(0, 100 − Σ published)` is
   split over the missing parts. The split follows a reference composition: the containing wijk if
   it is complete, else the municipality, else the population-weighted mean composition of the
   complete buurten in the municipality → province → nation. A buurt with every part missing takes
   the reference composition rescaled to 100 %. Only the filled parts are flagged
   (`app.geography.demographics.fill_composition`).
3. **Other core indicators (2025).** Buurt value → the containing wijk's value → the
   municipality's value (all official CBS figures).
4. **Supplement indicators (2022).** Buurt → wijk → gemeente, joined on code. The education
   shares come from complete low/mid/high counts, so they always sum to 100 %.
5. **Anything still missing.** Population-weighted mean of the observed values in the
   municipality → province → nation.

Counts for 2025 (units with a flag, out of 14,729) are in `manifest.json` under `imputation`.
For example: 15–25 age share 1,521 (almost all small or zero-population buurten), education
3,263, income 3,716, owner-occupied housing 2,813, urbanity 88. At municipality level only
**Voorne aan Zee** (GM1992, formed on 1 January 2023) is imputed. It has no 2022 supplement
record, so its education, income and housing values are population-weighted province means.

## 5. Buurt as precinct, and CBS rounding and confidentiality

**Buurten as precincts.** Dutch elections are counted per polling station (*stembureau*), but
there are no official polling-district polygons: voters may vote at any station in their
municipality. The CBS buurt is the finest official unit that has both boundaries and statistics,
so the simulator uses it as the precinct substitute (≈ 14.7k units, median ≈ 720 inhabitants).
All simulated "precinct" results are therefore buurt results. They are **not** real polling-
station results.

**Rounding and confidentiality in CBS neighbourhood figures.**

* Buurt populations are rounded to multiples of 5. Summed per municipality, they therefore differ
  slightly from the official municipal totals: +93 people nationally in 2025 (18,044,120 vs
  18,044,027), less than 0.1 % in every province.
* Percentages are published as whole numbers (age bands) or multiples of 5 (migration
  background at buurt level), so published compositions sum to 98–102 % (age) and 95–105 %
  (origin). Rows with imputed parts sum to exactly 100 %, unless the published parts alone
  already exceed it (§4).
* Indicators are suppressed (`-99997`) for buurten with few inhabitants or households. They are
  imputed and flagged as described in §4.
* Supplement figures (2022) describe the buurt as it was delimited in 2022. Where CBS re-coded
  buurten or municipalities merged since then, the wijk or gemeente fallback applies (flagged).

## 6. Reproducing the store

```bash
source .venv/bin/activate
python -c "from app.geography.download import download_all; download_all()"   # ≈ 10 s (GPKG reused if present)
python -c "from app.geography.build import build_geography; print(build_geography(2025).counts)"  # ≈ 45 s
```

Once the CLI is wired in, the same steps run as `python -m app geography download` and
`python -m app geography build`. Set `NLFED_ALLOW_NETWORK=false` to guarantee offline operation.
The download then fails with a clear error if a file is missing.

* The build is **idempotent**: it is skipped when `manifest.json` has the same fingerprint, which
  covers the source hashes, the build parameters and the schema version. Pass `force=True` to
  rebuild. `app.geography.build.SCHEMA_VERSION` is bumped whenever an algorithm changes. Schema 2
  added the compositional imputation and the valid web geometry. A store built by older code
  is rebuilt by the next `build_geography()` call, and stays readable until then.
* The build is **deterministic**: a forced rebuild reproduces every Parquet and GeoJSON file
  byte for byte. Per-file SHA-256 digests are in `manifest.json` (`files`).
* The build writes into a temporary directory and swaps it into place with two renames. Readers
  never see a partially written store; the old directory is only absent for the instant between
  the renames. If the second rename fails, the previous store is restored.

Loading into the database:
`app.geography.loader_db.load_into_db(session, 2025)` takes ≈ 1.2 s on SQLite and is idempotent.
The loader records the store's manifest fingerprint in `app_meta`
(`geo_vintage_fingerprint_<year>`). A later load of a rebuilt store whose content changed
re-synchronises the rows in place and keeps the database ids. Invalid EPSG:4326 input geometry
is repaired with `make_valid` before it is stored.

Output layout: see `docs/ARCHITECTURE.md` §4. `manifest.json` records the sources, counts,
parameters, imputation counts, water links, province-assignment diagnostics, per-step timings
and library versions.

## 7. Known limitations

* **Eligible voters** are an estimate. There is no citizenship data per buurt, so a flat factor
  is applied, and the 18+ share of the 15–24 band is approximated.
* **Temporal mismatch.** Education, income and housing (2022) are combined with the 2025
  geometry and population. Buurten that CBS re-delimited since 2022 get wijk or gemeente values
  (flagged).
* **Land buurten contain water.** CBS land-buurt polygons include adjacent inland and coastal
  water (for example the Wadden sea around the islands), so geometric `area_km2` exceeds
  `land_area_km2`. Density always uses land area.
* **Water links are synthetic connections** for contiguity, not real transport links.
  District-building code can tell them apart through `kind`/`gap_m`.
* **Generalised province polygons** are used only to assign municipalities. Province boundaries in
  the store are the union of their buurten, not the generalised layer.
* **Web geometry is simplified** (20–150 m tolerances) and only suitable for display.
* **Lineage between vintages** (`app.geography.lineage`) matches buurten by code, then by
  location (point in polygon or nearest centroid). Where a merger re-codes all buurten, the
  match relies on centroids.
