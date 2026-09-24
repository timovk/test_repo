# Monte Carlo forecasting

> **These are fictional-model outputs, not predictions.**  Every number produced by
> `app.forecasting` is a SIMULATED estimate (`data_category = "SIMULATED"`): the share of simulated
> elections in which something happens under the invented political model of the NL Federal
> Election Simulator.  The parties, candidates, coefficients and the electoral system are
> FICTIONAL; only the geography (provinces, municipalities, CBS neighbourhoods, demographics) is
> REAL.  Nothing here says anything about real Dutch elections.

Code: `src/app/forecasting/` — `config.py` (schema of `config/forecast.yaml`), `plan.py`
(draw-independent precomputation), `kernel.py` (vectorised formulas), `engine.py` (Monte Carlo
loop, scheduling, `run_forecast`), `result.py` (result types and summaries), `service.py`
(SQLAlchemy persistence; not imported by the package).  `montecarlo.py` re-exports `run_forecast`
under the name used in ARCHITECTURE.md §6.  Engines are pure (NumPy in, dataclasses out).

---

## 1. API

```python
from app.forecasting import run_forecast

result = run_forecast(
    model,                       # StructuralModel (app.simulation.structural)
    races,                       # list[RaceSpec] (app.simulation.races)
    context,                     # ElectionContext (None = from the scenario)
    n_sims=100_000,              # None = config.simulations (10,000)
    seed=2028,                   # 0 ≤ seed < 2**63
    ev_by_province=ev,           # province → EV (default: the contests' electoral_votes)
    constitution=None,           # default: get_constitution()
    holdover_senate={"VLP": 5},  # party → Senate seats not up for election
    poll_shift=shift,            # party → logit shift (polling.blend.shift_from_poll_average)
    workers=0,                   # 0 auto, 1 in-process, ≥ 2 spawned processes
    config=None,                 # ForecastConfig (default: config/forecast.yaml)
    progress=None,               # progress(done, total) after every finished chunk
    ev_method=None,              # EV allocation (default: scenario electoral_college.allocation)
    keep_draws=True,             # keep per-draw arrays on result.draws (never serialised)
)
result.to_dict()                 # JSON-serialisable; result.to_json(); result.fingerprint()
```

`run_forecast` = `prepare_forecast` + `run_plan`.  A runner that re-runs the same election (other
seed, more draws) can prepare once:

```python
plan = prepare_forecast(model, races, context, config=cfg, ev_by_province=ev, poll_shift=shift)
result = run_plan(plan, n_sims=50_000, seed=7, workers=0, config=cfg)  # cfg must match the plan
```

Lower-level pieces: `execute_plan(plan, n_sims, seed, *, workers, chunk_size, progress)`,
`simulate_block(plan, seed, block, n)`, `resolve_workers(requested, n_sims, n_blocks, config)`.
The plan is picklable and holds no reference to the model.

Persistence (`app.forecasting.service`, flushes but never commits):

```python
run = store_forecast(session, election_id, result,
                     race_ids={"PRES-NB": 17, …},                 # race code → race.id
                     line_ids={"PRES-NB": {"lotte-van-der-ploeg": 101, …}, …},  # → ballot_candidate.id
                     scenario_id=None, code_version=None)          # -> SimulationRun (kind 'forecast')
load_forecast(session, run_id) -> dict       # run metadata + full result + relational rows
latest_forecast(session, election_id) -> dict | None
list_forecasts(session, election_id) -> list[dict]
```

---

## 2. Why cells, not neighbourhoods

Simulating 14,729 neighbourhoods × 100,000 draws × 8 parties is ~12 billion logits per step.  It is
also unnecessary: every random component of the simulation model below the municipality is
independent noise (neighbourhood utility noise, neighbourhood turnout noise, binomial and multinomial
counting noise) that averages out within a contest.  The forecast therefore evaluates **cells**:

