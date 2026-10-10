# Data provenance

The simulator puts a **fictional** U.S.-style federal system on top of the **real** geography of the
Netherlands. This document lists every external dataset, what is taken from it unchanged (REAL),
what is computed from it (DERIVED), and the known limitations. The pipeline code is in
`src/app/geography/` (`download.py`, `build.py`, `store.py`, `loader_db.py`).

Data categories (`app.core.constitution.DataCategory`):

| Category | Meaning in the geography store |
|---|---|
| **REAL** | Official CBS/PDOK figures as published: codes, names, boundaries, population counts, CBS indicators; the water board areas, codes and names of Het Waterschapshuis |
| **DERIVED** | Computed deterministically from REAL data. Includes eligible voters, density transforms, imputed gaps, water links, dissolves and simplified display geometry |
| **FICTIONAL / SIMULATED** | Never stored in the geography store. Districts, parties, votes and so on live elsewhere |

## 1. Sources

All sources are open data. The CBS datasets are under **CC BY 4.0** (attribution: *Centraal
Bureau voor de Statistiek (CBS) / PDOK*); the water board areas are under **CC0 1.0** (public
domain dedication; source: *Het Waterschapshuis via PDOK*). They are configured in
`config/geography.yaml`, downloaded to `data/raw/` by `app.geography.download.download_all()`
(`python -m app geography download`), and recorded with URL, SHA-256, size, retrieval time,
publisher, license and data category (all `REAL`) in `data/raw/sources_<year>.json`. The same
records are copied into `data/processed/geo_<year>/manifest.json` and the `data_source` table.

| Key | Dataset | Publisher | Vintage | URL | File |
|---|---|---|---|---|---|
| `wijkenbuurten` | CBS **Wijk- en Buurtkaart 2025** (GeoPackage, layers `buurten`, `wijken`, `gemeenten`, EPSG:28992) | CBS via PDOK | 2025 | `https://service.pdok.nl/cbs/wijkenbuurten/2025/atom/downloads/wijkenbuurten_2025.gpkg` | `wijkenbuurten_2025.gpkg` (219,668,480 bytes) |
| `provinces` | CBS **Gebiedsindelingen 2025**: `provincie_gegeneraliseerd` (WFS, GeoJSON) | CBS via PDOK | 2025 | `https://service.pdok.nl/cbs/gebiedsindelingen/2025/wfs/v1_0?…typeNames=gebiedsindelingen:provincie_gegeneraliseerd&outputFormat=application/json` | `provincies_gegeneraliseerd_2025.geojson` |
| `municipalities_generalized` | CBS **Gebiedsindelingen 2025**: `gemeente_gegeneraliseerd` (WFS, GeoJSON) | CBS via PDOK | 2025 | `https://service.pdok.nl/cbs/gebiedsindelingen/2025/wfs/v1_0?…typeNames=gebiedsindelingen:gemeente_gegeneraliseerd&outputFormat=application/json` | `gemeenten_gegeneraliseerd_2025.geojson` |
| `supplement` | CBS StatLine **Kerncijfers wijken en buurten 2022** (table `85318NED`, OData feed) | CBS StatLine | 2022 | `https://opendata.cbs.nl/ODataFeed/odata/85318NED/TypedDataSet?$format=json&$select=…` | `kerncijfers_wijken_buurten_2022.json` (all `odata.nextLink` pages merged, 18,003 rows) |
| `water_boards` | **Waterschappen Administratieve eenheden (INSPIRE geharmoniseerd)**: the areas of the 21 water authorities (GML) | Het Waterschapshuis via PDOK (CC0 1.0) | current boundaries at retrieval | `https://service.pdok.nl/hwh/waterschappen-administratieve-eenheden/atom/downloads/administrativeunit.gml` (ATOM feed: `…/atom/administratieve-eenheden.xml`) | `waterschappen_administratieve_eenheden.gml` (7,059,188 bytes) |

SHA-256 digests of the build this document describes (CBS sources retrieved 2026-09-24, water
boards retrieved 2026-10-09):

```
wijkenbuurten               11894edc9d7602786a829c281f9bc3cad395c450b66b41af6ebedfecf0741e34
provinces                   462278b47aa4c726fc6e7b126ca2af3549a64326f6f2125264edf0891b05967d
municipalities_generalized  670f22e1e99d0c73d69bf5d88518bb07e7694cce5125f3ebc18cec314dd5728d
supplement                  15ae38c88e6de4b93dd9985cf63abc140efc520a82134b279fadaebdb04ef38a
water_boards                550146ee4cc4e908c134b6db78256657637a486f08d1e9e930f240748b148800
```

The WFS and OData responses are generated on request, so their bytes (and hashes) can change
when PDOK or CBS re-serialise them, even if the content is the same. The GeoPackage is a static file.

### 1a. Water boards (waterschappen)

The Netherlands has 21 regional water authorities. Their areas cross municipal and provincial
borders, so they form a geography of their own next to province → municipality → buurt. The
simulator keeps the **areas** as REAL data; anything **elected** in a water board is FICTIONAL.

