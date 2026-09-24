# HTTP API

The FastAPI backend serves JSON under `/api` and the single-page UI under `/`.

```bash
uvicorn app.api.main:app            # or: python -m app run   (default http://127.0.0.1:8000)
```

* Interactive OpenAPI docs: `/api/docs` (Swagger), `/api/redoc`; schema at `/api/openapi.json`.
* Factory: `app.api.main.create_app(db_url=None, *, ui_dir=None, user_scenarios_dir=None,
  cors_origins=None, configure_logs=True)`.
* The API performs **no election mathematics**: every payload comes from the read models in
  `app.services.read.*`, the write actions in `app.services.read.actions`, the forecast runner
  (`app.services.forecast_runner`) and the night service (`app.services.night`).

This document is the contract the UI is built against. Examples are real responses, trimmed
(`"… 5 more"` marks a shortened list, `…` a shortened object).

---

## 1. Conventions

### 1.1 Transport

* JSON rendered with `orjson`: `NaN` / missing values are `null`; dates `YYYY-MM-DD`, datetimes
  ISO-8601 (simulated local time for election-night clocks).
* Responses above 1 KB are gzip-compressed when the client sends `Accept-Encoding: gzip`
  (Server-Sent Events are never compressed).
* CORS is **off** unless origins are configured (`create_app(cors_origins=[…])` or
  `NLFED_CORS_ORIGINS=http://a,http://b`).

### 1.2 Errors

Every error is `{"detail": "<message>", "code": "<code>"}` (request validation adds `errors`,
a list of `{loc, msg, type}`; constitutional validation adds `problems`).

| status | `code` | when |
|---|---|---|
| 404 | `not_found` | unknown election, province, municipality, district, race, candidate, scenario, dataset, job, run, **or unknown `/api/...` path** |
| 409 | `conflict` | state conflicts: results of an unreported election requested from an export, finalizing a finalized / live election, re-simulating a live / final election, night actions on a final night, editing a built-in scenario, duplicate scenario slug |
| 422 | `invalid` | bad parameters or bodies (unknown race family, sort field, poll type, malformed scenario, …) |
| 503 | `not_prepared` | the database or the geography has not been set up (`python -m app setup`) — `GET /api/health` still answers |
| 503 | `unavailable` | an optional service (the election-night service) is not installed |
| 500 | `internal` | unexpected server error (logged) |

### 1.3 Identifiers

* Geography uses **codes**, never database ids: provinces `NB`, municipalities `GM0855`,
  neighbourhoods (precincts) `BU08550101`, House districts `NB-07`.
* Races: `PRES` (national parent), `PRES-NB` (province EV contest), `HOUSE-NB-07`, `SEN-NB-1`,
  `GOV-NB`, `PROVLEG-NB`, `MAYOR-GM0855`, `COUNCIL-GM0855`. Codes are case-insensitive in paths.
* Elections are numeric ids; **`latest`** (the most recent election by date) and **`demo`**
  (`app_meta.demo_election_id` when set, else the most recent election not reported yet, else
  `latest`) are accepted wherever `{election_id}` appears.
* Ballot lines are identified by their **line key** (a candidate slug such as
  `lotte-van-der-ploeg`, or a party code for party-list races). Parties by **party code**;
  lines without a party (independents) count under the party key `independent`.

### 1.4 Units

`pct` fields are percentages 0–100, `share` fields fractions 0–1, margins (`margin_pp`) in
percentage points of the valid votes (winner − runner-up), turnout in % of eligible voters.

### 1.5 Provenance

Every payload carries `data_category` (`REAL` | `DERIVED` | `FICTIONAL` | `SIMULATED`) and,
where it mixes categories, a `provenance` map (field → category). The legend is in
`GET /api/meta` (`data_categories`). Geography is REAL (CBS/PDOK) or DERIVED; the constitution,
districts, parties and candidates are FICTIONAL; every vote, call, poll and forecast is SIMULATED.

### 1.6 The hidden-until-reported rule (`results_source`)

An election has a complete stored result as soon as it is *simulated*, but it is revealed only
progressively during its election night and completely once it is FINAL. Every election-scoped
payload states where its results come from:

| `results_source` | election status | results shown |
|---|---|---|
| `final` | `final`, `certified` | the stored results |
| `live` | `live` (a night is in progress) | only the night service's **counted** votes, calls and projections |
| `hidden` | `scheduled`, `simulated` | none: ballots, candidates, incumbents and geography only; every vote / pct / margin / winner / turnout field is `null`, statuses are `SCHEDULED`, and a `notice` explains why |

Hidden elections also refuse result exports (409), hide the reporting timeline (only
`total_events`), campaign *realised* effects and poll accuracy. Polls (generated from the
pre-election expectation) and forecasts (model estimates) are always available.

### 1.7 The election envelope

Every `/api/elections/{id}/…` payload (and polls, campaigns, analytics, forecast runs lists)
starts with:

```json
{
  "election": {"id": 3, "year": 2028, "name": "General Election 2028", "election_type": "general",
               "status": "final", "election_date": "2028-11-07", "previous_election_id": 2,
               "reported": true, "live": false, "results_source": "final"},
  "data_category": "SIMULATED",
  "results_source": "final",
  "notice": "Results of this election are not revealed yet: …"   // hidden only
}
```

### 1.8 Caching

Payloads of reported and hidden elections are cached server-side (keyed by election id **and**
status / finalisation time, cleared after every write action); live payloads are never cached.
GeoJSON layers carry `ETag` + `Cache-Control: public, max-age=3600, must-revalidate`
(`If-None-Match` → `304`).

---

## 2. Common objects

### 2.1 Person (FICTIONAL)

```json
{"id": 3705, "key": "lotte-van-der-ploeg", "name": "Lotte van der Ploeg", "first_name": "Lotte",
 "last_name": "van der Ploeg", "gender": "F", "portrait_key": "avatar-23", "home_municipality": "GM0363"}
```

`portrait_key` names a deterministic placeholder avatar (`avatar-01` … `avatar-32`).

### 2.2 Line (full ballot line)

```json
{"key": "lotte-van-der-ploeg", "ballot_candidate_id": 6383, "order": 2,
 "name": "Lotte van der Ploeg / Anil Jagesar",
 "candidate": {Person}, "running_mate": {Person} | null,
 "party": "PA", "party_name": "Progressieve Alliantie", "party_abbr": "PA", "color": "#2E9E54",
 "incumbent": false, "withdrawn": false, "write_in": false,
 "votes": 3447392 | null, "pct": 32.327 | null, "winner": true}
```

The party identity (`party*`, `color`) is the one printed on that ballot (parties can be renamed
or recoloured later). Presidential **tickets** replace `candidate` by `president`.

### 2.3 CompactLine (lists of many races)

```json
{"key": "erik-pijnenburg-tyoz", "name": "Erik Pijnenburg", "party": "VLP", "color": "#1F5AA6",
 "candidate_id": 251, "portrait_key": "avatar-28", "running_mate_name": "Floris Lemmens",
 "incumbent": true, "votes": 571224, "pct": 34.928, "winner": false}
```

### 2.4 Race (the common race view)

```json
{
  "code": "GOV-NB", "name": "Governor of Noord-Brabant", "type": "GOVERNOR",
  "province_code": "NB", "district_code": null, "district_name": null,
  "municipality_code": null, "municipality_name": null,
  "electoral_votes": null, "seats": 1, "electoral_system": "fptp", "is_special": false,
  "open_seat": false,
  "incumbent": {"candidate_id": 251, "name": "Erik Pijnenburg", "portrait_key": "avatar-28",
                "party": "VLP", "running": true},
  "previous_party": "VLP",
  "status": "FINAL",
  "leader": "noor-van-hout-l674", "runner_up": "erik-pijnenburg-tyoz", "winner": "noor-van-hout-l674",
  "leader_party": "PA", "leader_name": "Noor van Hout", "leader_color": "#2E9E54",
  "winner_party": "PA", "winner_name": "Noor van Hout", "winner_color": "#2E9E54",
  "margin_pp": 0.2322, "margin_votes": 3798, "total_votes": 1635445,
  "turnout_pct": 81.155, "ballots_cast": 1648230, "eligible": 2030957,
  "reporting_pct": 100.0, "win_probability": null,
  "flip_status": "flip", "decided_by": "recount", "called_at": null,
  "lines": [Line | CompactLine, …]
}
```

* `status`: a `RaceStatus` (`SCHEDULED`, `POLLS_CLOSED`, `TOO_EARLY_TO_CALL`, `TOO_CLOSE_TO_CALL`,
  `LEAN`, `PROJECTED_WINNER`, `CALLED`, `RECOUNT`, `FINAL`).
* `winner` is set when the race is decided (FINAL; live: `PROJECTED_WINNER` / `CALLED` / `FINAL`);
  `leader` is the counted leader.
* `previous_party`: the party that held the seat before (winner of the previous race for the
  same office, else the incumbent's party); `flip_status` ∈ `hold` | `flip` | `new` (no previous
  holder) | `null` (not decided).
* `decided_by` (FINAL): `popular_vote`, `recount`, `lot`, `electoral_college`, `contingent`, …
* Live only: `win_probability` (calling model, last evaluation), `lean`, `is_manual`.
* Multi-seat party-list races (`PROVLEG-*`, `COUNCIL-*`) add `seats_won: {party: seats}` (FINAL).

### 2.5 Municipality map row

```json
{"code": "GM0014", "name": "Groningen", "province_code": "GR", "population": 244420, "eligible": 186234,
 "votes": 147928, "ballots_cast": 149636, "turnout_pct": 80.348, "reporting_pct": 100.0,
 "leader": "PA", "leader_party": "PA", "leader_color": "#2E9E54", "runner_up": "VLP",
 "margin_pp": 40.8861,
 "shares": {"CVU": 7.425, "DM": 7.87, "NVB": 6.803, "PA": 55.34, "SAP": 8.109, "VLP": 14.454},
 "swing_pp": 1.843, "previous_winner": "PA", "flip_status": "hold", "outstanding_est": 0}
```

