# NL Federal Election Simulator

**The real Netherlands under a fictional American-style federal constitution.**

This is an election-modelling platform with a full election-night broadcast. It keeps the **real geography and
population of the Netherlands** (12 provinces, 342 municipalities, 14,729 CBS neighbourhoods) and
replaces the parliamentary monarchy with a **fictional presidential federal republic**:

| U.S. institution | Fictional Dutch counterpart | Numbers |
|---|---|---|
| State | **Province** (provincie) | 12 |
| County | **Municipality** (gemeente) | 342 (from the CBS data, not hard-coded) |
| Precinct | **CBS neighbourhood** (buurt) | 14,729 |
| House of Representatives | **Tweede Kamer**: 150 single-member districts, first-past-the-post, every 2 years | **76 FOR CONTROL** |
| Senate | **Eerste Kamer**: 2 senators per province, 6-year terms, 3 staggered classes of 8 | 24 seats · **13 FOR CONTROL** |
| Electoral College | 150 House-based + 24 Senate-based electoral votes, winner-take-all per province | 174 EV · **88 TO WIN** |
| President / Vice President | Directly elected national ticket (replaces the minister-president) | contingent election if nobody reaches 88 |
| Governors, mayors | Directly elected; provincial legislatures and municipal councils | |

> **REAL vs FICTIONAL.** Geography, boundaries, population and demographics are **real** open
> data from CBS (Statistics Netherlands) and PDOK. The constitution, House districts,
> Senate classes, parties, candidates, polls, campaigns, forecasts and **every result are fictional
> simulations**. None of this is a forecast of real Dutch politics. Every screen, API payload and
> export states which category each piece of data belongs to (`REAL`, `DERIVED`, `FICTIONAL` or
> `SIMULATED`).

## What it does

- **Reproducible geography pipeline.** It downloads the CBS *Wijk- en Buurtkaart* and generalized
  boundaries from PDOK, plus CBS StatLine demographics. It then builds a GeoParquet store with
  neighbourhood adjacency (with water links for islands), topology-preserving web layers and
  flagged imputations. Once built, it works offline.
- **Apportionment.** The 150 House seats are divided among the provinces with Huntington–Hill by
  default (Hamilton, Webster, Jefferson and Adams are also available). Each province's EV = seats + 2,
  which always totals 174.
- **Algorithmic districting.** It draws 150 contiguous, population-balanced districts (maximum deviation
  under 2 %) that keep municipalities whole wherever possible. It splits large cities along neighbourhood
  lines, reports compactness metrics, is reproducible from a seed, and accepts manual overrides.
- **Probabilistic election model.** Votes come from a multinomial-logit model. Its inputs are real
  CBS demographics plus fictional party parameters, calibrated baselines, a persistent spatially correlated
  local lean, elasticity, turnout, candidate quality, incumbency, campaign effects, strategic voting and
  correlated shocks. Every run takes a seed.
- **Full electoral mechanics.**
  - Winner-take-all Electoral College, plus district and proportional variants.
  - Tipping point, and EV/PV divergence.
  - A configurable contingent election in which province delegations vote.
  - Staggered Senate classes, an election calendar, special elections, and auditable automatic recounts.
  - Governors, mayors, D'Hondt councils and provincial legislatures.
- **Live election night.** Municipalities report in batches through a realistic night. A race-calling
  engine (TOO EARLY / TOO CLOSE / LEAN / PROJECTED / CALLED / RECOUNT / FINAL) records the evidence for
  every call. EVs, House seats and Senate seats are allocated as races are called, and the night
  recognises when a ticket reaches 88 EV. Playback runs at 1×–25×, step by step or with instant
  finish, and is deterministic by seed.
- **Monte Carlo forecasting.** 100,000 simulated elections in about 15 s. It produces EV, seat and
  popular-vote distributions, win and majority probabilities, province and district win
  frequencies, and the most common Electoral College maps.
- **Polling and campaigns.** Fictional pollsters, poll aggregation (recency, sample-size and
  rating weights, house effects, trend, uncertainty), and campaign resource allocation with modest,
  uncertain effects.
- **History and analytics.** Every election is stored permanently. You can compute swing, flips,
  partisan lean, elasticity, uniform swing, efficiency gap / wasted votes, seat–vote curves,
  Electoral College efficiency, closest races, landslides and EV/PV divergence. Comparisons account for
  municipal mergers.
- **Web UI** styled like a national election-night broadcast, a **FastAPI** JSON API, a **CLI**, and
  stable-schema **CSV/JSON exports**.

## Quick start

Requirements: Python ≥ 3.11 and about 1.5 GB of free disk space. Network access is needed only for the
first geography download (about 260 MB of open data).

