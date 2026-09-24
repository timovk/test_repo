# HTTP API

The FastAPI backend (`python -m app run`, default <http://127.0.0.1:8000>) serves JSON under
`/api` and the single-page UI under `/`. Interactive OpenAPI docs: `/api/docs`.

Conventions:

* Every payload that carries data includes provenance: either a top-level `data_category`
  (`REAL` | `DERIVED` | `FICTIONAL` | `SIMULATED`) or a `provenance` object mapping fields/sections
  to categories. The UI renders these as badges.
* Codes, not DB ids, identify geography: provinces `NB`, municipalities `GM0855`, districts `NB-07`,
  races `PRES`, `PRES-NB`, `HOUSE-NB-07`, `SEN-NB-1`, `GOV-NB`, `MAYOR-GM0855`.
* Elections are addressed by numeric `election_id`; `latest` and `demo` aliases are accepted
  wherever `{election_id}` appears.
* Percentages are numbers in 0–100 (`pct`), shares in 0–1 (`share`); margins in percentage points.
* Errors: `{"detail": "...", "code": "not_found|invalid|conflict|not_prepared"}` with a proper HTTP status.

## System

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | liveness + DB/geography readiness |
| GET | `/api/meta` | app/version, constitution (`electoral_votes` 174, `presidential_majority` 88, `house_seats` 150, `house_majority` 76, `senate_seats` 24, `senate_majority` 13, labels `88 TO WIN` …), active vintage/plan, elections, demo election id, data-category legend |
| GET | `/api/settings` / PUT | UI settings persisted in `app_meta` (default playback speed, party colour overrides, theme) |
| GET | `/api/data/provenance` | REAL sources (publisher, URL, licence, sha256, retrieval date), DERIVED transforms, FICTIONAL constructs, SIMULATED outputs |

## Geography (REAL) and fictional electoral geography

| Method | Path | Description |
|---|---|---|
| GET | `/api/geo/{layer}.geojson` | cached EPSG:4326 layers: `provinces`, `municipalities`, `districts` (active plan), `units/{PV}` |
| GET | `/api/provinces` | 12 provinces: population, area, density, municipalities, House seats, EV |
| GET | `/api/provinces/{code}` | detail incl. demographics, districts, Senate seats, governor |
| GET | `/api/municipalities?province=` | municipalities with population, area, density, urbanity, demographics |
| GET | `/api/municipalities/{code}` | detail incl. districts overlapping (with population shares), units |
| GET | `/api/apportionment` | method, per province population / quota / seats / EV / persons-per-seat, priority list, totals |
| GET | `/api/districts?province=` | active-plan districts with statistics |
| GET | `/api/districts/{code}` | district detail: stats, municipalities (fragments), neighbours, election history |
| GET | `/api/districts/plan` | plan metadata: seed, method, config hash, validation summary, deviation stats |
| GET | `/api/senate/seats` | 24 seats: province, seat number, class, current holder, next election |

## Elections, results and history

| Method | Path | Description |
|---|---|---|
| GET | `/api/elections` | list: id, year, name, type, status, date, contents (president/house/senate classes/governors/municipal) |
| POST | `/api/elections` | create from scenario `{scenario: slug, seed?}` |
| GET | `/api/elections/{id}` | summary incl. race counts, seed, scenario, previous election |
| POST | `/api/elections/{id}/simulate` | generate the (hidden) final result + reporting timeline `{seed?}` |
| POST | `/api/elections/{id}/finalize` | instant finish without election night |
| GET | `/api/elections/{id}/president` | national tickets (votes, pct, EV, portrait key, colours), 174/88, winner, PV/EV margins, tipping point, divergence, contingent election, province map data |
| GET | `/api/elections/{id}/electoral-college` | EC analysis: EV by ticket, province winners and margins, tipping point, closest, largest victory, divergence |
| GET | `/api/elections/{id}/provinces` | per province: EV, leader/winner, status, margin, turnout, reporting, flip/hold |
| GET | `/api/elections/{id}/provinces/{code}` | province page: presidential result, municipality table, largest remaining municipalities, outstanding vote, swing, previous result, House/Senate/Governor races |
| GET | `/api/elections/{id}/municipalities?race=PRES&province=` | map/table rows: leader, margin, shares, turnout, reporting, previous winner, swing, outstanding |
| GET | `/api/elections/{id}/municipalities/{code}` | municipality page: every race touching it, unit (precinct) table |
| GET | `/api/elections/{id}/house` | seat counter by party (won/called/leading/net change), 76 for control, district rows |
| GET | `/api/elections/{id}/house/{district}` | district race detail + municipality breakdown + history |
| GET | `/api/elections/{id}/senate` | 24 seats: not up / contested, candidates, leader, projected winner, flip/hold, current vs projected composition, 13 for control |
| GET | `/api/elections/{id}/governors` | governor races |
| GET | `/api/elections/{id}/mayors` | mayor races (when held) |
| GET | `/api/elections/{id}/races/{race}` | generic race detail incl. call history and recount audit |
| GET | `/api/elections/{id}/calls` | chronological race-call log with evidence |
| GET | `/api/elections/{id}/timeline` | reporting timeline summary (per event) |
| GET | `/api/history/summary` | per election: president, EV, PV, House/Senate control |
| GET | `/api/history/compare?a=&b=&race=&level=` | swing table, flips (lineage-aware) |
| GET | `/api/history/closest?n=&race_type=` · `/landslides` · `/divergence` | records |
| GET | `/api/history/municipality/{code}` · `/province/{code}` · `/district/{code}` | series across elections |
| GET | `/api/analytics/{id}` | lean, elasticity, competitiveness, efficiency gap, seat–vote, EC efficiency, tipping-point margin |
| GET | `/api/parties` · `/api/candidates?search=` · `/api/candidates/{id}` | parties (lineage, colours), candidate profiles and careers |

## Election night (SIMULATED)

| Method | Path | Description |
|---|---|---|
| GET | `/api/night/{id}/state?detail=summary|full` | live snapshot (national counters, EV, House/Senate/Governor tallies, provinces, races, recent calls) + clock `{status, speed, seq, total_events, sim_time_s, clock}` |
| POST | `/api/night/{id}/control` | `{action: start|pause|resume|speed|step|finish|reset, speed?}` |
| GET | `/api/night/{id}/stream` | Server-Sent Events: pushes the summary snapshot whenever it changes |
| GET | `/api/night/{id}/municipalities?race=` | live municipality map rows |
| GET | `/api/night/{id}/races/{race}` | live race detail incl. evidence of the current call state |
| POST | `/api/night/{id}/calls/{race}/override` | manual call override `{status, line_key?, reason}` |

## Forecasting, polling, campaigns, scenarios, export

| Method | Path | Description |
|---|---|---|
| POST | `/api/forecast/{id}/run` | `{simulations, seed}` → background run id |
| GET | `/api/forecast/{id}/latest` · `/api/forecast/runs/{run_id}` | EV / seat distributions, win & majority probabilities, province & district win frequencies, top EC maps |
| GET | `/api/polls/{id}?type=&geo=` · `/api/polls/{id}/average?type=&geo=` | polls and averages (trend, house effects, weights) |
| POST | `/api/polls/{id}` | add a manual poll |
| GET | `/api/campaigns/{id}` · POST `/api/campaigns/{id}/plan` | campaigns, allocations and realised effects |
| GET | `/api/scenarios` · `/api/scenarios/{slug}` · POST · PUT · POST `/{slug}/duplicate` · GET `/{slug}/export` · POST `/validate` | scenario editor |
| GET | `/api/export/{id}/{dataset}.{csv|json}` | stable-schema exports (national, provinces, municipalities, units, house, senate, governors, electoral_votes, timeline, calls, montecarlo_summary, montecarlo_distribution, polling_averages, polls) |