Keyed **by party** (`leader`, `runner_up`, `shares` keys are party codes). `swing_pp` is the
leader's share change (pp) against the previous reported election holding the same race family,
lineage-aware across municipal mergers; `previous_winner` / `flip_status` likewise. Live rows add
`units_total`, `units_reported`, `expected_ballots` and estimate `outstanding_est` from the
pre-election expectation (never the hidden result); hidden rows have `null` results.

---

## 3. System

### `GET /api/health`

Never fails; reports readiness without creating a missing SQLite file.

```json
{"status": "ok", "ready": true, "version": "1.0.0",
 "database": {"backend": "sqlite", "database": "nlfed.db", "exists": true, "schema": true,
              "revision": "d4bc2f767e86", "ready": true, "error": null},
 "geography": {"store_year": 2025, "store_prepared": true, "loaded": true, "vintage_year": 2025,
               "source": "store"},
 "elections": 3, "night_service": true, "ui_built": true}
```

`ready` = schema present and geography loaded; `geography.source` is `store` (REAL CBS store),
`synthetic` (test country) or `database`.

### `GET /api/meta`

Everything the UI shell needs at start-up.

```json
{
  "app": {"name": "NL Federal Election Simulator", "version": "1.0.0"},
  "notice": "FICTIONAL constitutional system over the REAL geography of the Netherlands …",
  "provenance": {"constitution": "FICTIONAL", "active.vintage": "REAL", "elections": "SIMULATED", …},
  "constitution": {
    "data_category": "FICTIONAL", "canonical": true, "provinces": 12,
    "electoral_votes": 174, "presidential_majority": 88, "house_seats": 150, "house_majority": 76,
    "senate_seats": 24, "senate_majority": 13, "senators_per_province": 2, "senate_classes": 3,
    "seats_per_senate_class": 8, "min_house_seats_per_province": 1,
    "terms": {"president": 4, "house": 2, "senate": 6, "governor": 4, "mayor": 4},
    "apportionment_method": "huntington_hill", "ev_allocation": "winner_take_all",
    "contingent_mode": "province_delegations", "lieutenant_governors": true,
    "labels": {"president": "88 TO WIN", "house": "76 FOR CONTROL", "senate": "13 FOR CONTROL"}
  },
  "active": {
    "vintage": {"id": 1, "year": 2025, "label": "CBS Wijk- en Buurtkaart 2025", "provinces": 12,
                "municipalities": 342, "units": 14729, "population": 18044120, "source": "store",
                "data_category": "REAL"},
    "apportionment": {"id": 1, "method": "huntington_hill", "total_seats": 150,
                      "total_electoral_votes": 174, "data_category": "FICTIONAL"},
    "plan": {"id": 1, "name": "House plan 2025 seed 2028", "seed": 2028, "method": "multires_bisection",
             "config_hash": "7959c87e67e77c9c", "total_districts": 150, "max_abs_deviation_pct": 1.9725,
             "data_category": "FICTIONAL"}
  },
  "elections": [{ElectionBrief}, "… 2 more"],
  "demo_election_id": 3, "latest_election_id": 3,
  "parties": [{"code": "PA", "name": "Progressieve Alliantie", "abbreviation": "PA", "color": "#2E9E54",
               "is_active": true}, "… 7 more"],
  "data_categories": [{"key": "REAL", "label": "Real", "description": "…"}, "… 3 more"],
  "race_statuses": ["SCHEDULED", "POLLS_CLOSED", "…"],
  "decided_statuses": ["CALLED", "FINAL", "PROJECTED_WINNER"],
  "night": {"available": true, "speeds": [1.0, 2.0, 5.0, 10.0, 25.0], "default_speed": 1.0}
}
```

`ElectionBrief` is the `election` block of §1.7.

### `GET /api/settings` · `PUT /api/settings`

UI settings persisted in `app_meta` (`ui_settings`). `PUT` merges a partial body:
`{theme?: "dark"|"light", playback_speed?: <one of night.speeds>, party_colors?: {CODE: "#rrggbb"},
map_metric?: "margin"|"share"|"turnout"|"swing"|"reporting", show_provenance_badges?: bool,
default_election_id?: int|null}` and returns the new settings (422 for unknown parties, bad
colours, speeds or elections).

```json
{"theme": "dark", "playback_speed": 1.0, "party_colors": {"PA": "#112233"}, "map_metric": "margin",
 "show_provenance_badges": true, "default_election_id": null,
 "defaults": {"theme": "dark", "playback_speed": 1.0, "party_colors": {}, …},
 "options": {"themes": ["dark", "light"], "speeds": [1.0, 2.0, 5.0, 10.0, 25.0],
             "map_metrics": ["margin", "share", "turnout", "swing", "reporting"],
             "parties": [{"code": "PA", "name": "Progressieve Alliantie", "color": "#2E9E54"}, …]},
 "provenance": {"party_colors": "FICTIONAL"}}
```

### `GET /api/data/provenance`

```json
{"legend": [...], "notice": "…",
 "real": {"data_category": "REAL", "attribution": "Centraal Bureau voor de Statistiek (CBS) / PDOK — CC BY 4.0",
          "vintage": {"year": 2025, "label": "…", "provinces": 12, "municipalities": 342, "units": 14729, …},
          "sources": [{"key": "wijkenbuurten", "name": "CBS Wijk- en Buurtkaart 2025 (GeoPackage)",
                       "publisher": "…", "url": "https://service.pdok.nl/…", "license": "CC BY 4.0",
                       "sha256": "11894edc…", "size_bytes": 219668480, "retrieved_at": "…", …}, …],
          "store": {"year": 2025, "built_at": "…", "fingerprint": "…", "counts": {…}, "imputation": {…},
                    "water_links": […], "sources": […]},
          "precinct_note": "CBS neighbourhoods (buurten) are used as precincts: …"},
 "derived": {"data_category": "DERIVED", "transforms": [{"name": "Estimated eligible voters", "definition": "…"}, …]},
 "fictional": {"data_category": "FICTIONAL", "constructs": [{"name": "Constitution", "where": "…", "description": "…"}, …],
               "counts": {"parties": 8, "candidates": 4712, "district_plans": 1, "scenarios": 3}},
 "simulated": {"data_category": "SIMULATED", "outputs": [...],
               "counts": {"elections": 3, "polls": 525, "simulation_runs": 14, "forecast_runs": 1}}}
```

### `GET /api/data/validation?elections=false`

`app.services.validation.validate_system` (constitutional arithmetic, apportionment, plan,
Senate classes, offices; with `elections=true` also the exact reconciliation of every stored
election — about 10 s on the real country). Cached per set of election states.

```json
{"ok": true, "errors": [], "warnings": [],
 "checks": [{"name": "electoral votes total", "ok": true, "detail": "174 EV (constitution 174, 88 to win)",
             "severity": "error"}, …],
 "data_category": "FICTIONAL", "elections_checked": false}
```

---

## 4. Geography (REAL) and electoral geography (FICTIONAL)

### `GET /api/provinces`

```json
{"data_category": "REAL",
 "provenance": {"population": "REAL", "eligible_voters_est": "DERIVED", "house_seats": "FICTIONAL",
                "electoral_votes": "FICTIONAL", "governor": "FICTIONAL", …},
 "vintage": {"id": 1, "year": 2025, "label": "CBS Wijk- en Buurtkaart 2025"}, "apportionment_id": 1,
 "totals": {"population": 18044120, "house_seats": 150, "electoral_votes": 174},
 "provinces": [
   {"code": "GR", "cbs_code": "PV20", "name": "Groningen", "name_en": "Groningen", "capital": "Groningen",
    "population": 596075, "population_official": 596075, "eligible_voters_est": 459331,
    "area_km2": 2960.03, "land_area_km2": 2316.2, "density": 257.3, "municipalities": 10,
    "unit_count": 590, "centroid": [6.73, 53.28], "house_seats": 5, "senators": 2, "electoral_votes": 7,
    "governor": {"office": "GOV-GR", "candidate_id": 1188, "name": "…", "portrait_key": "avatar-07",
                 "party": "PA", "term_start": "2029-01-01", "term_end": "2033-01-01", "start_reason": "elected"}},
   "… 11 more"]}
```

### `GET /api/provinces/{code}`

The province row plus `demographics` (population-weighted means of the municipal CBS indicators,
DERIVED), `lieutenant_governor`, `legislature` (`{name, seats, electoral_system}`), `districts`
(`[{code, name, population, deviation_pct, holder}]`), `senate_seats`
(`[{code, seat_number, senate_class, holder}]`) and `largest_municipalities`
(`[{code, name, population}]`, 10).

### `GET /api/municipalities?province=NB`

```json
{"data_category": "REAL", "provenance": {…}, "vintage": {"id": 1, "year": 2025}, "count": 56,
 "municipalities": [
   {"code": "GM0855", "name": "Tilburg", "province_code": "NB", "population": 229842,
    "population_official": 229836, "eligible_voters_est": 173590, "area_km2": 119.137,
    "land_area_km2": 117.26, "density": 1960.1, "urbanity_class": 2, "address_density": 1972.0,
    "unit_count": 143, "centroid": [5.063, 51.575],
    "demographics": {"pct_age_0_15": 14.0, "pct_age_15_25": 16.0, "pct_age_25_45": 29.0, "pct_age_45_65": 23.0,
                     "pct_age_65_plus": 17.0, "pct_single_households": 49.0, "pct_households_with_children": 25.0,
                     "avg_household_size": 1.9, "pct_origin_nl": 69.0, "pct_origin_europe": 9.0,
                     "pct_origin_non_europe": 22.0, "pct_education_low": 26.2, "pct_education_mid": 38.4,
                     "pct_education_high": 35.4, "income_per_capita_keur": 29.3, "pct_owner_occupied": 48.0},
    "imputed_fields": []},
   "… 55 more"]}
```

`imputed_fields` lists indicators that are DERIVED (imputed) rather than REAL.

### `GET /api/municipalities/{code}`

The row above plus `demographics_source_years`, `districts` (House districts overlapping:
`[{code, name, population, unit_count, share_of_municipality, share_of_district}]`), `units`
(every neighbourhood / precinct: `[{code, name, wijk_code, population, eligible_voters_est,
land_area_km2, density, urbanity_class, district_code, imputed_fields}]`), `mayor` (current
holder or `null`), `council` (`{name, seats, electoral_system}`) and `lineage` (merger records).