* **Fragments** — the finest partition such that every fragment lies in one municipality and, for
  every *layer* of races whose jurisdictions do not follow municipal borders (House districts), in
  one race.  On the real country with a generated 150-district plan: **438 fragments** (342
  municipalities + the pieces of split municipalities); the synthetic test country with striped
  districts has 704.
* **Layers** — races of one type with pairwise-disjoint jurisdictions (the 12 province contests;
  the 150 House districts; a Senate class; the governors; mayors).  Two Senate seats of one
  province elected together (founding election) form two layers.
* A layer whose races are unions of whole municipalities (provinces, municipalities) is evaluated
  on **municipality cells** (fragment state aggregated by expected ballots); other layers on
  fragments.  The Electoral College's DISTRICT method forces fragment cells for the presidency.

A `PRESIDENT` race passed together with its `PRESIDENT_PROVINCE` contests is *derived*: the national
popular vote is the sum of the province contests, exactly as in `simulate_election`.

## 3. Methodology

### 3.1 Party state of a fragment (exact at the expectation)

With eligible voters `E_u`, preference shares `s_up`, supporter turnout `q_up` and turnout `T_u` of
the model expectation (`app.simulation.voting.expected_party_state` — structural model + scenario
environment + context shifts, incl. campaign and poll shifts):

```
s̄_fp = Σ E_u s_up / Σ E_u              q̄_fp = Σ E_u s_up q_up / Σ E_u s_up
V_fp = log s̄_fp                        Tq_fp = logit q̄_fp
```

so at zero shocks the fragment's turnout `T̄ = Σ s̄ q̄` and vote shares `π̄ = s̄ q̄ / T̄` are
*exactly* the eligible-weighted aggregates of the unit expectation.  Under shocks (mirroring
`StructuralModel.party_state`):

```
s = softmax_p(V_f + ε_f)      q = clip(σ(Tq_f + g_f ⊙ (τ_f + t_p)), q_min, q_max)
T_f = Σ_p s q                  π_f = s q / T_f          ballots_f = E_f · T_f
```

### 3.2 Matching the shock response (first order)

A single logit reacts more strongly to a shock than the sum of its heterogeneous neighbourhoods
(Jensen's inequality; ≈ 5 % too strong for national shocks and ≈ 11 % for turnout shocks on the
real country without a correction).  The plan therefore computes, per fragment, the exact Jacobian
of the aggregated vote shares with respect to a party-utility shock,

```
∂π̄_p/∂δ_k = ( Σ_u E_u h_u q_up s_up (1[p=k] − s_uk) − π̄_p Σ_u E_u h_u s_uk (q_uk − T_u) ) / Σ_u E_u T_u
```

(`h_u = e_u`, the unit elasticity, for national shocks; `h_u = 1` for local shocks) and maps shocks
through `A_f = argmin ‖J_cell A − J_units‖² + λ‖A − I‖²` (ridge towards the identity for
directions the fragment cannot identify).  Turnout shocks are scaled per party by
`g_fp = Σ E s q(1−q) / (E_f s̄ q̄(1−q̄))` (0 where the turnout probability is clipped).  With these
maps the fragment responds to a +0.1 national VLP shock within 0.3 % of the exact unit-level model.

### 3.3 Error structure and its correspondence to `simulate_election`

Standard deviations default to the simulation model's own values (`errors.*: null` in
`config/forecast.yaml`) — the scenario's `environment.shocks` (ShockSpec) and `config/model.yaml`:

| component | simulation model (`voting.draw_shocks`, per draw) | forecast (per draw) |
|---|---|---|
| national | `national_sd · (√(1−ρ)·t_ν·√((ν−2)/ν) + √ρ·ιξ/‖ι‖)` per party, × `e_u` | identical draw; mapped through `A_nat` (includes `e_u`) |
| events | Bernoulli(`probability`) per event, deviation from the expectation | identical |
| province | `N(0, province_sd²)` province × party | identical |
| municipality + spatial | iid `N(0, municipality_sd²)` + GP `spatial_sd · L z` (Matérn, centroids) | one Gaussian field with covariance `municipality_sd² I + spatial_sd² K` (same distribution) |
| neighbourhood | `N(0, unit_sd²)` unit × party | cell noise `N(0, unit_sd²/n_eff)` per fragment × party (`n_eff` = effective neighbourhoods by eligible voters) |
| turnout | national `turnout_national_sd`, municipal `turnout_local_sd`, unit `unit_shock_share · turnout_local_sd`, party mobilisation `party_turnout_shock_sd` | identical, unit part as cell noise `/√n_eff`, all scaled by `g_fp` |
| race line | `N(0, race_line_sd²)` per race × line; a presidential ticket shares one national shock across all province contests (`race_line_shocks`) | identical scoping |
| ballots / votes | Binomial turnout, Binomial blank / invalid, Multinomial votes | expected counts (sampling noise of thousands of ballots is negligible) |

`errors.scale` multiplies every party-utility error (national, province, municipal, spatial,
cell, race line) — a single knob for "more / less uncertain than the model".  With a poll shift,
`polls.national_sd_scale` (default 1.0) can shrink the national error.

### 3.4 Races on cells (anchored)

Each race's `RacePlan` (`voting.prepare_race`: routing `R`, undervote `a`, independent mass,
candidate / incumbency / home / campaign effects `Δ`, strategic-voting matrix `G`, blank and
invalid rates) is aggregated to its cells (`Δ` and rates weighted by expected ballots) and
evaluated exactly as `RacePlan.shares`:

```
m = π R + indep;  W = log m + Δ + line shock;  sincere = softmax_L(W);  s = max(sincere G, 0)
blank = clip(b0 + (1 − b0 − inv) · π·a, 0, 0.95);   valid = ballots · (1 − inv − blank)
```

followed by a multiplicative **anchor** per cell and line, `ratio = target / pipeline(π̄)`, where
`target` is the unit-level expected line share aggregated with expected valid votes.  The forecast
is therefore centred *exactly* on the model's expectation (tested: with all errors switched off
every race's mean share equals `expected_race_shares` aggregated, to 2·10⁻⁵).

### 3.5 From cell votes to outcomes

