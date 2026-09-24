# Architecture

NL Federal Election Simulator transplants U.S.-style federal institutions onto the **real**
geography of the Netherlands. This document is the engineering contract between subsystems:
package layout, data flow, public APIs, and conventions. The reference docs are
[ELECTORAL_SYSTEM.md](ELECTORAL_SYSTEM.md), [SIMULATION.md](SIMULATION.md),
[DISTRICTING.md](DISTRICTING.md), [FORECASTING.md](FORECASTING.md),
[RACE_CALLING.md](RACE_CALLING.md), [DATABASE.md](DATABASE.md) and
[DATA_PROVENANCE.md](DATA_PROVENANCE.md).

## 1. Two kinds of data

| Category | Examples | Where |
|---|---|---|
| **REAL** | provinces, municipalities, CBS buurten (neighbourhoods), boundaries, population, CBS demographics | `data/raw`, `data/processed/geo_<year>`, tables `province*`, `municipality*`, `geo_unit*`, `data_source` |
| **DERIVED** | estimated eligible voters, log density, imputed demographic gaps (flagged) | same, with `imputed_fields` flags |
| **FICTIONAL** | constitution, House districts, Senate classes, parties, candidates, baselines, campaign effects | `config/`, tables `district_plan`, `house_district`, `party*`, `candidate*`, `scenario` |
| **SIMULATED** | votes, turnout, election nights, race calls, polls, forecasts | tables `election_result`, `reporting_event`, `race_call`, `poll*`, `forecast_*` |

`app.core.constitution.DataCategory` enumerates these. Every API payload carries a
`data_category` (or per-field provenance) and the UI shows a badge for it.

## 2. Package layout

```
src/app/
  core/         constitution (canonical constants), settings, config loader, rng (seed streams), logging, errors
  models/       SQLAlchemy ORM (persistence only — no election logic)
  db/           engine/session, Alembic migrations (db/migrations)
  geography/    download → build (GeoParquet store) → GeographyFrame; adjacency; simplification; DB loader
  districts/    apportionment, district generator, overrides, stats, validation, Senate classes, DB service
  elections/    engine types, tabulation, Electoral College, contingent election, calendar, recounts, seats
  simulation/   structural political model, calibration, vote simulation, candidate generation, baselines
  forecasting/  Monte Carlo engine (vectorised, chunked, multiprocessing)
  polling/      poll generation, aggregation (recency/sample/pollster weights, house effects, trend)
  reporting/    election-night timeline, race-calling engine, live night engine, playback clock
  campaigns/    campaign planning and (modest, uncertain) effects
  scenarios/    scenario document schema, editor services, import/export
  analytics/    swing, lean, elasticity, efficiency gap, competitiveness, tipping point, history queries
  export/       CSV/JSON exporters with stable column schemas
  services/     orchestration: election creation, simulation persistence, demo builder, history, validation
  api/          FastAPI application and routers (JSON only; no election math)
  ui/static/    single-page broadcast-style frontend (vanilla ES modules + vendored Leaflet)
  cli/          Typer CLI (`python -m app …`)
  utils/        small generic helpers
```

Rules (spec §42):
* **Engines are pure** (NumPy/pandas in, dataclasses out). They never import SQLAlchemy.
* **Services** translate ORM ⇄ engine types and own transactions.
* **API/UI contain no election mathematics** — they call services and render results.
* **No global random state.** All randomness flows from `app.core.rng.make_rng(seed, *stream_keys)`.
* **Constitutional numbers** come from `app.core.constitution` (`HOUSE_SEATS`, `ELECTORAL_VOTES`,
  `PRESIDENTIAL_MAJORITY`, …) or the validated `ConstitutionConfig` (`get_constitution()`).

## 3. Data flow