### `GET /api/apportionment`

```json
{"data_category": "FICTIONAL", "provenance": {"population": "REAL", "seats": "FICTIONAL", …},
 "id": 1, "method": "huntington_hill", "population_basis": "population", "total_seats": 150,
 "min_seats_per_province": 1, "senators_per_province": 2, "total_electoral_votes": 174,
 "total_population": 18044120, "average_persons_per_seat": 120294.1,
 "provinces": [{"code": "GR", "name": "Groningen", "population": 596075, "quota": 4.955127, "seats": 5,
                "senators": 2, "electoral_votes": 7, "persons_per_seat": 119215.0}, "… 11 more"],
 "priority_list": [{"rank": 13, "province": "ZH", "province_seat": 2, "priority": 2808216.451}, "… 137 more"],
 "first_out": [{"rank": 151, "province": "NH", "province_seat": 24, "priority": 122447.9}, "… 4 more"],
 "method_comparison": [{"province": "GR", "population": 596075, "huntington_hill": 5, "hamilton": 5,
                        "webster": 5, "jefferson": 5, "adams": 5}, "… 11 more"]}
```

### `GET /api/districts?province=&plan=`

Districts of the active plan (or `plan`):

```json
{"data_category": "FICTIONAL", "provenance": {"population": "REAL", "deviation_pct": "DERIVED", …},
 "plan": {"id": 1, "name": "House plan 2025 seed 2028", "seed": 2028, "is_active": true}, "count": 150,
 "districts": [
   {"code": "GR-01", "number": 1, "name": "Groningen-Centrum", "province_code": "GR", "population": 119580,
    "eligible_voters_est": 92911, "target_population": 119215.0, "deviation_pct": 0.306, "area_km2": 38.2,
    "polsby_popper": 0.3151, "reock": 0.4012, "convex_hull_ratio": 0.7831, "unit_count": 96,
    "municipality_count": 1, "split_municipalities": 1, "urban_share": 0.9841, "rural_share": 0.0031,
    "is_contiguous": true, "components": 1, "centroid": [6.566, 53.219],
    "holder": {"office": "HOUSE-GR-01", "candidate_id": 812, "name": "…", "party": "PA", …}},
   "… 149 more"]}
```

### `GET /api/districts/plan?plan=`

Plan metadata and audit: `id, name, chamber, year, vintage_id, apportionment_id, seed, method,
config_hash, config` (generation config JSON), `is_active, created_at, generation_seconds,
overrides_applied, notes, generation_run` (`{id, seed, config_hash, duration_s, summary}`),
`validation` (`{ok, problems, total_districts, noncontiguous_districts, split_municipalities,
warnings}`), `deviation` (`{max_abs_pct, mean_abs_pct, min_pct, max_pct, stdev_pct}`),
`compactness` (`{mean_polsby_popper, min_polsby_popper}`) and `provinces`
(`[{code, name, districts, population, target_population, max_abs_deviation_pct,
split_municipalities}]`).

### `GET /api/districts/{code}`

The district row plus `province_name`, `municipalities` (fragments: `[{code, name, population,
unit_count, share_of_municipality, share_of_district}]`), `neighbours`
(`[{code, name, shared_border_km, via_water_link}]`), `plan` and `history` — the reported House
results of this district code:
`[{election_id, year, race_code, winner, winner_party, margin_pp, turnout_pct, flipped,
incumbent_party, open_seat, district_plan_id, data_category}]`.

### `GET /api/senate/seats`

```json
{"data_category": "FICTIONAL", "provenance": {"seats": "FICTIONAL", "holder": "SIMULATED"},
 "seats_total": 24, "majority": 13, "label": "13 FOR CONTROL", "classes": {"1": 8, "2": 8, "3": 8},
 "as_of_year": 2028, "composition": {"PA": 11, "VLP": 8, "NVB": 5},
 "seats": [{"code": "SEN-GR-1", "province_code": "GR", "province_name": "Groningen", "seat_number": 1,
            "senate_class": 1, "holder": {"office": "SEN-GR-1", "name": "…", "party": "PA", …},
            "next_election_year": 2032}, "… 23 more"]}
```

### `GET /api/geo/{layer}.geojson`

| layer | content |
|---|---|
| `provinces` | REAL province polygons (`code, name, province_code`) |
| `municipalities` | REAL municipality polygons (`code, name, province_code`) |
| `districts` | FICTIONAL House districts of the active plan (or `?plan=`): `code, name, number, province_code, population, deviation_pct` |
| `units/{PV}` | REAL neighbourhoods (precincts) of one province, e.g. `units/NB.geojson` |

EPSG:4326 FeatureCollections, coverage-simplified, 5-decimal coordinates. Provinces and
municipalities come from the processed store (`data/processed/geo_<year>/web/`), or from the
database geometry when the store has no web layer (synthetic test country). The districts layer
is generated from `house_district.geometry_wkb` and cached as
`data/processed/geo_<year>/web/districts_<plan_id>.geojson` (the header records a plan
fingerprint; a changed plan regenerates it). Unit layers need the processed store (503
`not_prepared` otherwise). Responses have `ETag` / `Cache-Control`; `If-None-Match` → 304.

---

## 5. Elections

### `GET /api/elections`

```json
{"data_category": "SIMULATED", "count": 3, "demo_election_id": 3, "latest_election_id": 3,
 "elections": [
   {"id": 1, "year": 2024, "name": "Founding General Election 2024", "election_type": "general",
    "status": "final", "election_date": "2024-11-05", "previous_election_id": null, "reported": true,
    "live": false, "results_source": "final", "seed": 20240001,
    "scenario": {"id": 1, "slug": "founding-2024", "name": "…", "hash": "…"},
    "contents": {"president": true, "house": true, "senate_classes": [1, 2, 3], "governors": true,
                 "provincial_legislatures": true, "municipal": false,
                 "race_counts": {"GOVERNOR": 12, "HOUSE": 150, "PRESIDENT": 1, "PRESIDENT_PROVINCE": 12,
                                 "PROVINCIAL_LEGISLATURE": 12, "SENATE": 24}, "founding": true},
    "headline": {"president": {"winner": "charlotte-verbeek", "winner_name": "Charlotte Verbeek / Joost Brinkman",
                               "party": "VLP", "electoral_votes": 111, "decided_by": "electoral_college"},
                 "house_control": null, "senate_control": "VLP", "turnout_pct": 81.045},
    "data_category": "SIMULATED"},
   "… 2 more"]}
```

`headline` is `null` until the election is reported.

### `POST /api/elections` → 201

Body `{"scenario": "demo-2028", "seed"?: int, "year"?: int, "strict"?: bool, "simulate"?: bool}` —
create a SCHEDULED election from a built-in or user scenario (`strict` defaults to true on the
REAL geography, false on the synthetic test country; `simulate: true` also simulates it).
Returns the election detail (below). 404 unknown scenario, 422 invalid body, 409 calendar
conflicts (e.g. no election in that year, or an election on or after that date is already FINAL
— elections are certified in chronological order).

### `GET /api/elections/{id}`

The envelope plus `seed, scenario, contents, apportionment_id, district_plan_id, ballot_lines,
polls, campaigns, simulated_at, finalized_at, runs` (`{"election-setup"|"election"|"election-final":
{id, seed, duration_s}}`), `results` (reported only — President, EV, chambers, governors,
legislature seats, turnout, flips, recounts), `next_election_id`, `night`
(`{available, live, clock}`) and `constitution` (totals and majorities).

### `POST /api/elections/{id}/simulate`

Body (optional) `{"seed": int}`. Simulates (or re-simulates) the hidden result and the
election-night timeline; SIMULATED status. 409 for live / final elections.

### `POST /api/elections/{id}/finalize`

Instant finish without an election night (simulates first when needed); FINAL status. 409 when
already final or when the election is live (finish the night instead:
`POST /api/night/{id}/control {"action": "finish"}`).

---

## 6. Results pages

All take `{election_id}` (number, `latest`, `demo`) and return the envelope (§1.7).

### `GET /api/elections/{id}/president`

```json
{…envelope,
 "race_code": "PRES", "race_name": "President", "electoral_votes_total": 174, "majority": 88,
 "label": "88 TO WIN", "ev_allocation": "winner_take_all", "status": "FINAL",
 "winner": {"key": "lotte-van-der-ploeg", "name": "Lotte van der Ploeg / Anil Jagesar", "party": "PA",
            "color": "#2E9E54", "president": {Person}, "running_mate": {Person}, "electoral_votes": 144},
 "decided_by": "electoral_college",
 "tickets": [
   {"key": "lotte-van-der-ploeg", "ballot_candidate_id": 6383, "order": 2,
    "name": "Lotte van der Ploeg / Anil Jagesar", "president": {Person}, "running_mate": {Person},
    "party": "PA", "party_name": "Progressieve Alliantie", "party_abbr": "PA", "color": "#2E9E54",
    "incumbent": false, "withdrawn": false, "write_in": false,
    "votes": 3447392, "pct": 32.327, "electoral_votes": 144,
    "provinces_won": ["FL", "GE", "…"], "winner": true},
   "… 5 more"],
 "popular_vote": {"total_valid": 10664090, "ballots_cast": 10879753, "eligible": 13656831,
                  "turnout_pct": 79.665, "leader": "lotte-van-der-ploeg", "runner_up": "charlotte-verbeek",
                  "margin_votes": 507655, "margin_pp": 4.7604, "reporting_pct": 100.0},
 "electoral_vote_margin": 114, "majority_reached": true,
 "tipping_point": {"key": "lotte-van-der-ploeg", "province_code": "GE", "province_name": "Gelderland",
                   "margin_pp": 2.2267, "cumulative_ev": 88, "majority": 88, "national_margin_pp": 4.7604,
                   "ec_bias_pp": -2.5337,
                   "order": [{"province_code": "NH", "cumulative_ev": 27}, "… 11 more"]},
 "divergence": {"pv_leader": "lotte-van-der-ploeg", "pv_leader_share": 0.3233, "pv_margin_pp": 4.7604,
                "ev_leader": "lotte-van-der-ploeg", "ev_leader_votes": 144, "total_ev": 174, "majority": 88,
                "ev_majority": true, "diverged": false,
                "description": "lotte-van-der-ploeg leads both the electoral vote (144 EV) and the popular vote (by 4.76 pp)."},
 "contingent": null,
 "provinces": [
   {"code": "GR", "name": "Groningen", "race_code": "PRES-GR", "ev": 7, "status": "FINAL",
    "winner": "lotte-van-der-ploeg", "winner_party": "PA", "winner_color": "#2E9E54",
    "leader": "lotte-van-der-ploeg", "leader_party": "PA", "leader_color": "#2E9E54",
    "margin_pp": 11.8527, "turnout_pct": 79.985, "reporting_pct": 100.0, "win_probability": null,
    "pct": {"lotte-van-der-ploeg": 31.699, "ronald-kroon": 19.846, "…": "…"},
    "flip_status": "hold", "previous_party": "PA", "ev_awarded": {"lotte-van-der-ploeg": 7}},
   "… 11 more"]}
```