Cell votes are summed to race totals (`np.add.reduceat`, cells sorted by race); the plurality line
wins.  Electoral votes per draw: winner-take-all (default), PROPORTIONAL (largest remainder of the
province's EV by vote shares) or DISTRICT (`senators_per_province` EV to the province winner, 1 per
House district to the district's presidential plurality).  An exact tie of continuous vote totals
has probability zero, so no lot is drawn.

* **Outright win**: `EV ≥ majority` (88 of 174; `majority_of(total)` for partial maps).
* **Contingent election**: no ticket reaches the majority.  **EV tie**: the top two tickets hold
  exactly half each (87–87).  **Plurality of EV**: strictly the most electoral votes.
* **Tipping point** (per draw, for the outright winner, else the unique EV leader, else the tied
  top ticket with the most votes): provinces sorted by that ticket's margin over its strongest
  opponent, EV accumulated winner-take-all style; the province reaching the majority.
* **PV/EV divergence**: the national popular-vote plurality winner differs from the EV winner (or
  unique EV leader) — as `electoral_college.ev_pv_divergence`.
* **House / Senate / governors**: seats per party per draw from the district / seat / governor
  winners (independents under `"independent"`); Senate totals add `holdover_senate`.

### 3.6 Poll-informed forecasts

`poll_shift` (party → logit shift, from `app.polling.blend.shift_from_poll_average`, which is
already a precision-weighted Bayesian blend of the poll average with the model) is multiplied by
`polls.weight` and added to the context's `national_shifts`, i.e. scaled by local elasticity
exactly like a national shift in `simulate_election`.  A poll-informed forecast is identical to a
forecast whose context already carries those national shifts (tested).

## 4. What the outputs mean

`ForecastResult.to_dict()` (floats rounded to 6 decimals):

| key | content |
|---|---|
| `data_category`, `disclaimer` | `"SIMULATED"`; the not-a-prediction statement |
| `metadata` | seed, n_simulations, block_size, n_blocks, model_fingerprint, config_hash, input_hash (model + config + races + context + polls + EV map + constitution), scenario, year, election type, national/poll shifts, EV method, cells (units, fragments, layers), resolved error SDs, drawn events |
| `runtime` | workers, chunks, blocks, chunk_size, started/finished, timings (prepare / simulate / summarise), draws per second — excluded from `fingerprint()` |
| `turnout` | national turnout distribution (mean, median, p05, p25, p75, p95) |
| `president.tickets[]` | EV: mean, median, p05/p25/p75/p95, `ev_histogram` (P(EV = 0…174)); `prob_majority` (outright), `prob_plurality`; popular-vote share distribution (mean, median = p50, p05 … p95), `prob_pv_plurality`, `pv_histogram` (0.5-pp bins), `expected_pv_share` (model expectation) |
| `president` | `prob_contingent`, `prob_ev_tie`, `prob_pv_ev_divergence`; `provinces` (EV, win frequency per ticket, mean share, tipping-point frequency); `tipping_point` (frequency per province) and `tipping_point_by_ticket` (conditional on that ticket being the winner / leader); `combinations` (most common province → ticket maps with frequency and EV per ticket); `municipalities` (per municipality and ticket: mean, p05, p95 share and `lead` = how often the ticket carries the municipality) |
| `house`, `senate` | per party: seat distribution (mean, median, percentiles, histogram 0 … seats), `prob_majority` (≥ 76 / ≥ 13), `prob_plurality`, holdover; `prob_no_majority`; `race_win` (race → party → probability); Senate `compositions` (most common chamber compositions) |
| `governors` | win-count distribution per party and `race_win` |
| `races` | per race (input order, national `PRES` first): per line win probability, share mean / p05 / p50 / p95, mean votes, `expected_share`.  For the derived `PRES` race the win probability is the *outright* Electoral College majority (the remainder is the contingent probability).  For multi-seat proportional races (councils) it is the probability of the most votes (seat allocation is not forecast). |

Quantiles of per-draw scalars (EV, seats, national shares, turnout) are exact; race-line and
municipality share quantiles come from 400-bin histograms (≤ 0.25 pp resolution).

## 5. Persistence

`store_forecast` writes one `simulation_run` (kind `forecast`, seed, n_simulations, config hash,
timing, `summary_json` = `result.to_json()`) plus:

* `forecast_summary` — per race × ballot line (win probability, mean / p05 / p50 / p95 share,
  mean votes);
* `forecast_distribution` — non-empty histogram bins: `ev` and `popular_vote_share` per ticket,
  `house_seats`, `senate_seats`, `governor_wins` per party;
* `forecast_aggregate` — `ev` and `popular_vote` per ticket, `house_seats`, `senate_seats`,
  `governor_wins` per party: mean, median, p05, p25, p75, p95, `prob_majority`, `prob_plurality`;
* `forecast_combination` — top Electoral College maps (`GR:PA|FR:NVB|…`, party codes when unique)
  with `ev_json`.

Ticket keys in the distribution / aggregate tables are the `ballot_candidate.id` of the ticket on
the national `PRES` ballot (as a string), falling back to the party code.  Bulk inserts use
`insert(Model)` with lists of dicts in chunks of 5,000.

## 6. Validation

* **Exactness at the expectation** (unit test): with all errors off the forecast reproduces every
  race's model expectation.
* **Kernels** (unit tests): `line_shares` equals `RacePlan.shares` and `fragment_state` equals
  `StructuralModel.party_state` to 1e-10; the Jacobian formula matches finite differences.
* **Against full `simulate_election` draws** (synthetic country, 400 presidential draws; slow test:
  200 complete general elections).  Documented tolerance: mean shares within 4 standard errors of the
  simulation mean + 0.3 pp; win probabilities within 4 standard errors + 3 points; national share SD
  ratio in [0.75, 1.33]; expected EV within 4 SE + 1.5; House seat means within 4 SE + 1.5 seats.
* **REAL country, demo-2028 presidency** (2,400 `simulate_election` draws vs a 20,000-draw forecast):
  province share means differ by 0.02 pp on average (max 0.09 pp); national share SD ratio
  0.985–1.010; province win probabilities differ by 0.25 points on average (max 2.8, within Monte
  Carlo error of the simulation sample); outright VLP / PA 0.488 / 0.381 vs 0.481 / 0.385;
  contingent 0.131 vs 0.134; expected EV within 0.5.

## 7. Performance design

* One-time plan (≈ 0.1–0.8 s): race plans, fragments, cell aggregation, anchors, Jacobian maps,
  the municipal Cholesky factor.  The plan (a few MB, float32) is sent once to every worker.
* Per block of `block_size` (500) draws, cell-major float32 arrays `(cells, draws, parties|lines)`:
  per-cell linear maps are stacked matrix products (one small product per cell), group sums are
  `np.add.reduceat` over contiguous cells, softmaxes need no max-subtraction (bounded logits) and
  short-axis reductions are slice loops.  Only race totals, winners and small per-draw vectors leave
  a block; shares are summarised through histograms.
* Multiprocessing: `ProcessPoolExecutor` with the *spawn* context, workers initialised with the
  plan, single-threaded BLAS in workers (`OPENBLAS/OMP/MKL_NUM_THREADS=1`), chunks of whole blocks
  scheduled ~3 per worker; a broken pool falls back to in-process computation (same results).
  `workers = 0` uses `NLFED_FORECAST_WORKERS` or the available CPUs, and stays in-process below
  `parallel_min_simulations` (20,000).
* Measured (4 shared CPUs, other jobs running): REAL country, President + 150 House races —
  ≈ 2,100 draws/s in one process, **100,000 draws in 14.3 s** with `workers=0` (4 processes;
  44 s under heavy load from other jobs); synthetic country, complete general election (183 races):
  ≈ 1,500 draws/s in one process, 20,000 draws in 5.3 s.  Result JSON ≈ 0.47 MB (0.31 MB without
  municipality distributions).

## 8. Reproducibility

Block `b` draws every random number from streams of `derive_seed(seed, "forecast", b)`
(`make_rng(block_seed, "national" | "province" | "municipality" | "cell" | "turnout-…" | "line", …)`)
and is always evaluated as one array of the same shape.  Integer counters are summed (order-free),
floating-point sums are kept per block and added in block order, per-draw arrays are concatenated in
block order.  Hence the same model, races, context, configuration and seed give an identical
`ForecastResult.fingerprint()` for any `chunk_size`, number of workers or completion order (tested:
1 vs 2 workers and two chunk sizes).  `block_size` is part of the configuration hash; `workers`,
`chunk_size` and `parallel_min_simulations` are not.  Bit-identity across worker counts relies on
matrix products whose results do not depend on the BLAS thread count (workers run single-threaded
BLAS, an in-process run may use several threads); this holds for the OpenBLAS bundled with NumPy
(verified for the shapes used here), not necessarily for MKL without its reproducibility mode.

## 9. Limitations

* The forecast is a **first-order approximation** of `simulate_election`: fragment heterogeneity
  is matched to first order (Jacobians) and exactly at the expectation, not for very large shocks;
  counting noise (binomial / multinomial) is replaced by expected counts; neighbourhood noise enters
  as cell noise.  Mean shares agree to ~0.1 pp and spreads to ~1–2 % (§6).
* The forecast quantifies the **model's own uncertainty** (its shock SDs), not uncertainty about the
  model itself; `errors.scale` is the knob for that.
* Contingent elections are counted, not resolved (who would win the contingent procedure is not
  forecast).  Exact ties are not modelled (probability zero for continuous totals).
* Multi-seat proportional races (councils) report only who gets the most votes, no seat
  distributions.
* Strategic voting uses the model's pre-election expectation (as in the simulation), so it does not
  react to the draw.
* Poll information enters only as a national shift per party (no province- or district-level poll
  conditioning).