```
PDOK/CBS ──download──▶ data/raw ──build──▶ data/processed/geo_2025 (GeoParquet + web GeoJSON)
                                              │
                                              ├──▶ GeographyFrame (vectorised arrays)
                                              └──▶ DB: province, municipality, geo_unit (+demographics)
apportion(populations) ──▶ 150 seats → EV = seats + 2  (174 total)
generate_plan(units, adjacency, seats, seed) ──▶ 150 districts (unit → district), stats, geometry
scenario YAML ──▶ StructuralModel(frame, scenario) ──▶ simulate_election(races, seed) ──▶ ElectionDraw
ElectionDraw ──▶ tabulation / Electoral College / seats ──▶ DB (unit → municipality → district → province → national)
ElectionDraw ──▶ generate_timeline ──▶ reporting_event(s) ──▶ NightEngine + RaceCaller ──▶ race_call(s)
StructuralModel + polls ──▶ Monte Carlo ──▶ forecast_* tables
```

## 4. Processed geography store (contract)

Directory `data/processed/geo_<year>/` produced by `app.geography.build.build_geography`:

| File | Content (CRS) |
|---|---|
| `provinces.parquet` | GeoParquet EPSG:28992: `code, cbs_code, name, population, area_km2, land_area_km2, geometry` |
| `municipalities.parquet` | GeoParquet EPSG:28992: `code, name, province_code, population, eligible_voters_est, area_km2, land_area_km2, density, urbanity_class, address_density, centroid_x, centroid_y, centroid_lon, centroid_lat, <demographics…>, imputed_fields, geometry` |
| `units.parquet` | GeoParquet EPSG:28992, one row per **land** CBS buurt (including zero-population buurten so districts cover all territory): `code, name, wijk_code, municipality_code, province_code, population, eligible_voters_est, area_km2, land_area_km2, density, urbanity_class, address_density, centroid_x, centroid_y, centroid_lon, centroid_lat, <DEMOGRAPHIC_VARIABLES…>, imputed_fields, geometry` |
| `units_attrs.parquet` | same as units without geometry (fast frame loading) |
| `unit_adjacency.parquet` | `a, b, shared_border_m, kind` (`border` or `water_link`), a < b by code |
| `municipality_adjacency.parquet` | same for municipalities |
| `web/provinces.geojson`, `web/municipalities.geojson` | EPSG:4326, coverage-simplified, 5-decimal precision, properties `code, name, province_code` |
| `manifest.json` | sources (url, sha256, retrieved_at, license), counts, build parameters, timings |

Demographic columns are exactly `app.geography.frame.DEMOGRAPHIC_VARIABLES`. `imputed_fields` is a
comma-separated list of variables that were imputed for that row.

## 5. Engine types (`app.elections.types`)

`BallotLine`, `RaceSpec`, `UnitTurnout`, `RaceVotes`, `ElectionDraw`, `TabulatedRace` — see the module.
Race codes: `PRES` (national parent), `PRES-<PV>` (province EV contest), `HOUSE-<PV>-<NN>`,
`SEN-<PV>-<1|2>`, `GOV-<PV>`, `MAYOR-<GMxxxx>`, `COUNCIL-<GMxxxx>`, `PROVLEG-<PV>`.

## 6. Module APIs

### geography
* `download.download_all(year=None, force=False) -> list[DownloadRecord]`
* `build.build_geography(year=None, force=False) -> BuildReport`
* `store.is_prepared(year=None) -> bool`, `store.load_frame(year=None) -> GeographyFrame` (cached),
  `store.load_units_gdf(year)`, `store.load_municipalities_gdf(year)`, `store.load_provinces_gdf(year)`,
  `store.load_unit_adjacency(year)`, `store.web_geojson_path(year, layer)`, `store.manifest(year)`
* `adjacency.compute_adjacency(gdf, code_col, snap_buffer_m, min_shared_border_m) -> DataFrame`,
  `adjacency.connect_components(adj, gdf, group_col) -> DataFrame`
* `simplify.coverage_simplify(gdf, tolerance_m)`, `simplify.write_web_geojson(gdf, path, props, tolerance_m)`
* `loader_db.load_into_db(session, year=None) -> GeoVintage`