* Tickets are sorted by electoral votes, then votes. `provinces[].pct` is keyed by **line key**;
  `ev_awarded` shows split allocations under non-winner-take-all rules.
* `contingent` (when nobody reached 88): `{mode, outcome, rounds, finalists: [line keys], winner,
  vice_president: {candidate_id, name}, ballots: [per-round tallies]}`.
* **Live**: tickets carry the counted `votes` / `pct`, `projected_pct`, `electoral_votes` (= EV
  of decided provinces), `ev_leading`, `ev_max_possible`; plus `ev_decided_total`,
  `ev_uncalled`, `contingent_likely`, `seq`, `clock`; `popular_vote` =
  `{total_valid, projected_valid, outstanding_ballots_est, reporting_pct}`; `winner` once the
  night has projected 88 EV. `tipping_point` / `divergence` are `null` until FINAL.
* **Hidden**: `status: "SCHEDULED"`, tickets with `votes`/`pct`/`electoral_votes` `null`,
  `popular_vote: null`, provinces with `ev` only; nothing is allocated before the night:
  `ev_decided_total: 0`, `ev_uncalled: 174`, `majority_reached: false` (with `majority: 88`,
  `label: "88 TO WIN"`).

### `GET /api/elections/{id}/electoral-college`

```json
{…envelope, "electoral_votes_total": 174, "majority": 88, "label": "88 TO WIN", "status": "FINAL",
 "winner": {…as in /president},
 "tickets": [{"key": "lotte-van-der-ploeg", "name": "…", "party": "PA", "color": "#2E9E54",
              "electoral_votes": 144, "ev_share": 0.827586, "popular_votes": 3447392, "pv_share": 0.323271,
              "efficiency_pp": 50.4315, "provinces_won": ["FL", "GE", "…"]}, "… 5 more"],
 "path": [{"code": "NH", "name": "Noord-Holland", "ev": 27, "winner": "lotte-van-der-ploeg",
           "winner_party": "PA", "winner_color": "#2E9E54", "margin_pp": 17.4864, "cumulative_ev": 27,
           "is_tipping_point": false, "flip_status": "hold"}, "… 11 more"],
 "tipping_point": {…}, "closest": {"province_code": "ZE", "margin_pp": 0.1057},
 "largest_victory": {"province_code": "NH", "margin_pp": 17.4864},
 "divergence": {…}, "contingent": null, "provinces": [...as in /president]}
```

`path` orders the provinces by the winner's margin (the winner's path to 88); `efficiency_pp` =
EV share − PV share. Live / hidden: `tickets` with `electoral_votes`, `ev_leading`,
`ev_max_possible`, `popular_votes`; `ev_decided_total`, `ev_uncalled` (hidden: 0 and 174),
`contingent_likely`; `path`/`tipping_point` `null`.

### `GET /api/elections/{id}/provinces?race=PRES|GOV|SEN|PROVLEG`

One row per province of the family's province-wide contest (default `PRES`): the
`provinces[]` rows of `/president` (`ev` only for PRES; for SEN the first contested seat, with
`races` listing every seat race of the province). Also `race` and `colors` (party → colour).
422 for HOUSE / MAYOR / COUNCIL or unknown families; 404 when the election has no such races.

### `GET /api/elections/{id}/provinces/{code}?sort=&order=`

The province page.

```json
{…envelope,
 "province": {"code": "NB", "name": "Noord-Brabant", "electoral_votes": 24},
 "race": "PRES", "colors": {"PA": "#2E9E54", "…": "…", "independent": "#8A8A8A"},
 "headline": {Race with full lines — PRES-NB, else GOV-NB / the first SEN race in a midterm},
 "previous": {"election_id": 1, "year": 2024, "race_code": "PRES-NB", "winner": "hendrik-wolters",
              "winner_name": "Hendrik Wolters / Marieke Pijnenburg", "winner_party": "CVU",
              "margin_pp": 2.0915, "turnout_pct": 82.473,
              "pct_by_party": {"VLP": 26.164, "PA": 16.516, "…": "…"}},
 "swing": {"CVU": -2.339, "DM": -1.223, "NVB": -4.972, "PA": 9.611, "SAP": 0.694, "VLP": -1.77},
 "municipalities": {"sort": "population", "order": "desc",
                    "sort_fields": ["name", "population", "votes", "reporting_pct", "margin_pp", "swing_pp",
                                    "turnout_pct", "outstanding_est"],
                    "rows": [{Municipality map row}, "… 55 more"]},
 "largest_remaining": [],
 "outstanding": {"votes_est": 0, "reporting_pct": 100.0},
 "races": {"house": [Race with the top 2 CompactLines, …], "senate": [Race (compact lines), …],
           "governor": Race | null, "legislature": Race | null}}
```

* `sort` ∈ `sort_fields` (default `population`), `order` ∈ `asc`/`desc` (422 otherwise).
* `swing` is the province-level swing (pp) against the previous reported election of the family.
* **Live**: municipality rows are the night's counted rows; `largest_remaining` lists the 10
  municipalities with most outstanding (expected, uncounted) ballots; `outstanding` =
  `{votes_est, expected_ballots, reporting_pct, basis}`.
* **Hidden**: rows without results; `previous`/`swing` `null`.

### `GET /api/elections/{id}/municipalities?race=PRES&province=`

Map / table rows (§2.5) for every municipality of the race's jurisdiction:
`{…envelope, "race": "PRES", "keyed_by": "party", "colors": {...}, "count": 342, "municipalities": [...]}`.

`race` is a family — `PRES` (default), `HOUSE`, `SEN`, `GOV`, `PROVLEG`, `MAYOR`, `COUNCIL`,
pooled per municipality (a municipality split between House districts sums its fragments) —
or a single race code (`HOUSE-NB-07`: that race only). Live rows of a pooled family come from
one night-service call at a single `seq` (`NightManager.municipality_rows_many`); `units_total` /
`units_reported` are summed over the fragments.

### `GET /api/elections/{id}/municipalities/{code}?race=`

The municipality page: every race touching the municipality with its municipal result, and the
precinct (neighbourhood) table of `race` (default: the first race, usually `PRES`).

```json
{…envelope,
 "municipality": {"code": "GM0855", "name": "Tilburg", "province_code": "NB", "province_name": "Noord-Brabant",
                  "population": 229842, "eligible_voters_est": 173590, "reporting_pct": 100.0,
                  "units_total": 143, "units_reported": null},
 "provenance": {"municipality": "REAL", "results": "SIMULATED"},
 "races": [{Race without lines,
            "municipal": {"lines": [CompactLine…], "leader": "lotte-van-der-ploeg", "leader_party": "PA",
                          "margin_pp": 12.31, "total_votes": 131250, "turnout_pct": 76.2,
                          "reporting_pct": 100.0}}, "…"],
 "precinct_race": "PRES",
 "precincts": [{"code": "BU08550000", "name": "Binnenstad", "province_code": "NB", "votes": {"lotte-van-der-ploeg": 612, "…": "…"},
                "pct": {"lotte-van-der-ploeg": 38.2, "…": "…"}, "total_votes": 1602, "eligible": 2208,
                "ballots_cast": 1631, "turnout_pct": 73.868, "leader": "lotte-van-der-ploeg",
                "leader_party": "PA", "leader_color": "#2E9E54", "runner_up": "…", "margin_pp": 17.1,
                "margin_votes": 274}, "…"],
 "precincts_available": true,
 "events": null}
```

Races are ordered PRES, PRES-<PV>, House fragment(s), Senate, governor, legislature, mayor,
council. Council / legislature races add `municipal.seats_won`. **Live**: `municipal` holds the
counted votes, `events` lists the reporting batches so far, precincts are not available.
**Hidden**: results `null`, no precincts.

### `GET /api/elections/{id}/house`

```json
{…envelope, "seats_total": 150, "majority": 76, "label": "76 FOR CONTROL", "races": 150, "called": 150,
 "status_counts": {"FINAL": 150},
 "by_party": [{"party": "PA", "color": "#2E9E54", "won": 67, "called": 67, "leading": 0, "total": 67,
               "previous": 47, "net_change": 20, "votes": 3403976, "vote_pct": 31.026}, "… 7 more"],
 "previous_composition": {"VLP": 55, "PA": 47, "NVB": 31, "CVU": 9, "PLB": 8}, "previous_vacant": 0,
 "control": {"controlling_party": null, "largest_party": "PA", "largest_seats": 67, "seats_short": 9,
             "coalition_hints": [["PA", "VLP"], "…"], "label": "No majority — PA largest (67, 9 short of 76)"},
 "flips": 38,
 "districts": [
   {"code": "DR-01", "name": "Assen e.o.", "race_code": "HOUSE-DR-01", "province_code": "DR",
    "district_code": "DR-01", "municipality_code": null, "status": "FINAL",
    "winner": "ingrid-oosterhuis-5lln", "winner_name": "Ingrid Oosterhuis", "winner_party": "PA",
    "winner_color": "#2E9E54", "leader": "ingrid-oosterhuis-5lln", "leader_name": "Ingrid Oosterhuis",
    "leader_party": "PA", "leader_color": "#2E9E54", "margin_pp": 7.4601, "reporting_pct": 100.0,
    "turnout_pct": 84.356, "win_probability": null,
    "incumbent": {"name": "Jurgen van der Laan", "party": "VLP", "running": true},
    "open_seat": false, "previous_party": "VLP", "flip_status": "flip",
    "top": [{"key": "ingrid-oosterhuis-5lln", "name": "Ingrid Oosterhuis", "party": "PA", "color": "#2E9E54",
             "votes": 32818, "pct": 41.057, "winner": true, "incumbent": false}, "… 2 more"]},
   "… 149 more"]}
```

