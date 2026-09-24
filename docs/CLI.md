# Command-line interface

The CLI runs as `python -m app …` or through the `nlfed` console script (entry point
`app.cli.main:main`). It is built with Typer and rich. Every command calls the services layer:
the CLI does no election mathematics of its own.

The geography is **REAL** (CBS/PDOK: 12 provinces, 342 municipalities, 14,729 buurten used as
precincts). The constitution, the districts, the parties and the candidates are **FICTIONAL**.
Every vote, poll, forecast and race call is **SIMULATED**. Output that carries data is labelled
with its category.

```bash
source .venv/bin/activate
python -m app demo                              # the reproducible demo world (≈ 2 min)
python -m app run                               # web UI and API on http://127.0.0.1:8000
python -m app election-night --election demo    # the 2028 election night in the terminal
```

## Global options

| Option | Meaning |
|---|---|
| `--verbose` / `-v` | More logging: `-v` shows info, `-vv` shows debug and tracebacks. The default is warnings only. Logs go to stderr. |
| `--json-logs` | Logs as JSON lines on stderr. |
| `--database-url URL` | The database for this run. Same as `NLFED_DATABASE_URL`. The default is `sqlite:///data/nlfed.db`. |
| `--version` | Print the version. |

Elections are addressed with `--election/-e`, which accepts:

- an id (`3`);
- a year (`2028` means the most recent election of that year);
- `latest`;
- `demo` (`app_meta.demo_election_id`).

Many commands take `--json` for machine-readable output on stdout.

### Exit codes

Errors print a one-line message instead of a traceback. Add `-vv` to see the traceback.

| Code | Meaning |
|---|---|
| 0 | success |
| 1 | failure (for example a validation that failed) |
| 2 | usage error (bad option) |
| 3 | data not prepared: the REAL geography is not built or not loaded (`DataNotPreparedError`) |
| 4 | not found: election, race, dataset or file |
| 5 | invalid scenario, configuration or data |
| 6 | conflict with an election's state (for example finalizing twice, or controlling the night of a FINAL election) |
| 130 | interrupted (Ctrl+C) |

---

## System

### `demo`

`demo` builds the demo world in one command. It is reproducible and idempotent: completed steps
are skipped when you run it again.

1. **database:** Alembic migrations.
2. **geography:** the REAL CBS store. It is downloaded and built only when it is missing, so a
   machine that is already prepared works offline.
3. **system:** geography in the database, the apportionment (150 seats, 174 EV), the House plan
   (generated from `--seed`), Senate seats, offices and legislatures.
4. **history:** `founding-2024` and `midterm-2026` are each created, simulated, run through a
   complete election night at once and finalized, so every race call is stored.
5. **demo-2028:** created and simulated, then left **SIMULATED**. Its election night starts at
   polls closing: 0 EV allocated, 174 available, **88 TO WIN**.
6. **forecast:** a 2028 Monte Carlo forecast (`--forecast-simulations`, seed `--seed`).
7. **validate:** `app_meta.demo_election_id` is set and `validate_system` must pass.

```bash
python -m app demo                                  # data/nlfed.db
python -m app demo --force                          # rebuild the database from scratch
python -m app demo --seed 7 --forecast-simulations 0
python -m app demo --synthetic --database-url sqlite:///tmp/toy.db   # offline toy country
python -m app demo --json                           # summary with per-step timings
```

Options:

- `--seed` (2028): seed of the House plan and of the forecast.
- `--force`: drop and recreate the schema. The REAL geography store is kept.
- `--forecast-simulations` (10000; 0 means no forecast).
- `--election-seed`: derive every election's seed from this value. By default each election uses
  its scenario's seed.
- `--synthetic`: use the synthetic toy country.
- `--workers`: worker processes for districting and forecasting.

On the REAL data (4 shared CPUs, SQLite) the whole build takes about 105 s. The steps:

| Step | Time |
|---|---|
| system | 15 s |
| 2024 | 30 s (the night and finalize take 19 s of it) |
| 2026 | 32 s (night and finalize 19 s) |
| 2028 | 10 s |
| forecast of 10,000 draws | 5 s |
| validation | 11 s |

The database is about 260 MB. The build is deterministic: rebuilding it gives identical results,
race calls (with their evidence and times), recounts, office holders, polls and forecast.

### `init`

`init` creates or upgrades the schema. With `--with-geography` it also prepares the REAL
geography (download and build, only when missing) and sets up the system: apportionment, House
plan, Senate seats and offices.

```bash
python -m app init
python -m app init --with-geography --district-seed 2028 --workers 0
```

