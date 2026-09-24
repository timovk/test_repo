# Database

The NL Federal Election Simulator persists everything relationally through SQLAlchemy 2 models
(`src/app/models/`, 55 tables) and Alembic migrations (`src/app/db/migrations/`). The default
database is SQLite (`data/nlfed.db`); PostgreSQL works through `NLFED_DATABASE_URL`. Models hold
no election logic: pure engines compute, and the services layer (`src/app/services/`) translates
between rows and engine types and owns every transaction.

> **REAL vs FICTIONAL.** Each table below belongs to one data category
> (`app.core.constitution.DataCategory`). The REAL tables hold official Dutch geography and
> statistics (CBS / PDOK). The FICTIONAL tables hold the invented federal system, parties and
> people. The SIMULATED tables hold model output. No table mixes REAL facts with invented values
> without labelling them: DERIVED columns (estimated eligible voters, imputed demographics) are
> flagged where they live.

---

## 1. Tables by data category

### REAL (official geography and statistics)

| table | content |
|---|---|
| `data_source` | provenance of each downloaded dataset: publisher, URL, licence, sha256, retrieval time |
| `geo_vintage` | one edition of the CBS Wijk- en Buurtkaart (year, counts, population total, active flag) |
| `province` | the 12 provinces, with stable ids 1–12 in canonical order (`GR` … `LI`) |
| `province_stats` | population, area, density and simplified EPSG:4326 geometry (WKB) per province and vintage |
| `municipality` | CBS gemeenten per vintage: population, area, density, urbanity class, centroids, geometry |
| `municipality_demographics` | CBS indicators per municipality (`imputed_fields` lists DERIVED values) |
| `geo_unit` | CBS buurten, used as precincts: population, `eligible_voters_est` (DERIVED), wijk code, centroid |
| `geo_unit_demographics` | CBS indicators per buurt (`imputed_fields` lists DERIVED values) |
| `municipality_lineage` | mergers and splits between vintages, with population weights |

### FICTIONAL (the invented system, its actors and scenarios)