* `previous` = seats held going into the election (the incumbents' parties; `previous_vacant`
  counts seats without a previous holder); `net_change` = `total − previous`.
* **Live**: `called` / `leading` / `total` per party from the night; `control` =
  `{controlling_party, control_at_seq}` (the party whose *called* seats reach 76).
* **Hidden**: seat counts `null`, `previous` only; district rows without results.

### `GET /api/elections/{id}/house/{district}`

The race detail (below) of `HOUSE-<district>` plus `district` (`{code, name, number, population,
eligible_voters_est, deviation_pct, area_km2, urban_share, municipality_count, unit_count,
data_category}`) and `history` (reported House results of the district code, as in
`/api/districts/{code}`).

### `GET /api/elections/{id}/senate`

```json
{…envelope, "seats_total": 24, "majority": 13, "label": "13 FOR CONTROL", "up": 8, "not_up": 16,
 "classes_up": [2],
 "by_party": [{"party": "VLP", "color": "#1F5AA6", "holdover": 9, "won": 4, "called": 4, "leading": 0,
               "total_decided": 13, "total_projected": 13, "current": 15}, "… 2 more"],
 "composition": {"current": {"VLP": 15, "NVB": 6, "PA": 3}, "projected": {"VLP": 13, "PA": 6, "NVB": 5}},
 "control": {"controlling_party": "VLP", "largest_party": "VLP", "largest_seats": 13, "seats_short": 0,
             "coalition_hints": [["VLP"]], "label": "VLP control (13/24)"},
 "flips": 4,
 "seats": [
   {"code": "SEN-GR-1", "province_code": "GR", "province_name": "Groningen", "seat_number": 1,
    "senate_class": 1, "up": false,
    "holder": {"candidate_id": 344, "name": "Gerda Mulder", "portrait_key": "avatar-05", "party": "NVB",
               "term_start": "2025-01-15", "term_end": "2027-01-15"},
    "party": "NVB", "race": null},
   {"code": "SEN-FR-1", "…": "…", "up": true, "is_special": false, "holder": {incumbent}, "party": "VLP",
    "race": {Race with CompactLines}, "projected_winner": "…", "projected_party": "PA", "flip_status": "flip"},
   "… 22 more"]}
```

`current` is the composition going into the election (holdovers + the previous holders of the
seats up); `projected` = holdovers + winners (live: + called; hidden: `null`).

### `GET /api/elections/{id}/governors`

`{…envelope, "races": 12, "colors": {...}, "by_party": {"won": {"PA": 5, …} | null, "previous": {...}},
"flips": 3, "governors": [Race with CompactLines (running_mate_name = lieutenant governor), …]}`

### `GET /api/elections/{id}/mayors?province=`

`{…envelope, "races": 342, "by_party": {"won": {...} | null}, "flips": 57, "mayors": [row, …]}` —
rows as the House district rows with `code` = municipality code and `name` = municipality
name, `top` = best 2 lines. Empty when the election holds no municipal races.

### `GET /api/elections/{id}/races/{race}`

Any race.

```json
{…envelope,
 "race": {Race with full Lines},
 "calls": [{call row with "evidence"}],
 "recounts": [{"id": 54, "reason": "automatic_threshold", "threshold_pct": 0.25,
               "margin_before_votes": 3806, "margin_before_pct": 0.2327, "margin_after_votes": 3798,
               "margin_after_pct": 0.2322, "status": "completed", "outcome_changed": false,
               "seed": 2571958392611399747, "adjustments": 150,
               "net_change": {"noor-van-hout-l674": -2, "invalid": 21, "…": "…"},
               "audit": [{"line_key": "youssef-jagesar-y1sr", "pile": "line", "votes_before": 38,
                          "votes_after": 35, "delta": -3, "reason": "misread tally"}, "… (≤ 200)"]}],
 "municipalities": [{"code": "GM0743", "name": "Asten", "municipality_code": "GM0743", "province_code": "NB",
                     "votes": {line: votes}, "pct": {line: pct}, "total_votes": 10832, "eligible": 13322,
                     "ballots_cast": 10934, "turnout_pct": 82.075, "leader": "robin-van-es-uv4s",
                     "leader_party": "CVU", "runner_up": "…", "margin_pp": 3.2589, "margin_votes": 353}, "…"],
 "previous_race": {"election_id": 1, "year": 2024, "code": "GOV-NB", "winner": "erik-pijnenburg-tyoz",
                   "winner_name": "Erik Pijnenburg", "winner_party": "VLP", "margin_pp": 2.6591, "turnout_pct": 82.473}}
```