* **Dataset.** Het Waterschapshuis publishes the harmonised INSPIRE *Administrative Units* of the
  water boards on PDOK under CC0 1.0 (ATOM feed
  `https://service.pdok.nl/hwh/waterschappen-administratieve-eenheden/atom/administratieve-eenheden.xml`,
  metadata record `2d4ec7e6-50ae-4c71-bd20-5b9211e2135f` in the Nationaal Georegister). The file is
  a WFS 2.0 GML `FeatureCollection` of 21 `au:AdministrativeUnit` features; the feed and the file
  carry no vintage (the feed reported `updated 2026-10-08`, the HTTP `Last-Modified` was
  2026-10-08 and the collection `timeStamp` reads 2020-05-13). It describes the boundaries current
  at retrieval, which the store combines with the 2025 buurten.
* **Fields used.** `au:nationalCode` (e.g. `33`) → store code `WS33` (`WS` + two digits);
  `gml:name` (e.g. *Waterschap Hunze en Aa's*); `au:geometry`, a `gml:MultiSurface` with
  `srsName="urn:ogc:def:crs:EPSG::4258"` (ETRS89) and 3-D `gml:posList`s
  (`srsDimension="3"`) in **latitude, longitude, height** order (the EPSG axis order that GML 3.2
  follows). The ATOM entry advertises EPSG:28992, but the file itself is in EPSG:4258.
* **Parsing.** GDAL/pyogrio cannot read these 3-D multi-surfaces ("Geometry type is not supported"),
  so `app.geography.water_boards.read_water_boards_gml` parses the XML with `xml.etree.ElementTree`:
  exterior and interior rings (holes) become shapely polygons, the axes are swapped to
  longitude/latitude, the height is dropped, and the coordinates are projected to EPSG:28992 with
  pyproj. All 21 polygons are valid as published (33 polygons, 23 holes; *Brabantse Delta* has 10
  parts). Invalid input would be repaired with `make_valid`.
* **Quality of the published polygons.** Neighbouring boards do not form a perfect coverage: they
  overlap by 0.15 km² in total (largest 0.07 km², Hunze en Aa's / Noorderzijlvest) and leave
  sliver gaps along shared borders. The polygons include water (IJsselmeer, Wadden Sea, Zeeland
  estuaries), so `area_km2` (41,452 km² summed) exceeds the land area.
* **Assignment of buurten** (`app.geography.water_boards.assign_units`; DERIVED). A buurt belongs
  to the board whose polygon contains its *representative point*: the centroid when it lies inside
  the buurt (14,162 of 14,729 buurten), otherwise a point on its surface
  (`shapely.point_on_surface`). A point inside several (overlapping) polygons goes to the board
  holding the largest share of the buurt. A point outside every polygon (gaps, coastline slivers)
  falls back to the **nearest** board within `water_boards.nearest_max_m` = 2,000 m
  (`config/geography.yaml`); beyond that the buurt has no board (`water_board_code` null,
  frame index −1, `geo_unit.water_board_id` NULL). Every case is listed in `manifest.json`
  (`water_boards.nearest`, `water_boards.unassigned`).

  **Result for 2025: all 14,729 buurten lie inside exactly one board polygon** (`within`
  14,729, `nearest` 0, `none` 0). A buurt crossed by a board boundary belongs to one board as a
  whole: 933 buurten intersect a second board, 231 have at least 10 % of their area outside their
  board and 30 (23,220 inhabitants) at least half — mostly large rural buurten (median 5.0 km²,
  against 0.53 km² for all buurten) whose representative point lies in the smaller part; the most
  populous is Kudelstaart (Aalsmeer, 9,305 inhabitants, 51 % outside Rijnland)
  (`manifest.json`, `water_boards.boundary`).
* **Board totals** (DERIVED): population, estimated eligible voters, buurt and municipality
  counts are sums over the assigned buurten, so they reconcile exactly with the units (the
  national total is 18,044,120). The **province of a board** (`province_code`, the province
  that would hold its fictional election) is the province with most of the board's estimated
  eligible voters; `provinces` lists every province with buurten in the board. 12 of the 21 boards
  cross a province border.

| code | name | province (election) | provinces with buurten | buurten | population | est. eligible voters | area km² |
|---|---|---|---|---|---|---|---|
| `WS02` | Wetterskip Fryslân | FR | FR, GR | 949 | 670,490 | 508,724 | 5,762 |
| `WS07` | Waterschap Rijn en IJssel | GE | GE, OV | 589 | 634,515 | 491,054 | 1,949 |
| `WS09` | Waterschap Rivierenland | GE | GE, ZH, UT, NB | 1,047 | 1,068,900 | 801,254 | 2,001 |
| `WS11` | Waterschap Amstel, Gooi en Vecht | NH | NH, UT, ZH | 841 | 1,374,440 | 1,051,582 | 774 |
| `WS12` | Hoogheemraadschap Hollands Noorderkwartier | NH | NH | 1,064 | 1,204,795 | 912,703 | 3,133 |
| `WS13` | Hoogheemraadschap van Rijnland | ZH | ZH, NH | 1,016 | 1,340,990 | 1,009,989 | 1,130 |
| `WS14` | Hoogheemraadschap De Stichtse Rijnlanden | UT | UT, ZH | 490 | 870,400 | 648,153 | 830 |
| `WS15` | Hoogheemraadschap van Delfland | ZH | ZH | 556 | 1,270,815 | 951,036 | 448 |
| `WS25` | Waterschap Brabantse Delta | NB | NB | 600 | 858,865 | 655,255 | 1,705 |
| `WS27` | Waterschap De Dommel | NB | NB | 739 | 951,815 | 726,167 | 1,510 |
| `WS33` | Waterschap Hunze en Aa's | GR | GR, DR | 578 | 431,625 | 332,373 | 2,074 |
| `WS34` | Waterschap Noorderzijlvest | GR | GR, DR, FR | 359 | 358,350 | 272,626 | 1,904 |
| `WS37` | Waterschap Zuiderzeeland | FL | FL, FR, OV | 490 | 458,365 | 334,241 | 2,418 |
| `WS38` | Waterschap Aa en Maas | NB | NB | 681 | 794,725 | 605,926 | 1,610 |
| `WS39` | Hoogheemraadschap van Schieland en de Krimpenerwaard | ZH | ZH | 348 | 657,140 | 493,309 | 361 |
| `WS40` | Waterschap Hollandse Delta | ZH | ZH | 628 | 894,430 | 670,066 | 1,437 |
| `WS42` | Waterschap Scheldestromen | ZE | ZE | 453 | 392,950 | 300,311 | 2,928 |
| `WS43` | Waterschap Vallei en Veluwe | GE | GE, UT, OV | 941 | 1,193,540 | 887,907 | 2,456 |
| `WS44` | Waterschap Vechtstromen | OV | OV, DR, GE | 745 | 832,665 | 630,864 | 2,260 |
| `WS59` | Waterschap Drents Overijsselse Delta | OV | OV, DR | 683 | 649,010 | 486,081 | 2,551 |
| `WS60` | Waterschap Limburg | LI | LI | 932 | 1,135,295 | 887,210 | 2,210 |
| | **total** | | | **14,729** | **18,044,120** | **13,656,831** | 41,452 |

Province codes: see `config/geography.yaml`. Population is the CBS buurt population (rounded to
multiples of 5, §5); eligible voters are the DERIVED estimate of §3.

**Store and database.** `water_boards.parquet` holds the board table (`code, name,
national_code, province_code, provinces, population, eligible_voters_est, unit_count,
municipality_count, area_km2`) with the REAL board polygon (EPSG:28992) as geometry;
`units.parquet` / `units_attrs.parquet` carry `water_board_code`; `web/water_boards.geojson` is
the display layer (see §3). The loader writes the `water_board` table and
`geo_unit.water_board_id` (docs/DATABASE.md §1a). The synthetic test country has 4 invented
boards (`WS01`–`WS04`, "Synthetic Water Board 1" …: vertical bands of municipalities of roughly
equal population), which are not real data.

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
* **Water boards**: the 21 water board areas with their national codes (`WS<nn>`), names and
  polygons as published by Het Waterschapshuis (reprojected to EPSG:28992), and the polygon area
  `area_km2` (§1a).

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
| Water board of a buurt | The board whose REAL polygon contains the buurt's representative point; nearest board within 2 km otherwise; none beyond (§1a). 2025: 14,729 within, 0 nearest, 0 none |
| Water board totals and province | Population, eligible voters, buurt and municipality counts: sums over the assigned buurten. Province of a board: most estimated eligible voters (ties: population, then canonical order) |
| Water board display geometry | `web/water_boards.geojson` is the coverage union of the buurten assigned to each board, simplified like the other web layers (150 m, `simplify.water_boards_m`). It is consistent with the unit, municipality and province layers and shows the electorate of each board; the REAL board polygon is in `water_boards.parquet` |
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
  added the compositional imputation and the valid web geometry; schema 3 added the water boards
  (`water_boards.parquet`, `units.water_board_code`, `web/water_boards.geojson`, the
  `water_boards` section of the manifest and the `units_water_board_*` counts). The water board
  file is one of the hashed sources, so a changed download rebuilds the store. A store built by
  older code is rebuilt by the next `build_geography()` call, and stays readable until then (an
  older store simply has no water boards).
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
* **Water boards are assigned per buurt.** Board boundaries do not follow buurt boundaries; a
  buurt crossed by one belongs to a single board (its representative point) with all its
  residents. 30 buurten (23,220 inhabitants) have most of their area inside a neighbouring
  board's polygon (§1a); there is no sub-buurt population to split them. The water board file has
  no vintage: it holds the boundaries current at retrieval, combined with the 2025 buurten.
* **Water board polygons overlap and leave slivers** (§1a); the assignment is robust to both
  (largest share, nearest fallback), but `area_km2` is the published polygon area including water.