```bash
git clone <this repository> && cd <repository>
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"            # or: uv pip install -e ".[dev]"

python -m app demo                 # ≈ 2 min: data → database → districts → 2024 + 2026 history → 2028 ready
python -m app run                  # open http://127.0.0.1:8000
```

`python -m app demo` performs the whole pipeline and is idempotent:

1. Creates or upgrades the database (SQLite `data/nlfed.db`; Alembic migrations).
2. Downloads and builds the CBS/PDOK geography if it is missing (`data/raw`, `data/processed/geo_2025`).
3. Apportions 150 seats, generates the 150 districts (seed 2028), and creates the 24 Senate seats,
   the offices and the legislatures.
4. Simulates the **2024 founding election** (President, the whole House, all 24 senators, 12 governors)
   with a full election night, and the **2026 midterm** (House, Senate class 1, 342 mayors and councils).
5. Creates and simulates the **2028 general election** but keeps its results hidden. Its election night
   waits at polls closing (21:00): **0 EV allocated, 174 EV available, 88 TO WIN**.
6. Runs a 10,000-draw forecast and validates every constitutional invariant.

In the web UI, go to **Election Night** and press ▶. You can also watch the night in the terminal:

```bash
python -m app election-night --election demo --speed 25
```

## Step by step (what `demo` does)

```bash
python -m app init                                   # database schema (Alembic)
python -m app geography download                     # REAL sources → data/raw (cached; skipped when present)
python -m app geography build                        # → data/processed/geo_2025 (GeoParquet, adjacency, web layers)
python -m app geography load                         # → provinces, municipalities, neighbourhoods in the database
python -m app apportion                              # 150 seats → EV per province (174, 88 to win)
python -m app districts generate --year 2028 --seed 42
python -m app districts validate
python -m app election create --year 2028            # uses config/scenarios/general_2028.yaml
python -m app simulate --election 2028 --seed 42
python -m app forecast --election 2028 --simulations 100000 --seed 42
python -m app election-night --election 2028         # or run it live in the UI
python -m app run
```

Other useful commands: `validate`, `history`, `polls`, `export`, `scenario list|show|duplicate|import|export`,
and `db upgrade|current|revision`. The complete reference is in [docs/CLI.md](docs/CLI.md).

### Apportionment with the canonical data (CBS 2025)

| | ZH | NH | NB | GE | UT | OV | LI | FR | GR | DR | FL | ZE | total |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| House seats | 32 | 25 | 22 | 18 | 12 | 10 | 9 | 6 | 5 | 4 | 4 | 3 | **150** |
| Electoral votes | 34 | 27 | 24 | 20 | 14 | 12 | 11 | 8 | 7 | 6 | 6 | 5 | **174** |

## The web application

The sections of the app: **Election Night** · **President** · **Electoral College** · **Provinces** ·
**Municipalities** · **House** · **Senate** · **Governors** · **Forecast** · **Polling** · **Campaign** ·
**History** · **Scenario Editor** · **Data** · **Settings**.

- A national strip stays visible on every page. It shows the EV race with the 88 TO WIN marker, the House
  (76) and Senate (13) counters, reporting %, the simulated clock and the playback controls.
- The maps are Leaflet vector layers with no tile server, so they work offline.
- There are dark and light themes, and party colours can be overridden per user.
- Results of elections not yet reported stay hidden until their night reveals them.

The API is documented in [docs/API.md](docs/API.md), with interactive docs at `/api/docs`.

## Configuration

Everything political is editable data in `config/`, validated by Pydantic schemas:

| File | Contents |
|---|---|
| `constitution.yaml` | seats, classes, terms, apportionment method, EV allocation, contingent-election mode |
| `geography.yaml` | real data sources, province list, eligible-voter assumption, simplification |
| `districts.yaml`, `districts/overrides.yaml` | districting parameters and manual overrides |
| `senate.yaml` | Senate class assignment (8/8/8, two different classes per province) |
| `calendar.yaml` | founding year, election-day rule, governor/municipal cycles, term starts |
| `model.yaml`, `regions.yaml` | political-model hyperparameters; named regions (fictional assumption layer) |
| `parties/fictional.yaml` | the eight fictional parties (ideology, baselines, modifiers, colours) |
| `scenarios/*.yaml` | complete election scenarios: candidates, tickets, environment, calibration, polling, campaigns |
| `polling.yaml`, `campaigns.yaml`, `forecast.yaml`, `night.yaml`, `recount.yaml` | subsystem settings (pollster ratings, action profiles, Monte Carlo errors, reporting speed and calling thresholds, recount thresholds) |

Runtime settings are environment variables prefixed with `NLFED_`, for example:

- `NLFED_DATABASE_URL=postgresql+psycopg://…` to use PostgreSQL (install with `pip install -e ".[postgres]"`)
- `NLFED_DATA_DIR`
- `NLFED_ALLOW_NETWORK=false` to guarantee offline operation
- `NLFED_LOG_JSON=1` for structured logs

## Reproducibility

Every stochastic step takes a seed, and the seed is stored with its results. This covers district
generation, the persistent political geography, each election, each election night, recounts,
polls, campaigns and forecasts. Randomness flows through named streams (`app.core.rng`), never through
global state, so the same configuration and seed give identical output. The test suite checks this,
down to the stored calls of an election night.

## Testing

```bash
python -m pytest                       # full suite (≈ 1,100 tests; real-data tests skip until the store is built)
python -m pytest -m "not realdata"     # offline synthetic-geography tests only
python -m pytest -m realdata           # checks on the real CBS geography
ruff check src tests && ruff format --check src tests
```

The suite covers:

- constitutional invariants: 12 provinces, 150 seats, 24 senators, 174 EV, majorities 88 / 76 / 13, and EV = seats + 2
- apportionment
- districting: every unit assigned exactly once, no district crossing a province boundary, contiguity, deviation
- Senate classes and the calendar
- aggregation that reconciles exactly from precinct to national
- the Electoral College, including exact 87–87 ties, no majority, three-way races and contingent elections
- recounts, tied races, withdrawn candidates, disappearing parties and municipal mergers
- missing demographics
- simulation reproducibility
- election-night reporting and calling accuracy
- the forecasting engine, the API (including the hidden-results rule), the CLI and end-to-end runs

## Project structure

```
src/app/
  core/          constitution constants & config, settings, seed streams, logging, errors
  models/        SQLAlchemy ORM (normalised; ~55 tables)          db/  sessions + Alembic migrations
  geography/     CBS/PDOK download → GeoParquet store → vectorised GeographyFrame, adjacency, DB loader
  districts/     apportionment, districting, overrides, stats, validation, Senate classes
  simulation/    structural political model, voting, candidates, regions, baselines
  elections/     tabulation, Electoral College, contingent election, calendar, recounts, seats
  reporting/     election-night timeline, race calling, live engine, playback clock
  forecasting/   Monte Carlo engine        polling/  polls & aggregation        campaigns/  campaign model
  analytics/     swing, lean, elasticity, efficiency gap, history     export/  stable CSV/JSON schemas
  scenarios/     scenario schema, loader, editor
  services/      orchestration & persistence (elections, night, demo, validation, read models)
  api/           FastAPI application           ui/static/  broadcast-style single-page app
  cli/           Typer CLI (`python -m app`, `nlfed`)
config/          all editable (mostly FICTIONAL) configuration
docs/            technical documentation
tests/           unit, integration, real-data and end-to-end tests
scripts/         development helpers (headless UI screenshots)
legacy_web/      an unrelated static site that previously lived in this repository (kept unchanged)
```

## Documentation

| Document | Topic |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | layers, data flow, engine contracts |
| [ELECTORAL_SYSTEM.md](docs/ELECTORAL_SYSTEM.md) | the fictional constitution, EV arithmetic, contingent election, calendar, recounts |
| [DATA_PROVENANCE.md](docs/DATA_PROVENANCE.md) | real data sources, licences, derivations, limitations |
| [DISTRICTING.md](docs/DISTRICTING.md) | apportionment methods and the districting algorithm |
| [SIMULATION.md](docs/SIMULATION.md) | the political model and demo calibration |
| [FORECASTING.md](docs/FORECASTING.md) | Monte Carlo methodology |
| [RACE_CALLING.md](docs/RACE_CALLING.md) | election-night reporting and race-calling methodology |
| [POLLING.md](docs/POLLING.md), [CAMPAIGNS.md](docs/CAMPAIGNS.md) | polls, aggregation and campaigns |
| [ANALYTICS.md](docs/ANALYTICS.md) | metric definitions and export schemas |
| [DATABASE.md](docs/DATABASE.md) | schema and migrations |
| [API.md](docs/API.md), [CLI.md](docs/CLI.md) | interfaces |

## Data licences and attribution

Geographic and demographic data: © Centraal Bureau voor de Statistiek (CBS) and PDOK, used under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Details are in
[docs/DATA_PROVENANCE.md](docs/DATA_PROVENANCE.md). All parties, candidates and pollsters are
invented; any resemblance to real persons or organisations is coincidental. The vendored
Leaflet library (BSD-2-Clause) and the Inter and JetBrains Mono fonts (SIL OFL 1.1) keep their licence files
in `src/app/ui/static/vendor/`.