`PRES` adds `electoral_votes` (line → EV), `contingent` and `provinces` (as in `/president`).
**Live**: `decision` (current call decision with evidence: counted, outstanding, projected
shares and quantiles, win probabilities), `calls` (history with evidence), `lead_changes`,
`municipalities` (counted per municipality: `{code, name, reporting_pct, votes}`); `PRES` adds
`president` (the night's EV state). **Hidden**: lines without votes, empty lists.

### `GET /api/elections/{id}/calls?race_type=&status=&include_superseded=true&evidence=false&limit=`

Chronological race-call log (every change of a race's published state):

```json
{…envelope, "count": 886,
 "calls": [{"id": 885, "race_code": "PROVLEG-DR", "race_type": "PROVINCIAL_LEGISLATURE",
            "race_name": "Provincial Legislature of Drenthe", "status": "FINAL", "line_key": "VLP",
            "candidate": "Vrije Liberale Partij", "party": "VLP", "color": "#1F5AA6", "seq": 1877,
            "sim_time_s": 30964.0, "called_at": "2028-11-08T05:36:04", "clock": "05:36",
            "reporting_pct": 100.0, "margin_pct": 3.2808, "win_probability": 1.0, "is_manual": false,
            "override_reason": null, "superseded": false}, "…"]}
```

`limit` keeps the most recent N; `evidence=true` adds each call's evidence object. Live: every
call the night has made up to its current `seq` (the night service stores them as they are made;
same row shape). Elections finalized without a night have no calls.

### `GET /api/elections/{id}/timeline?detail=summary|events&bucket_minutes=15&limit=500&offset=0`

```json
{…envelope, "total_events": 1877, "revealed_events": 1877, "clock": null, "total_ballots": 11124074,
 "bucket_minutes": 15,
 "first_report": {"seq": 1, "clock": "21:33"}, "last_report": {"seq": 1877, "clock": "05:36"},
 "buckets": [{"start_s": 1800.0, "clock": "21:33", "events": 22, "ballots": 46439,
              "cumulative_ballots": 46439, "cumulative_pct": 0.417, "municipalities_reporting": 20,
              "municipalities_complete": 2, "last_seq": 22}, "… 32 more"],
 "events": null}
```

`detail=events` pages through the batches instead: `events: [{seq, sim_time_s, clock, time,
municipality_code, municipality_name, province_code, kind, ballots, municipality_fraction_after,
cumulative_ballots, national_fraction}]`. Live: only events up to the night's current `seq`
(`clock` is the night clock). Hidden: `total_events` only.

---

## 7. Election night (SIMULATED)

Served by the night service (`app.services.night.NightManager`, one per database, bound to the
API's database). Starting a night makes the election LIVE; finishing it finalizes the election
(FINAL). 503 `unavailable` when the night service is not installed.

**Playback in the server.** While a night runs, a background thread of the night service (the
*driver*) applies the reporting events that are due, persists the race calls and certifies the
election after the last event — nights advance whether or not anyone is polling. Read requests
(`/state`, `/municipalities`, `/races/…`, the live results pages) do at most ~30 ms of engine
work, so they answer quickly at every speed. Real 2028 night, `GET /state` polled every 0.5 s
next to an SSE stream: 25× p50 ≈ 80 ms / p95 ≈ 120 ms; 10× (two pollers) p50 ≈ 25 ms / p95 ≈
100 ms; the one slow answer (≈ 0.5–0.7 s) is the certification after the last event. Under a
heavy mix in one server process (25×, three pollers, SSE and six live results pages per second)
p95 rises to ≈ 360 ms (CPU-bound, one process). If the engine is behind the clock, the state
shows the last event applied and `clock.lag_events` > 0. Control actions (`pause`, `step`,
`finish`, …) first apply every due event.

**Reported elections.** When a night ends, its final summary state is stored (`simulation_run`
of kind `night`) and `GET /state` of a FINAL election is served from it (≈ 15–35 ms) without
rebuilding the night; `detail=full`, `/municipalities` and `/races/…` rebuild the night on first
use (≈ 15–20 s on the real country; the replay reproduces the stored calls exactly). The final
state carries the **certified** outcome: races that ended the night in `RECOUNT` are resolved
(`status: "FINAL"`, `called_key` = certified winner, `decided_by`, `night_status: "RECOUNT"`),
the Electoral College tally is the certified allocation (all 174 EV decided, or the contingent
winner) and the House / Senate counters are the certified seats; `snapshot.certified =
{final, finalized_at, recounts: [race codes], note}`. Vote counts stay those of election night
(before recount corrections); the results pages carry the certified counts.

### `GET /api/night/{id}/state?detail=summary|full`

```json
{"election_id": 3, "year": 2028, "name": "General Election 2028", "election_status": "live",
 "clock": {"status": "running", "speed": 5.0, "speeds": [1.0, 2.0, 5.0, 10.0, 25.0], "seq": 412,
           "total_events": 2403, "sim_time_s": 7260.0, "clock": "23:01", "polls_close_local": "21:00",
           "base_rate": 60.0, "end_sim_time_s": 30815.0, "end_clock": "05:33", "next_event_in_s": 0.4,
           "lag_events": 0},
 "labels": {"president": "88 TO WIN", "house": "76 FOR CONTROL", "senate": "13 FOR CONTROL"},
 "snapshot": {NightEngine.snapshot — schema in app.reporting.live: seq, total_events, clock, status,
              reporting, popular_vote, president {tickets[ev_decided, ev_leading, …], winner, …},
              provinces, house {by_party, control, …}, senate, governors, races (compact),
              recent_calls, lead_changes; detail=full adds municipalities and race projections},
 "data_category": "SIMULATED"}
```

### `POST /api/night/{id}/control`

Body `{"action": "start"|"pause"|"resume"|"speed"|"step"|"finish"|"reset", "speed"?: number}`;
returns the new state. `step` reveals the next reporting event and pauses; `finish` applies every
remaining event and finalizes the election; `reset` returns to polls closing (refused for FINAL
elections). 409 for invalid transitions (e.g. `pause` before `start`, or starting the night of an
election that can no longer be certified because a later one is FINAL), 422 for unknown actions.

### `GET /api/night/{id}/stream`

`text/event-stream` (Server-Sent Events). First `retry: 3000`, then an unnamed `message` event
with **the same JSON as `GET /state` (summary)** immediately and whenever it changes (checked
every second while running, every 2 s otherwise; `id:` = seq), and `: heartbeat` comments every
15 s. Query `max_events` / `timeout_s` end the stream early (tools, tests). An `event: error`
carries `{detail, code}` if the night fails. Use `EventSource` and fall back to polling `/state`.

### `GET /api/night/{id}/municipalities?race=PRES`

`{"election_id", "race", "data_category", "count", "municipalities": [{code, name, province_code,
race_code, reporting_pct, units_total, units_reported, units_partial, counted_valid,
votes: {line: n}, shares: {line: share}, leader, leader_party, leader_label, leader_color,
margin_votes, margin_pct, expected_ballots, outstanding_ballots_est, previous_election_id,
previous_winner_party, previous_margin_pct, swing_pct, flipped, data_category}]}` — raw night rows
keyed by line (the election endpoints re-key them by party).

### `GET /api/night/{id}/municipalities/{code}` · `GET /api/night/{id}/races/{race}`

Night service municipality detail (reporting events so far, counted result of every race) and
race detail (meta, lines with counted votes, `state`, `decision` with evidence, `history`,
`lead_changes`, per-municipality counts).

### `POST /api/night/{id}/calls/{race}/override`

Body `{"status": "LEAN"|"PROJECTED_WINNER"|"CALLED"|"TOO_CLOSE_TO_CALL"|"TOO_EARLY_TO_CALL"|"POLLS_CLOSED"|"CLEAR",
"line_key"?: str, "reason": str}` — a producer's manual call (persisted as an `is_manual` race
call; `CLEAR` hands the race back to the model). Returns `{election_id, race_key, applied, record,
state, data_category}`. 409 before the night starts / after it is final, 404 unknown race, 422
empty reason.

---

## 8. History and analytics (reported elections only)

### `GET /api/history/summary`

```json
{"data_category": "SIMULATED", "count": 3,
 "elections": [{"election": {ElectionBrief},
                "president": {"winner": "charlotte-verbeek", "winner_name": "…", "party": "VLP",
                              "electoral_votes": {"charlotte-verbeek": 111, "…": "…"},
                              "decided_by": "electoral_college", "contingent": null,
                              "popular_vote": {"winner_pct": 26.096, "margin_pp": 3.7706, "leader": "charlotte-verbeek"}},
                "house": {"composition": {"VLP": 55, "…": "…"}, "controlling_party": null, "majority": 76},
                "senate": {"composition": {…}, "seats_contested": 24, "holdovers": 0, "controlling_party": "VLP",
                           "majority": 13},
                "governors": {"GR": "NVB", "…": "…"}, "legislature_seats": {"PROVINCIAL_LEGISLATURE": {…}},
                "turnout_pct": 81.045, "flips": 0}, "…"]}
```

### `GET /api/history/compare?a=&b=&race=PRES&level=municipality|district|province|national`

Lineage-aware comparison (`app.analytics.history.compare_elections`, family-pooled). Default:
the last two reported elections holding the family (`a` earlier, `b` later; one of them may be
given). 404 when fewer than two exist; 409 when one is not reported; 422 when `a` and `b` are the
same election.

```json
{"data_category": "SIMULATED", "a": {ElectionBrief}, "b": {ElectionBrief}, "race": "PRES",
 "level": "province", "lineage_applied": false,
 "national": [{"party": "CVU", "share_a": 0.167427, "share_b": 0.154189, "swing_pp": -1.3239}, "…"],
 "flip_counts": {"hold": 6, "flip": 6},
 "flips_summary": [{"party": "VLP", "wins_prev": 7, "wins_curr": 5, "gains": 1, "losses": 3, "holds": 4,
                    "net": -2}, "…"],
 "rows": [{"geo_code": "DR", "geo_name": "Drenthe", "province_code": "DR", "winner_a": "VLP",
           "winner_b": "VLP", "flip_status": "hold", "turnout_a": 0.850988, "turnout_b": 0.845241,
           "turnout_change_pp": -0.5747, "swing": {"PA": 8.6316, "VLP": -1.993, "…": "…"},
           "share_b": {"PA": 0.188923, "…": "…"}, "status": {"PA": "both", "…": "…"}}, "…"]}
```

### `GET /api/history/closest?n=10&race_type=` · `GET /api/history/landslides?n=10&race_type=`

`{"data_category", "kind", "count", "races": [{rank, election_id, year, race_code, race_type, level,
geo_code, geo_name, province_code, winner_candidate, winner_party, winner_share,
runner_up_candidate, runner_up_party, runner_up_share, margin_votes, margin_pp, valid_votes,
tied, contested}]}` — at each race's jurisdiction level; `race_type` is a `RaceType` value.

### `GET /api/history/divergence`

`{"data_category", "count", "elections": [{election_id, year, pv_leader, pv_leader_share,
pv_margin_pp, ev_leader, ev_leader_votes, ev_leader_pv_share, total_ev, majority, ev_majority,
diverged, description}]}`.

### `GET /api/history/municipality/{code}?race=PRES` · `GET /api/history/province/{code}?race=PRES`

```json
{"data_category": "SIMULATED", "level": "municipality", "code": "GM0855", "name": "Tilburg", "race": "PRES",
 "points": [{"election": {ElectionBrief}, "name": "Tilburg", "winner": "VLP", "margin_pp": 3.1,
             "turnout_pct": 77.4, "valid_votes": 128002,
             "shares": {"PA": 24.8, "VLP": 27.9, "…": "…"}, "national_shares": {…}, "lean_pp": {…}},
            {"…": "…", "change_pp": {"PA": 6.2, "…": "…"}, "flipped": true}]}
```

Older vintages are remapped onto the current municipality codes (`MunicipalityLineage`).

### `GET /api/history/district/{code}`

`{"data_category", "code", "note", "points": [{election_id, year, race_code, geo_code, geo_name,
province_code, winner_candidate, winner_party, winner_share, runner_up_candidate,
runner_up_party, margin_votes, margin_pp, valid_votes, turnout, previous_winner_party,
status ("first"|"hold"|"flip"|"undecided"), district_plan_id}]}`.

### `GET /api/analytics/{id}`

Reported elections: `available: true` and

| key | content |
|---|---|
| `lean` | `{level: "province", race: "PRES", pair: [a, b], provinces: [{code, name, lean_pp: {party: pp}, two_party_lean_pp, leans}]}` |
| `elasticity` | `{available, elections, provinces: [{code, name, elasticity: {party: β}, method, n_obs}]}` (needs ≥ 2 reported presidential elections) |
| `ec_efficiency` | `[{party, label, popular_votes, pv_share, electoral_votes, ev_share, provinces_won, efficiency_pp}]` |
| `tipping_point` | `{party, province_code, margin_pp, national_margin_pp, ec_bias_pp}` |
| `competitiveness` | `{by_race_type: [{race_type, n_contests, mean_competitiveness, median_margin_pp, mean_enc, n_tossup, n_lean, n_likely, n_safe}], most_competitive: [{race_code, race_type, geo_code, winner_party, runner_up_party, margin_pp, enc, competitiveness, rating}]}` |
| `efficiency_gap` | House, pairwise (positive favours `party_a`): `[{election_id, year, race_family, party_a, party_b, wasted_a, wasted_b, efficiency_gap, efficiency_gap_pair, …}]` |
| `seat_vote` | House per party: `[{party, votes, vote_share, seats, seat_share, seat_bonus_pp, waste_rate, efficiency_gap, votes_per_seat}]` |
| `notes` | reading guide |

Hidden / live elections: `{…envelope, "available": false}`.

---

## 9. Parties and candidates (FICTIONAL)

### `GET /api/parties`

```json
{"data_category": "FICTIONAL", "provenance": {"identity": "FICTIONAL", "record": "SIMULATED"},
 "parties": [{"code": "PLB", "name": "Plattelands- en Regiopartij", "abbreviation": "PRP", "color": "#D2A10F",
              "color_secondary": null, "family": "agrarian / rural", "description": "…",
              "ideology": {"economic": 0.1, "social": 0.4, "europe": -0.3},
              "founded_year": 2024, "dissolved_year": null, "successor": null, "is_active": true,
              "candidates": 512,
              "events": [{"year": 2027, "event_type": "renamed", "old_value": "…", "new_value": "…",
                          "related_party": null, "notes": "…"}],
              "record": [{"election_id": 1, "year": 2024, "president_pct": null, "electoral_votes": null,
                          "house_seats": 8, "senate_seats": 0, "governors": 0}, "…"]}, "…"]}
```

### `GET /api/candidates?search=&party=&office=&limit=50&offset=0`

`office` filters serving office holders by prefix (`PRES`, `VP`, `HOUSE`, `SEN`, `GOV`, `LTGOV`,
`MAYOR`). `{"data_category", "total", "limit", "offset", "candidates": [{Person, party, color,
birth_date, home_province, offices: [office codes currently held], races: n}]}`.

### `GET /api/candidates/{id}`

```json
{"data_category": "FICTIONAL", "provenance": {"profile": "FICTIONAL", "results": "SIMULATED"},
 …Person, "birth_date": "1971-03-02", "party": "VLP", "home_province": "GE",
 "ideology": {"economic": 0.4, "social": 0.1}, "quality": 0.8, "campaign_strength": 0.5,
 "fundraising": 1.3, "favorability": 4.0, "approval": null, "bio": "…",
 "affiliations": [{"party": "VLP", "from_year": 2024, "to_year": null}],
 "offices": [{"office": "PRES", "office_name": "President", "office_type": "PRESIDENT", "party": "VLP",
              "term_start": "2025-01-15", "term_end": "2029-01-15", "ended_on": "2029-01-15",
              "serving": false, "start_reason": "elected", "end_reason": "defeated", "elected_in": "PRES"}],
 "candidacies": [{"election": {ElectionBrief}, "race_code": "PRES", "race_name": "President",
                  "race_type": "PRESIDENT", "role": "candidate", "ballot_name": "…", "line_key": "…",
                  "party": "VLP", "color": "#1F5AA6", "incumbent": true, "running_mate": {Person},
                  "ticket_leader": null,
                  "result": {"votes": 2940437, "pct": 27.573, "won": false, "margin_pp": 4.7604} | null}],
 "summary": {"candidacies": 2, "wins": 1, "losses": 1, "offices_held": 1, "serving": []}}
```

`result` is `null` for elections that are not reported.

---

## 10. Polls (FICTIONAL pollsters, SIMULATED numbers)

### `GET /api/polls/{id}?type=&geo=&pollster=&limit=500&offset=0`

```json
{…envelope, "provenance": {"pollsters": "FICTIONAL", "polls": "SIMULATED"}, "total": 175,
 "limit": 500, "offset": 0, "colors": {"PA": "#2E9E54", "…": "…"},
 "groups": [{"poll_type": "national_president", "geo_code": "NL", "polls": 50,
             "first_end_date": "2028-07-12", "latest_end_date": "2028-11-06"}, "…"],
 "pollsters": [{"name": "Delta Meningsonderzoek", "rating": 1.2, "rating_label": "A", "method": "online",
                "house_effects": {"VLP": 0.5}, "data_category": "FICTIONAL"}, "…"],
 "polls": [{"id": 412, "pollster": "Delta Meningsonderzoek", "pollster_rating": 1.2,
            "poll_type": "national_president", "geo_code": "NL", "start_date": "2028-11-02",
            "end_date": "2028-11-06", "sample_size": 1450, "population": "LV", "method": "online",
            "margin_of_error": 2.6, "undecided_pct": 5.0,
            "results": {"VLP": 29.0, "PA": 29.0, "NVB": 16.0, "…": "…"}}, "…"]}
```

Types: `national_president`, `province_president`, `house_district`, `senate`, `governor`,
`generic_house`, `favorability`. `geo_code`: `NL`, a province, a district (`NB-07`) or a Senate seat
(`NB-1`). Results are percent of all respondents keyed by party code (else the option label).

### `GET /api/polls/{id}/average?type=&geo=NL&as_of=`

Weighted average of one poll group (default: `national_president`, else `generic_house`; as of
the day before the election): `{…envelope, "poll_type", "geo_code", "as_of", "colors",
"average": {…}}` where `average` is `PollAverage.to_dict()`:
`keys, mean, se, lower, upper, total_se` (percent, 90 % interval), `interval_level, trend_latest,
n_polls, effective_n, effective_polls, latest_poll_date, undecided_mean, undecided_mode,
house_effects: [{pollster, key, prior_pp, estimated_pp, n_polls}], weights: [per-poll weight table:
poll_id, pollster, end_date, age_days, recency, sample, rating, population, method, volume,
weight, …], trend: {dates, series: {key: [..]}, se: {key: [..]}}, data_category`, plus
`groups` and `accuracy` (reported elections: `{actual_pct, error_pp, mean_abs_error_pp}`;
otherwise `null`). 404 when the group has too few polls.

### `POST /api/polls/{id}` → 201

Add a manual FICTIONAL poll to an unreported election:
`{"pollster": "Peilpunt Test Research", "poll_type": "national_president", "geo_code": "NL",
"start_date": "2028-10-20", "end_date": "2028-10-24", "sample_size": 1500,
"results": {"PA": 30.0, "VLP": 28.0}, "population"?: "LV"|"RV"|"A", "method"?: "online",
"undecided_pct"?: 8.0, "margin_of_error"?: 2.5, "notes"?: str}` → `{data_category, id,
election_id, pollster, poll_type, geo_code, start_date, end_date, sample_size, results}`.
422 for unknown types, invalid percentages, `end_date` after election day or pollster names
resembling real polling organisations; 409 for reported elections.

---

## 11. Campaigns (FICTIONAL plans, SIMULATED effects)

### `GET /api/campaigns/{id}?party=&all_targets=false`

```json
{…envelope, "provenance": {"plans": "FICTIONAL", "effects": "SIMULATED"},
 "effects_revealed": true, "hidden_fields": [],
 "units": {"amount": "abstract resource units", "effects": "logit points of party utility"},
 "campaigns": [{"id": 12, "party": "PA", "party_name": "Progressieve Alliantie", "color": "#2E9E54",
                "ticket": "Lotte van der Ploeg / Anil Jagesar", "budget": 100.0, "strategy": "battleground",
                "seed": 7243…, "spend": 100.0, "fundraising_proceeds": 3.2, "allocations": 412,
                "targets_total": 58,
                "by_action": [{"action": "advertising", "amount": 41.2, "allocations": 120, "units": 0}, "…"],
                "by_week": [{"week": 1, "amount": 6.1}, "…"],
                "targets": [{"level": "province", "code": "GE", "name": "Gelderland", "amount": 12.4,
                             "expected_effect": 0.0231, "realized_effect": 0.0198, "turnout_effect": 0.004,
                             "actions": {"advertising": 5.2, "rally": 2.0, "…": "…"}}, "… (top 25)"]}, "…"]}
```

Realised effects (`realized_effect`, `turnout_effect`) are inputs of the hidden result: they are
`null` (and listed in `hidden_fields`) until the election is reported. Expected effects are
always shown.

### `POST /api/campaigns/{id}/plan`

What-if re-plan, **not stored**: body `{"seed"?: int, "budgets"?: {party: amount},
"strategies"?: {party: "balanced"|"battleground"|"base"|"expansion"}}` →
`{…envelope, "preview": true, "stored": false, "seed", "provenance", "note",
"campaigns": [{party, budget, strategy, spend, fundraising_proceeds, allocations, targets_total,
by_action, by_week, targets (top 25, expected effects only)}]}`. 422 for unknown parties.

---

## 12. Forecasts (SIMULATED model estimates — not predictions)

### `POST /api/forecast/{id}/run` → 202

Body `{"simulations": 10000, "seed"?: int, "workers"?: 0, "use_polls"?: true, "include_local"?: false}`
(seed default: derived from the election seed and the number of earlier runs; `workers` 0 = worker
processes for large runs, 1 = in-process; `include_local` adds mayors, councils and provincial
legislatures). Starts a background job (one at a time, FIFO) and returns its status:

```json
{"job_id": "cc0f1436c15b42b4", "election_id": 3, "simulations": 100000, "seed": 2028, "workers": 0,
 "use_polls": true, "include_local": false, "status": "queued", "done": 0, "total": 100000,
 "run_id": null, "error": null, "created_at": "…", "started_at": null, "finished_at": null,
 "duration_s": null, "progress": 0.0, "data_category": "SIMULATED",
 "status_url": "/api/forecast/jobs/cc0f1436c15b42b4"}
```

The run: engine inputs of the election → polling average of the stored polls as of the day before
the election (national presidential ballot, else the generic House ballot) → national logit shift
per party (`shift_from_poll_average`, prior SD = the scenario's `environment.shocks.national_sd`)
→ `app.forecasting.engine.run_forecast` → `store_forecast` (a `simulation_run` of kind `forecast`
with its seed). 100,000 draws of the real 2028 election take ≈ 22 s.

### `GET /api/forecast/jobs/{job_id}` · `GET /api/forecast/jobs?election=`

Job status (as above; `status` ∈ `queued` | `running` | `completed` | `failed`, `progress` 0–1,
`run_id` when completed, `error` when failed). Jobs live in the server process.

### `GET /api/forecast/{id}/latest?include=races,municipalities` · `GET /api/forecast/runs/{run_id}`

```json
{"data_category": "SIMULATED", "disclaimer": "SIMULATED model estimates of a FICTIONAL electoral system: …",
 "run": {"id": 11, "election_id": 3, "status": "completed", "seed": 2028, "n_simulations": 100000,
         "config_hash": "d541fe59e28e0262", "started_at": "…", "finished_at": "…", "duration_s": 21.085,
         "code_version": "1.0.0",
         "polls": {"used": true, "poll_type": "national_president", "geo_code": "NL", "as_of": "2028-11-06",
                   "n_polls": 50, "effective_n": 43738.6, "poll_mean_pct": {"VLP": 31.085, "…": "…"},
                   "model_expected_pct": {"VLP": 30.172, "…": "…"}, "model_prior_sd": 0.12,
                   "shift": {"VLP": 0.026786, "…": "…"}},
         "race_types": ["GOVERNOR", "HOUSE", "PRESIDENT", "PRESIDENT_PROVINCE", "SENATE"],
         "model_fingerprint": "958ca571d51b0415"},
 "election": {ElectionBrief}, "constitution": {"electoral_votes": 174, "presidential_majority": 88},
 "turnout": {"mean": 0.791341, "median": 0.791626, "p05": 0.769082, "p25": 0.782618, "p75": 0.800406, "p95": 0.81252},
 "president": {"method": "winner_take_all", "total_ev": 174, "majority": 88, "label": "88 TO WIN",
               "prob_contingent": 0.07805, "prob_ev_tie": 0.0, "prob_pv_ev_divergence": 0.19958,
               "tickets": [{"key": "charlotte-verbeek", "name": "Charlotte Verbeek / Joost Brinkman",
                            "party": "VLP", "color": "#1F5AA6", "prob_win": 0.63585, "prob_plurality": 0.68048,
                            "ev": {"mean": 82.07, "median": 98.0, "p05": 0.0, "p25": 44.0, "p75": 115.0, "p95": 129.0},
                            "ev_histogram": [0.10174, "… (P(EV = k), k = 0…174)"],
                            "pv_share": {"mean": …, "median": …, "p05": …, "p25": …, "p75": …, "p95": …},
                            "prob_pv_plurality": 0.61, "pv_histogram": [[0.2975, 0.0012], "…"],
                            "expected_pv_share": 0.3017}, "… 5 more"],
               "provinces": [{"code": "GR", "name": "Groningen", "ev": 7,
                              "win": {"lotte-van-der-ploeg": 0.99846, "…": "…"}, "favourite": "lotte-van-der-ploeg",
                              "share_mean": {"lotte-van-der-ploeg": 0.3223, "…": "…"}, "tipping_point": 0.00017}, "…"],
               "tipping_point": {"GE": 0.39454, "ZH": 0.31783, "…": "…"},
               "tipping_point_by_ticket": {"charlotte-verbeek": {"GE": 0.478, "…": "…"}, "…": "…"},
               "combinations": [{"rank": 1, "frequency": 0.1121,
                                 "winners": {"GR": "lotte-van-der-ploeg", "FR": "ronald-kroon", "…": "…"},
                                 "ev": {"charlotte-verbeek": 98.0, "lotte-van-der-ploeg": 46.0, "ronald-kroon": 30.0}},
                                "… (top 10)"]},
 "house": {"chamber": "house", "seats_total": 150, "seats_up": 150, "majority": 76, "prob_no_majority": 0.65954,
           "parties": [{"party": "VLP", "color": "#1F5AA6",
                        "seats": {"mean": 67.83, "median": 68.0, "p05": 44.0, "p25": 59.0, "p75": 77.0, "p95": 89.0},
                        "histogram": [0.0, "… (P(seats = k), k = 0…150)"], "prob_majority": 0.29384,
                        "prob_plurality": 0.7546, "holdover": 0}, "…"],
           "compositions": [],
           "races": [{"race_code": "HOUSE-DR-01", "win": {"PLB": 0.836, "VLP": 0.16379, "…": "…"},
                      "favourite": "PLB"}, "… 149 more"]},
 "senate": {…same shape; "seats_up": 8, parties[].holdover, compositions: [{"seats": {"PA": 11, "VLP": 6, "NVB": 7}, "frequency": 0.10459}, …]},
 "governors": {…same shape; majority null}}
```

`prob_win` of a ticket is the probability of an outright Electoral College majority (tickets'
`prob_win` + `prob_contingent` = 1). `include=races` adds `races` (every race's line summaries:
win probability, share mean / p05 / p50 / p95, mean votes, expected share);
`include=municipalities` adds `president.municipalities` (municipality → ticket →
`{mean, p05, p95, lead}`). 404 when the election has no forecast yet.

### `GET /api/forecast/{id}/runs`

`{…envelope, "runs": [{run_id, election_id, scenario_id, kind, status, seed, n_simulations,
config_hash, started_at, finished_at, duration_s, code_version, data_category, disclaimer}]}`
(newest first).

---

## 13. Scenarios (FICTIONAL political assumptions)

Built-in scenarios (`config/scenarios/*.yaml`) are read-only; user scenarios are saved to
`config/scenarios/user/<slug>.yaml` **and** the `scenario` table (updated in place unless an
election was created from it — then a variant row `<slug>--<hash8>` is added, so existing
elections keep their exact document).

| Method | Path | Body | Result |
|---|---|---|---|
| GET | `/api/scenarios` | | `{data_category, count, operations: [editor ops], scenarios: [{slug, name, year, election_type, seed, description, source: "builtin"|"user"|"database", read_only, file, valid, error, elections: [ids]}]}` |
| GET | `/api/scenarios/{slug}` | | `{data_category, slug, name, year, election_type, seed, source, read_only, file, yaml, document, hash, validation: {ok, problems, geography_checked}, database: {id, hash, updated_at} \| null, elections, operations}` |
| POST | `/api/scenarios` → 201 | `{yaml?: str, document?: object, slug?: str}` (exactly one of yaml / document) | the new scenario (as GET); 409 when the slug exists |
| PUT | `/api/scenarios/{slug}` | `{yaml?, document?, edits?: [{op, …}]}` | the updated scenario plus `changes`; 409 for built-in scenarios |
| POST | `/api/scenarios/{slug}/duplicate` → 201 | `{new_slug, seed?, name?}` | the copy (a user scenario) |
| GET | `/api/scenarios/{slug}/export` | | `application/x-yaml` attachment (fully merged document) |
| POST | `/api/scenarios/validate` | `{yaml?, document?, base?: slug, edits?}` | `{ok, schema_ok, errors, problems, geography_checked, changes, slug, document, yaml}` — never fails for invalid input |
| DELETE | `/api/scenarios/{slug}` | | `{data_category, slug, deleted: true}`; 409 for built-in or used scenarios |

Editor operations (`edits`): `set_national_environment`, `set_province_environment`,
`set_candidate_quality`, `set_party_base_share`, `set_party_field`, `add_ticket`, `remove_ticket`,
`withdraw_ticket`, `set_ev_allocation`, `set_seed`, `set_geo_seed`, `add_candidate`,
`remove_candidate`, `set` (`{"op": "set", "path": "environment.turnout_base", "value": 0.8}`) — see
`app.scenarios.editor`. `validation.problems` lists references that do not fit the loaded
geography (saving is still allowed; `POST /api/elections` with `strict` rejects them).

---

## 14. Export (stable schemas)

### `GET /api/export/schemas`

`{"formats": ["csv", "json"], "datasets": [{name, version, data_category, aliases, key,
fingerprint, description, requires_reported_election, columns: [{name, type, nullable,
description, data_category, decimals?, allowed?}]}]}` — the registry of `app.export.schemas`
(column lists are documented in docs/ANALYTICS.md §5.4).

### `GET /api/export/{id}/{dataset}.{csv|json}?race=&race_type=&level=&run_id=&as_of=`

| dataset (alias) | schema | notes |
|---|---|---|
| `national`, `provinces`, `municipalities`, `units`, `district_lines` | `*_results` | one row per race × geography × line; `race` / `race_type` filter (recommended for `units`) |
| `house`, `senate`, `governors` | `*_results` | race summaries with flip vs the previous reported election / incumbents |
| `electoral_votes` | `electoral_votes` | per province: EV, winner, margin, `decided_by` |
| `swing` | `swing` | vs the previous reported election of `race` (PRES, HOUSE, SEN, GOV) at `level` (default municipality), lineage-aware |
| `timeline` | `reporting_timeline` | every reporting batch |
| `calls` | `race_calls` | every stored race call |
| `montecarlo_summary`, `montecarlo_distribution` | same | latest forecast run (or `run_id`) |
| `polls`, `polling_averages` | same | `as_of` for the averages (default: day before the election) |
| `districts`, `apportionment` | same | the election's plan / apportionment (FICTIONAL) |

CSV: `text/csv` attachment, header in schema order, deterministic (`app.export.writers.to_csv_text`).
JSON: the envelope `{"schema", "schema_version", "data_category", "generated_at", "metadata":
{"election_id", "year", "election", "seed", …}, "columns": [...], "rows": [[…], …]}`. Result,
timeline and calls datasets need a reported election (409 otherwise); unknown datasets 404,
unknown formats 422.

---

## 15. Static UI

The UI (`src/app/ui/static`) is mounted at `/` after every API route. Unknown non-API paths
serve `index.html` (client-side routing); until the UI exists a placeholder page links to
`/api/docs`. Unknown `/api/...` paths always answer the JSON 404 of §1.2.

---

## 16. Endpoint index

| Method | Path | § |
|---|---|---|
| GET | `/api/health` · `/api/meta` · `/api/settings` · PUT `/api/settings` | 3 |
| GET | `/api/data/provenance` · `/api/data/validation` | 3 |
| GET | `/api/provinces` · `/api/provinces/{code}` · `/api/municipalities` · `/api/municipalities/{code}` · `/api/apportionment` | 4 |
| GET | `/api/districts` · `/api/districts/plan` · `/api/districts/{code}` · `/api/senate/seats` · `/api/geo/{layer}.geojson` | 4 |
| GET/POST | `/api/elections` · GET `/api/elections/{id}` · POST `/api/elections/{id}/simulate` · POST `/api/elections/{id}/finalize` | 5 |
| GET | `/api/elections/{id}/president` · `/electoral-college` · `/provinces` · `/provinces/{code}` · `/municipalities` · `/municipalities/{code}` · `/house` · `/house/{district}` · `/senate` · `/governors` · `/mayors` · `/races/{race}` · `/calls` · `/timeline` | 6 |
| GET/POST | `/api/night/{id}/state` · POST `/control` · GET `/stream` · `/municipalities` · `/municipalities/{code}` · `/races/{race}` · POST `/calls/{race}/override` | 7 |
| GET | `/api/history/summary` · `/compare` · `/closest` · `/landslides` · `/divergence` · `/municipality/{code}` · `/province/{code}` · `/district/{code}` · `/api/analytics/{id}` | 8 |
| GET | `/api/parties` · `/api/candidates` · `/api/candidates/{id}` | 9 |
| GET/POST | `/api/polls/{id}` · GET `/api/polls/{id}/average` · POST `/api/polls/{id}` | 10 |
| GET/POST | `/api/campaigns/{id}` · POST `/api/campaigns/{id}/plan` | 11 |
| POST/GET | `/api/forecast/{id}/run` · `/api/forecast/jobs` · `/api/forecast/jobs/{job_id}` · `/api/forecast/{id}/latest` · `/api/forecast/{id}/runs` · `/api/forecast/runs/{run_id}` | 12 |
| GET/POST/PUT/DELETE | `/api/scenarios` · `/api/scenarios/{slug}` · `/duplicate` · `/export` · `/api/scenarios/validate` | 13 |
| GET | `/api/export/schemas` · `/api/export/{id}/{dataset}.{csv\|json}` | 14 |