### districts
* `apportionment.apportion(populations: Mapping[str,int], seats, method, min_seats=1) -> ApportionmentResult`
* `generator.generate_plan(units_gdf, adjacency, seats_by_province, config, seed, overrides=None) -> GeneratedPlan`
* `stats.district_stats(plan, units_gdf) -> DataFrame`, `stats.district_geometries(plan, units_gdf)`
* `validation.validate_plan(plan, units_df, seats_by_province) -> list[str]`
* `senate.assign_senate_classes(province_codes, config) -> dict[str, tuple[int, int]]`
* `service.*` — persistence of apportionments / plans / Senate seats and `load_plan_mapping(session, plan_id, frame)`

### simulation
* `structural.StructuralModel.build(frame, scenario, model_config=None, regions=None) -> StructuralModel`
* `voting.simulate_election(model, races, seed, context) -> ElectionDraw`
* `voting.expected_race_shares(model, race, context) -> (n, L) array` (no shocks; used by calling/forecast)
* `candidates.generate_down_ballot(...)`, `candidates.fictional_name(rng, gender)`
* `baselines.import_historical_csv(...)`, `baselines.province_lean(...)`, `baselines.elasticity(...)`

### elections
* `tabulation.tabulate(race_votes, tie_seed) -> TabulatedRace`, `tabulation.aggregate_levels(race_votes, frame, unit_district=None) -> dict[str, DataFrame]`
* `electoral_college.allocate(province_results, ev_by_province, method) -> EVOutcome`, `tipping_point(...)`
* `contingent.run_contingent_election(...) -> ContingentResult`
* `calendar.ElectionCalendar.from_config()` → `.cycle(year)`, `.election_date(year)`, Senate class rotation
* `recount.needs_recount(...)`, `recount.perform_recount(...) -> RecountOutcome`
* `seats.chamber_composition(...)`, `seats.dhondt(...)`, `seats.council_size(population)`

### reporting
* `timeline.generate_timeline(frame, ballots_per_unit, config, seed) -> Timeline`
* `calling.RaceCaller(config).evaluate(progress, seed, seq) -> CallDecision`
* `live.NightEngine(frame, timeline, races, ev_by_race, config, seed)` → `.advance_to(seq)`, `.snapshot()`
* `live.PlaybackClock` — start / pause / resume / speed / step / finish; deterministic content

### forecasting
* `montecarlo.run_forecast(model, races, n_sims, seed, polls=None, workers=0) -> ForecastResult`

### polling / campaigns
* `polling.aggregate.aggregate_polls(polls, results, config, as_of) -> PollAverage`
* `polling.generate.generate_polls(...)`
* `campaigns.engine.plan_campaign(...)`, `campaigns.engine.realize_effects(...)`

### analytics / export
Pure functions over the **standard results frame** (below); exporters with stable schemas.

## 7. Standard results frame (analytics & export contract)

Services produce results as a `pandas.DataFrame` with these columns (one row per race × geo × line):

| column | type | notes |
|---|---|---|
| `election_id` | int | |
| `year` | int | |
| `race_code` | str | e.g. `PRES-NB`, `HOUSE-NB-07` |
| `race_type` | str | `RaceType` value |
| `level` | str | `unit`, `municipality`, `district`, `province`, `national` |
| `geo_code` | str | CBS code / district code / province code / `NL` |
| `geo_name` | str | |
| `province_code` | str | null for national |
| `line_key` | str | stable key of the ballot line within the race |
| `candidate` | str | ballot name |
| `party_code` | str | null for independents |
| `votes` | int | |
| `share` | float | 0–1 of valid votes at that level |
| `valid_votes` | int | at that level |
| `eligible` | int | at that level |
| `ballots_cast` | int | at that level |
| `winner` | bool | line won at that level (plurality) |

## 8. Conventions

* Python ≥ 3.11, type hints everywhere, `ruff` clean, docstrings on public functions.
* Tests live in `tests/<package>/test_*.py`; unit tests use the `synthetic` fixture (offline, fast);
  tests needing the real CBS store use the `real_frame` fixture and `@pytest.mark.realdata`.
* Logging via `app.core.logging.get_logger(__name__)`; long steps wrapped in `Timer`.
* Config documents in `config/*.yaml`, each validated by a Pydantic schema owned by its subsystem.