| table | content |
|---|---|
| `apportionment`, `apportionment_seat` | House seats per province (Huntington–Hill by default), quota, EV = seats + 2 |
| `district_plan` | a generated House plan: seed, method, config JSON and hash, deviation statistics, active flag |
| `house_district` | the 150 single-member districts (`NB-07`): statistics, simplified geometry |
| `district_assignment` | every buurt → exactly one district of a plan (`source`: generated / override) |
| `district_municipality`, `district_adjacency` | district × municipality fragments; district neighbours |
| `senate_seat` | the 24 Senate seats `SEN-<PV>-<1\|2>` with their class (1–3) |
| `office` | elected offices: `PRES`, `VP`, `HOUSE-<district>`, `SEN-<PV>-<n>`, `GOV-<PV>`, `LTGOV-<PV>`, `MAYOR-<GM>` |
| `governor_seat`, `mayor_seat` | the office(s) of each province or municipality, and the first election year |
| `legislature` | the 12 provincial legislatures and one council per municipality. Sizes follow the real Provinciewet and Gemeentewet brackets |
| `party`, `party_modifier`, `party_event` | parties (current identity), model parameters per scenario (`source = scenario:<slug>`), lineage (founded, renamed, recoloured …) |
| `candidate`, `candidate_affiliation` | fictional people, identified by a persistent `key` across elections; party membership history |
| `scenario` | the scenario YAML each election was created from (text + hash; changed documents become variants `<slug>--<hash8>`) |
| `pollster`, `pollster_house_effect` | invented pollsters and their configured house effects |
| `election` | one election day: year, date, type, status, seed, scenario, vintage, apportionment, plan, previous election |
| `race` | one contest (`PRES`, `PRES-NB`, `HOUSE-NB-07`, `SEN-NB-1`, `GOV-NB`, `PROVLEG-NB`, `MAYOR-GM0855`, `COUNCIL-GM0855`) with its incumbent, previous race and, once final, its summary |
| `ballot_candidate` | ballot lines: candidate + running mate, `line_key`, a snapshot of the party identity and of the candidate quality used on this ballot |
| `campaign` | one campaign per party and election: budget, strategy, seed (linked to the party's presidential ticket when it has one) |

### SIMULATED (model output)

| table | content |
|---|---|
| `election_result` | votes per ballot line at every geographic level (section 3) |
| `turnout_result` | eligible / cast / valid / blank / invalid per race and level |
| `electoral_vote_allocation` | EV per province × ticket (the national `PRES` race and its `PRES-<PV>` contest) |
| `contingent_election` | a contingent election with its complete audit trail (`ballots_json`) |
| `legislature_seat_result` | D'Hondt seats per party in council and provincial-legislature races |
| `office_holder` | who holds which office: term start and end (calendar), actual end, reasons, the race that elected them |
| `vacancy` | vacancies and their special elections |
| `reporting_event`, `reporting_event_unit` | the election-night timeline: batches per municipality; fraction increments per buurt |
| `race_call`, `night_session` | race calls with their evidence; the playback state of an election night |
| `recount`, `recount_adjustment` | automatic recounts and each audited correction (`pile = line \| invalid`) |
| `poll`, `poll_result` | fictional polls generated from the model expectation |
| `campaign_allocation` | spending per action, target and week, with expected and realised effects |
| `simulation_run` | the audit record of every stochastic step (section 6) |
| `forecast_summary`, `forecast_distribution`, `forecast_aggregate`, `forecast_combination` | Monte Carlo output |

### Application

`app_meta` holds key/value state: `active_vintage_year`, `geo_vintage_fingerprint_<year>`, and
`geography_source`, which tells `services.runtime.get_frame` whether the frame of this database
comes from the REAL store, from a registered synthetic test geography, or from the database rows.
`alembic_version` is the migration state.

---

## 2. Key relationships

```
geo_vintage ─┬─ municipality ── geo_unit ─── district_assignment ── house_district ── district_plan ── apportionment
province ────┘        │               │                                   │
                      │               └── election_result.geo_unit_id     └── office (HOUSE-<code>, by district code)
senate_seat ── office (SEN-<PV>-<n>)      governor_seat / mayor_seat ── office          legislature (provincial | municipal)

scenario ── election ─┬─ race ─┬─ ballot_candidate ── candidate (+ running mate) ── candidate_affiliation ── party
                      │        ├─ parent_race_id (PRES-<PV> → PRES), previous_race_id (same office, earlier election)
                      │        ├─ election_result / turnout_result (per level)
                      │        ├─ recount ── recount_adjustment
                      │        ├─ electoral_vote_allocation, contingent_election, legislature_seat_result
                      │        └─ race_call
                      ├─ reporting_event ── reporting_event_unit
                      ├─ poll ── poll_result;  campaign ── campaign_allocation
                      └─ simulation_run (election-setup | election | election-final)
office ── office_holder (candidate, party, term_start, term_end, ended_on, election_race_id)
```

* **Offices outlive plans.** A House office is keyed by district *code* (`HOUSE-NB-07`), so it
  survives redistricting. Offices of districts that are no longer in the active plan are
  deactivated, not deleted.
* **Parties never rewrite history.** `party` holds the current identity. Every ballot line keeps
  the code, name, abbreviation and colour it was printed with (`party_*_snapshot`). Renames and
  recolours are `party_event` rows.
* **Careers.** A candidate `key` is permanent. An incumbent who runs again appears on the new
  ballot as the same `candidate` row, and `race.previous_race_id` links each race to the previous
  race for the same office.
* **Incumbency snapshot.** When an election is created, the office holders serving on election
  day are frozen into `race.incumbent_candidate_id` / `incumbent_party_id` and
  `ballot_candidate.is_incumbent`. The president's party and the Senate holdovers are frozen into
  the `election-setup` run. An election's engine inputs therefore never change afterwards.

---

## 3. Result levels and `geo_key`

`election_result` (one row per race × ballot line × geography) and `turnout_result` (one row per
race × geography) are stored at every level:

| level | `geo_key` | id columns set | stored for |
|---|---|---|---|
| `unit` | `U:<geo_unit_id>` | unit, municipality, province (+ district for House / presidential races) | every race |
| `municipality` | `M:<municipality_id>` | municipality, province | every race (House races: the district's municipal fragments) |
| `district` | `D:<house_district_id>` | district, province | House races and presidential races (`PRES`, `PRES-<PV>`) |
| `province` | `P:<province_id>` | province | every race |
| `national` | `N` | – | every race |

The levels come from `app.elections.tabulation.aggregate_levels` and are integer sums. Before
anything is written, the services check that line votes and every count reconcile exactly:
unit → municipality → province → national, and unit → district → province. The national `PRES`
race is the union of its 12 province contests. `services.validation.reconcile_election`
repeats the check on the stored rows.

`share` is the share of the valid votes at that level (0–1). `turnout_pct` is ballots cast /
eligible × 100. `services.results.results_frame` turns the rows into the standard results frame
of docs/ARCHITECTURE.md §7.

---

## 4. Election lifecycle and the hidden-until-reported rule

```
create_election ──▶ scheduled ──simulate_election──▶ simulated ──(night service)──▶ live ──▶ final
                                                          └──────────finalize_election / instant_finalize──────▶ final
```

* **scheduled** — races, ballots, candidates, campaigns and polls exist; no votes.
* **simulated** — the complete result is stored at every level, and the election-night timeline
  (`reporting_event*`) is stored with it. The realised random environment is kept in
  `election.national_environment_json`.
* **live** — an election night is replaying the stored result (managed by the night service).
* **final** — tabulation, recounts, Electoral College, contingent election, race summaries,
  D'Hondt seats, office holders and (optionally) race calls are stored.

**Hidden until reported.** A SIMULATED or LIVE election already has its full result in the
database. Only election-night components that reveal it progressively may read those rows (the
night engine reads the timeline and the final unit results). The read layer, `results_frame`
(unless `include_hidden=True`), `election_summary` (unless `include_hidden=True`) and every
export must treat an election as unreported until its status is `final` (or `certified`). Poll
audit data that would reveal the truth is never stored.

A SIMULATED election can be simulated again: its results, timeline, calls and night state are
replaced. LIVE and FINAL elections cannot be re-simulated. Elections are finalized in
chronological order.

---

## 5. Office holders and terms

When an election is finalized, each winner takes office for the term given by
`ElectionCalendar.term_bounds`. Federal terms start on 15 January after the election, provincial
and municipal terms on 1 January. Founding senators of class *c* serve 2*c* years. A special
Senate election fills only the rest of the seat's term. The previous holder's `ended_on` is set to
the successor's `term_start`, with `end_reason` `term_expired` (re-elected), `defeated` (ran and
lost) or `retired` (did not run).

The Vice-President is the elected ticket's running mate, or the Senate's choice in a contingent
election. If a contingent election deadlocks with `vice_president_acts`, the Vice-President-elect
also holds `PRES` with `start_reason = 'acting'`. Lieutenant governors are the running mates of
the elected governors.

The holders on a given day are the rows with `term_start ≤ day` and no `ended_on` or a later one.
A holder whose term has expired without an election of a successor keeps serving.

---

## 6. Seeds and audit

Randomness comes from `app.core.rng.make_rng(seed, *keys)`. Every stochastic step writes a
`simulation_run` row (kind, seed, config hash, duration, summary JSON):

| kind | written by | summary |
|---|---|---|
| `districts` | House plan generation | plan id, deviation, splits, timings |
| `election-setup` | `create_election` | president's party, Senate holdovers, race counts, campaigns, polls, warnings |
| `election` | `simulate_election` | row counts, turnout, model fingerprint, timeline metadata (reference close, time zone, seed, config fingerprint, poll-closing offsets) |
| `election-final` | `finalize_election` | recounts, EV, President, contingent summary, office holders, calls |

The election seed (`election.seed`) drives the vote draw, the timeline, tie lots, recounts and the
contingent election. Campaigns and polls use child seeds derived from it (`derive_seed(seed,
"campaigns" | "polls")`). The same scenario, seed and system give identical stored results; this is
tested.

---

## 7. Migrations

```bash
python -m app db upgrade                       # latest revision (SQLite: data/nlfed.db)
NLFED_DATABASE_URL=postgresql+psycopg://user:pw@host/nlfed python -m app db upgrade
```

Programmatically: `app.services.bootstrap.init_db(url=None)` or `app.db.migrate.upgrade_db(url)`.
To add a revision after a model change, run `app.db.migrate.make_revision("message")` against an
up-to-date database, then review the generated file. It must upgrade and downgrade cleanly on
SQLite (batch mode is enabled for SQLite automatically).

The initial revision (`d4bc2f767e86`) creates all 55 tables. The only reference cycle is
`race.winner_ballot_candidate_id` ↔ `ballot_candidate.race_id`. On SQLite it is created inline,
because SQLite accepts forward references. Elsewhere it is added with `ALTER TABLE` once both
tables exist. The downgrade clears and drops it before dropping `ballot_candidate`. Tests may
create the schema with `Base.metadata.create_all`, which is equivalent; `alembic check` reports no
difference.

PostgreSQL notes: install the `postgres` extra (`psycopg[binary]`). Long constraint names are
truncated with a hash (63-character limit). All `IN (…)` queries are chunked.

---

## 8. Sizes (REAL geography, 2025 vintage)

After `setup_system` and the three demo elections (`founding-2024` 211 races, `midterm-2026` 842
races incl. 342 mayors and 342 councils, `demo-2028` 195 races):

| table | rows |
|---|---|
| `geo_unit`, `geo_unit_demographics`, `district_assignment` | 14,729 each |
| `municipality` / `legislature` / `office` | 342 / 354 / 542 |
| `election_result` | 686,000 / 355,000 / 553,000 per election (≈ 85 % at unit level) |
| `turnout_result` | 106,000 / 58,000 / 86,000 per election |
| `reporting_event` / `reporting_event_unit` | ≈ 2,400 / ≈ 15,200 per election |
| `candidate` / `office_holder` | ≈ 4,700 / ≈ 880 after three elections |
| `campaign_allocation` / `poll` | ≈ 12,000 / ≈ 525 after three elections |

The SQLite file of the system plus the three elections is ≈ 240 MB.

Measured timings (4 shared CPUs, SQLite):

| step | founding-2024 | midterm-2026 | demo-2028 |
|---|---|---|---|
| `create_election` | 2.1 s | 1.8 s | 1.6 s |
| `simulate_election` | 9.2 s | 10.5 s | 8.5 s |
| `finalize_election` | 8.3 s | 5.4 s | 7.2 s |

`setup_system` takes 25–50 s. Most of that is House districting with worker processes; a
re-run reuses the plan. `validate_system` over the three elections takes 10 s. The synthetic
sandbox (`services.sandbox.build_sandbox`) takes ≈ 12 s.

## 9. Services quick reference

| module | API |
|---|---|
| `services.bootstrap` | `init_db`, `prepare_geography`, `load_geography`, `ensure_apportionment`, `ensure_district_plan`, `ensure_offices`, `setup_system`, `setup_synthetic_system` |
| `services.runtime` | `get_frame`, `plan_mapping`, `get_model`, `election_inputs → ElectionInputs`, `race_expectations`, `load_final_race_votes`, `load_timeline`, `timeline_meta` |
| `services.elections` | `create_election`, `simulate_election`, `finalize_election`, `instant_finalize`, `election_summary`, `list_elections` |
| `services.results` | `results_frame` (standard results frame; hidden elections excluded) |
| `services.validation` | `validate_system`, `validate_election`, `reconcile_election` → `ValidationReport` |
| `services.sandbox` | `build_sandbox` — the synthetic world used by tests |

---

## 10. Limitations

* Campaign effects on the turnout of a party's supporters enter the vote model as the equivalent
  vote-share shift, `t · (1 − turnout_base)`, because the election context has no
  geography-specific party turnout.
* Municipal councils are always elected from D'Hondt party lists. The scenario option
  `council_system: wards_fptp` is recorded as a warning.
* A candidate's ideology on the European axis is not stored (`candidate` has economic and social
  columns only). People who return in a later scenario without their own ideology use their
  party's.
* `party` rows hold the latest identity. An older election created after a newer one leaves them
  unchanged; its ballots still carry the identity of their own scenario.