### `run`

`run` serves the API and the single-page UI: `uvicorn app.api.main:create_app` with
`factory=True`. The host and port default to the settings (`NLFED_HOST`, `NLFED_PORT`).

```bash
python -m app run
python -m app run --host 0.0.0.0 --port 8080
python -m app run --reload          # development
```

### `validate`

`validate` checks the constitution (174/88, 150/76, 24/13), the geography, the apportionment, the
House plan, the Senate classes, the offices, and the exact reconciliation of every stored
election. It exits with code 1 on failure.

```bash
python -m app validate
python -m app validate --election 2028 --json
```

### `db upgrade | current | revision`

```bash
python -m app db upgrade                      # to the latest revision (creates the database)
python -m app db upgrade --revision d4bc2f767e86
python -m app db current
python -m app db revision -m "add a column"   # autogenerate against an up-to-date database
python -m app db revision -m "data fix" --empty
```

---

## Geography, apportionment and districts

### `geography download | build | load`

```bash
python -m app geography download [--year 2025] [--force]   # REAL CBS/PDOK sources → data/raw
python -m app geography build    [--year 2025] [--force]   # → data/processed/geo_2025
python -m app geography load     [--year 2025] [--force]   # → provinces, municipalities, units in the database
```

`download` reuses files that are already present, unless you pass `--force`. Set
`NLFED_ALLOW_NETWORK=false` to guarantee offline operation.

### `apportion`

`apportion` assigns the 150 House seats to the 12 provinces. Each province's EV is its seats + 2,
for 174 in total and 88 to win. The table lists population, quota, seats, EV and persons per seat.

```bash
python -m app apportion
python -m app apportion --method webster --json
```

The method is `huntington_hill` by default. The others are `webster`, `jefferson`, `adams`,
`dean` and `hamilton`.

### `districts generate | validate | show`

```bash
python -m app districts generate --year 2028 --seed 42 --workers 0   # generate (or reuse) and activate
python -m app districts validate                                     # 150 districts, seats per province, coverage …
python -m app districts show --province NB
python -m app districts show --plan 1 --json
```

`generate` reuses an existing plan made from the same geography, apportionment, seed and
configuration. It then updates the House offices of the active plan.

---

## Elections

### `election create | list | show`

```bash
python -m app election create --year 2028                      # uses that year's scenario (demo-2028)
python -m app election create --scenario demo-2028 --seed 42
python -m app election create --scenario demo-2028 --year 2032 # the scenario moved to another cycle
python -m app election create --year 2026 --simulate
python -m app election list
python -m app election show --election 2024
```

`--lenient` ignores scenario references to places that are missing from the geography. It is
switched on automatically for the synthetic geography. `show` prints results only once an election
is FINAL (the hidden-until-reported rule). `--scenario` accepts built-in and user scenarios. An
election cannot be created for a date on or before an election that is already FINAL (exit code 6):
elections are certified in chronological order.

### `simulate`

`simulate` draws the complete result, which stays hidden, and the election-night timeline. It
accepts an election id or a year.

```bash
python -m app simulate --election 2028 --seed 42
```

### `finalize`

`finalize` certifies an election without an election night. It runs tabulation, the automatic
recounts, the Electoral College (and the contingent election when needed), the chamber seats and
the office holders.

```bash
python -m app finalize --election 2028
```

### `election-night`

`election-night` shows a broadcast-style night in the terminal. It runs through the election-night
service, so every race call is persisted exactly as in the web UI:

- **Ctrl+C** pauses the night;
- running the command again resumes it;
- the UI shows the same night.

The screen shows:

- the simulated clock, the reporting percentage and the counting municipalities;
- the Electoral College bar: decided EV solid, leading EV shaded, the **88 TO WIN** mark;
- each ticket's electoral and popular votes;
- the House (**76 FOR CONTROL**) and Senate (**13 FOR CONTROL**) counters;
- the governors;
- the feed of race calls.

```bash
python -m app election-night --election 2028                  # live screen at 10×
python -m app election-night --election demo --speed 25       # 1× ≈ 8.7 min per night, 25× ≈ 21 s
python -m app election-night --election 2028 --until 23:30    # pause at 23:30 (simulated local time)
python -m app election-night --election 2028 --headless       # calls and periodic summaries as text
python -m app election-night --election 2028 --instant        # every event at once, then FINAL
python -m app election-night --election 2028 --instant --json # final state as JSON
python -m app election-night --election 2028 --reset          # back to polls closing (not for FINAL elections)
```

Options:

- `--speed`: one of 1, 2, 5, 10 or 25.
- `--until HH:MM`: pause exactly when the simulated clock reaches that time.
- `--reset`: delete the night's calls and session first.

When the last reporting event is applied, the election is finalized once, with the night's calls.
The final screen shows the certified outcome: races that ended the night in RECOUNT are resolved
by the automatic recounts, so all 174 EV and every seat are allocated. A FINAL election's night
can be displayed but not changed.

### `forecast`

`forecast` runs a Monte Carlo forecast and stores it (`app.services.forecast_runner`). The output
lists:

- each ticket's probability of an outright majority, its EV median with a 90 % interval, and its
  popular-vote mean;
- the probability of a contingent election;
- House and Senate seat medians and majority probabilities.

These are SIMULATED model estimates, never predictions.

```bash
python -m app forecast --election 2028 --simulations 100000 --seed 42
python -m app forecast --election 2028 -n 10000 --workers 1 --no-polls --json
python -m app forecast --election 2026 -n 5000 --include-local
```

### `polls`

`polls` shows the FICTIONAL polls of an election and their weighted averages as of the day before
the election.

```bash
python -m app polls --election 2028
python -m app polls --election 2028 --type national_president --geo NL
python -m app polls --election 2028 --geo NB --json
```

### `history`

`history` lists every reported election with:

- the President, their EV (`*` marks a contingent election) and the winner's popular vote;
- House and Senate control (or `none`), with the largest party;
- turnout.

```bash
python -m app history
python -m app history --json
```

### `export`

`export` writes stable-schema CSV or JSON datasets (`app.export`, via
`app.services.read.exports`) with a `manifest.json` that records schema versions, fingerprints,
data categories and SHA-256 sums. Files go to `<out>/election_<id>/`.

```bash
python -m app export --election 2028 --dataset national --format csv --out data/exports
python -m app export -e 2028 -d house,senate,governors,electoral_votes -f both
python -m app export -e 2028 -d all                       # every dataset except units and swing
python -m app export -e 2028 -d units                     # precinct results (large)
python -m app export -e 2028 -d swing --race PRES --level municipality
python -m app export -e 2028 -d municipalities --race-type HOUSE
```

Datasets are addressed by schema name or alias:

| Kind | Datasets |
|---|---|
| Results | `national`, `provinces`, `municipalities`, `units`, `district_lines`, `house`, `senate`, `governors`, `electoral_votes` (`ev`), `swing` (`compare`) |
| Election night | `timeline`, `calls` |
| Forecast | `montecarlo_summary`, `montecarlo_distribution` |
| Polls | `polls`, `polling_averages` |
| Fictional geography | `district_plan` (`districts`), `apportionment` |

Result, timeline and call datasets exist only for FINAL elections (the hidden-until-reported
rule). With `--dataset all`, datasets that are not available (for example forecasts before any
forecast has run) are skipped with a note.

---

## Scenarios

Scenario documents live in `config/scenarios`. They hold the FICTIONAL parties, candidates,
tickets, political environment and seeds.

```bash
python -m app scenario list
python -m app scenario show demo-2028
python -m app scenario show demo-2028 --yaml
python -m app scenario export demo-2028 --out /tmp/demo.yaml
python -m app scenario duplicate demo-2028 demo-2028-b --seed 99 --name "Demo 2028 (B)"
python -m app scenario import /tmp/demo.yaml --slug demo-2028-c
python -m app scenario validate demo-2028-b
```

- `import` and `duplicate` store **user scenarios** in `config/scenarios/user/<slug>.yaml` — the
  directory of the API's scenario editor, so the UI can edit them and the CLI can use scenarios
  made in the UI. A user scenario shadows a built-in one with the same slug; `list` shows the
  `source` (`builtin` or `user`).
- `import` validates a file and stores it under its slug, or under `--slug`. It refuses to
  overwrite an existing slug unless you pass `--force`.
- `validate` checks the schema. When the REAL geography is built, it also checks every reference
  to provinces and municipalities. It exits with code 5 on problems.

---

## Typical sessions

```bash
# from nothing to a live night
python -m app demo
python -m app election-night --election demo --speed 25

# a new election cycle
python -m app election-night --election 2028 --instant                          # 2028 becomes history
python -m app scenario duplicate midterm-2026 midterm-2030 --seed 20300001 --name "Midterm Election 2030"
python -m app election create --year 2030 --scenario midterm-2030 --simulate    # the copy moved to 2030
python -m app election-night --election 2030 --instant
python -m app history

# analysis and data
python -m app forecast --election 2028 -n 100000
python -m app export --election 2024 --dataset all --format both
```
