# Election night and race calling

Everything on this page is **SIMULATED**. The reporting process, the counting speeds, the
race calls and the Electoral College totals are made by a model. They are not a description
of how real Dutch votes are counted. The code is in `src/app/reporting/`:

| Module | Role |
|---|---|
| `config.py` | `NightConfig`, the schema of `config/night.yaml` |
| `timeline.py` | `generate_timeline(...) -> Timeline`, the reporting events of the night |
| `calling.py` | `RaceProgress`, `RaceCaller.evaluate(...) -> CallDecision` and `allocate_counted` |
| `live.py` | `NightEngine`, which replays the night and keeps tallies, calls, snapshots and details |
| `clock.py` | `PlaybackClock`, which maps wall-clock time to simulated time and then to `seq` |

The engines are pure: they take NumPy/pandas input and return dataclasses, and they never
import SQLAlchemy. Services persist their output in `reporting_event`,
`reporting_event_unit`, `race_call` and `night_session`.

---

## 1. Timeline model

`generate_timeline(frame, ballots_per_unit, config, seed, units_mask=None, *, election_date=None, unit_wijk=None)`
turns the final ballots per unit (CBS buurt ≙ precinct) into *reporting events*. Each event
is one batch of counted ballots from one municipality.

**Poll closing.** The default closing time is `polls_close.default` (21:00 Europe/Amsterdam).
It can be overridden per province or per municipality; a municipality override wins over a
province override. `sim_time_s = 0` is the earliest closing time among the reporting
municipalities (`Timeline.reference_close`). A local clock time is computed by converting
through UTC, so it stays correct across a DST change during the night.
`polls_close_times(frame, config, date, mask)` recomputes the reference for services.

**First report.** Each municipality *m* with *B* ballots and ballot-weighted urbanity *u*
(0 = rural … 4 = very urban) sends its first batch this many minutes after its own poll
closing:

```
first = (base + per_log10 · log10(B / size_ref) + per_urbanity · u + province_effect)
        · exp(N(0, jitter_sd))                        clipped to [min_minutes, max_minutes]
```

The province effect is a fixed FICTIONAL value in minutes plus a seeded random term,
`N(0, province_random_sd_minutes)`. With the defaults the Wadden islands report at about
21:25–21:35. A typical municipality of about 20 k ballots reports first at about 22:00–22:20,
and the big cities at about 22:30–23:15.

**Counting duration.** The time from the first batch to the last is
`base · (B/ref)^elasticity · (1 + urbanity_factor · u) · exp(N(0, jitter_sd)) · province factor`.
It is zero for single-batch municipalities. `latest_end_minutes` is a soft cap: a scheduled
end past the cap is pulled back to somewhere in the last `end_spread_minutes` before it. On
the real 2025 geography with the defaults the night ends at about 04:30, and the big cities
finish between 02:30 and 04:30.

**Batches.** The number of batches comes from the size tier (`batches_by_size`): a small
municipality has 1–2 and a city with more than 300 k ballots has 30–60. Batch sizes are
Dirichlet-distributed (`batch_size_concentration`). Units are reported one wijk at a time
(neighbourhood clusters, taken from the CBS code `BU gggg ww bb` or passed as `unit_wijk`). The
order of the wijken is shuffled with a seed, and a batch cut within `wijk_snap_tolerance` of a
wijk boundary snaps to that boundary, so most batches contain whole wijken. On the real
geography the defaults produce about 2,400 events.

**Partial precincts.** A unit with at least `partial_precinct.min_ballots` ballots that
crosses a batch cut is split across batches. Pieces smaller than `min_piece_fraction` are
merged into the neighbouring batch.

**Zero-ballot units** are still reported (fraction 1.0) in the batch that covers them, so
every unit reaches 100 %.

**Exactness.** Each unit's cumulative fraction after each of its batches is stored as a
multiple of 2⁻²⁰. That makes the increments exact binary fractions: they sum to exactly 1.0,
the last batch of every unit has cumulative **exactly 1.0**, and a database round trip
(`to_event_rows` / `to_unit_rows` → `Timeline.from_records`) reproduces the timeline
bit for bit. The CSR arrays (`unit_ptr`, `units`, `increments`, `cumulative`) and a
checkpoint every `checkpoint_every` events give fast random access through
`fraction_after(seq)`.

**Determinism.** Each municipality draws from its own stream,
`make_rng(seed, "timeline", "muni", code)`, and each province from
`make_rng(seed, "timeline", "province", code)`. Events are sorted by
(time, municipality index, batch) and numbered `seq = 1..N`.

## 2. Reporting statistics

* **Counting rule.** After an event, the counted votes of a unit are `floor(f × final)` for
  each line (`allocate_counted`). Blank and invalid ballots are counted the same way, and
  counted ballots are their sum. So counted ≤ final always, counted == final exactly when
  `f == 1`, and counts never decrease.
* **Reporting %** is always measured in *expected* ballots (expected turnout × eligible):
  `R = Σ f·x / Σ x`. This avoids revealing actual turnout. Snapshots also give precincts
  (units) reported and partial, municipalities reporting and complete, and the
  municipality's fraction after each batch.
* **Leader and lead changes.** The counted leader is updated after every event; a tie keeps
  the previous leader. A `LeadChange` is recorded whenever the leader changes.

## 3. Race states

| Status (`RaceStatus`) | Meaning |
|---|---|
| `POLLS_CLOSED` | Nothing has been counted yet. This is the state at `seq 0`, and at 0 % reporting with the default `call_at_poll_close: false` (we use `POLLS_CLOSED`, not `TOO_EARLY_TO_CALL`, for 0 %). |
| `TOO_EARLY_TO_CALL` | Some votes are counted, but little is reported and uncertainty is high. |
| `LEAN` | The line with the highest win probability has ≥ `lean_threshold` (0.80). This is not a call. |
| `TOO_CLOSE_TO_CALL` | Either the expected final margin is inside `close_band_pct` (1 pp) with ≥ `min_reporting_projection` reported, or ≥ `too_close_min_reporting` (50 %) is reported without any threshold being met, or a call was retracted. Once `TOO_CLOSE`, a race never goes back to `TOO_EARLY`. |
| `PROJECTED_WINNER` | Win probability ≥ `projected_threshold` (0.995) and reporting ≥ `min_reporting_projection` (5 %). |
| `CALLED` | Win probability ≥ `called_threshold` (0.9995) and reporting ≥ `min_reporting_call` (15 %), **or** mathematical certainty, **or** an uncontested race with results. |
| `FINAL` | 100 % of units are reported and the result is outside the recount rule. |
| `RECOUNT` | 100 % of units are reported and the recount rule applies (§8). |

The rules are checked in this order. The first one that applies decides the status:

1. A manual override is active → keep it (§7).
2. 100 % reported → `FINAL` or `RECOUNT`. This always wins, even over a manual call.
3. The prior state is `PROJECTED` or `CALLED` → sticky (§6).
4. No results yet → `POLLS_CLOSED`. With `call_at_poll_close: true`, a call can be made from
   the expectation alone (`basis = poll_close`).
5. Mathematically certain → `CALLED` (`basis = mathematical`).
6. `CALLED` → `PROJECTED` → `TOO_CLOSE` (close band) → `LEAN` → `TOO_CLOSE` (substantial
   reporting) → `TOO_EARLY`.

`LEAN` has hysteresis: an existing LEAN for the same line is kept while its probability stays
above `lean_threshold − lean_hysteresis`. A race is **never called just because a candidate
leads**. Every call needs a win probability over the counted *and* projected outstanding
vote, a minimum amount of reporting, or strict mathematical certainty.
`DECIDED_STATUSES` (PROJECTED, CALLED, FINAL) is the set whose winner counts for EV and seat
tallies. `RECOUNT` does not count.

## 4. Calling algorithm

`RaceCaller(config, recount_check=None).evaluate(progress, seed, seq, prior)` sees only a
`RaceProgress`: the counted votes and counted ballots per unit, the reported fraction `f`,
the pre-election **expected** shares `e` and expected ballots `x`, eligible voters, and the
cluster of each unit (its municipality). It never sees final results for unreported ballots,
and the test suite checks this.

1. **Swing (log-linear).** For the reporting clusters *m*, let `C_m` be the counted valid votes
   and `Ê_m` the expected composition of exactly those ballots. `δ` solves
   `Σ_m C_m·softmax(log Ê_m + δ) = counted totals`. This is a multinomial-logit swing, the
   same functional form as the vote model, found by iterative scaling in about 6 iterations.
   A normal prior `N(0, swing_prior_sd²)` shrinks it:
   `k = s²/(s² + v_data)`, `δ̂ = k·δ_raw`, with
   `v_data = cluster_sd²·H_clusters + unit_sd²·H_units + 1/(C·p)`. Here `H` is the Herfindahl
   index of counted votes over clusters or units, so the prior tightens as more — and more
   balanced — clusters report. The posterior variance is `k·v_data`.
2. **Cluster residuals.** A partly reported municipality also gets its own residual,
   `ρ_m = k_m·log(obs_m / pred_m)`, with `k_m = cluster_sd²/(cluster_sd² + v_m)`. It is applied
   to the municipality's uncounted units.
3. **Turnout ratio.** `τ = exp(k_τ·log(counted ballots / expected ballots of the counted part))`,
   shrunk with `turnout_prior_sd`. The valid-vote rate is estimated from the counted ballots.
4. **Outstanding vote.**
   * Unreported units: `τ · valid_rate · x_u`, with composition `softmax(log e_u + δ̂ + ρ_m)`,
     aggregated per cluster.
   * The uncounted remainder of partly counted units: `counted·(1−f)/f`, with the observed
     composition.
5. **Simulation.** The final totals are `T = counted + outstanding`, linearised around the
   projection, so each draw costs O(L²) no matter how many units the race has:
   * race swing: Student-t (`swing_tail_df`), SD `sqrt(posterior + differential_sd²)`, where
     `differential_sd` covers reported areas not being representative of outstanding ones;
   * race turnout: `sd_log τ`;
   * cluster noise (`var ρ_m`) and unit noise (`unit_sd²` × Herfindahl of outstanding units)
     per outstanding cluster;
   * turnout noise per cluster (`turnout_cluster_sd`);
   * composition noise of partial remainders (`partial_sd`).

   Draws are clipped at the counted votes. The RNG is `make_rng(seed, "call", race_key, seq)`.
   **Win probability = share of draws won.**
6. **Adaptive draws.** Start with `n_draws_min` (500). Go to `n_draws` (4,000) when a threshold
   that could change the status now is within 3 Monte-Carlo SE. Go to `n_draws_max` (16,000)
   only to *confirm* that a threshold ≥ projected is met. Thresholds that cannot apply —
   below the reporting minimums, or when no results are in — never trigger more draws.
7. **Mathematical certainty.** The counted lead is larger than
   `Σ_{units with f<1} max(eligible − counted ballots, 0)`, which is every ballot that could
   still exist. Win probability is then exactly 1 for the leader.
8. **Comeback.** For the counted runner-up the engine reports: the deficit; the net margin it
   would need on the estimated outstanding valid vote; the 99.5th percentile of its simulated
   net gain ("plausible max"); whether a comeback is plausible; and whether it is
   mathematically possible.

**Calibration.** In the tests, 60 seeded races (province- and district-sized, expected margins
within ±10 pts, final results drawn from the expectation plus a race-level swing of SD 0.06
plus municipality and unit noise) produced ≥ 50 projections/calls, all correct. Over 4 seeds
it was 388 calls with 0 wrong. The rule is ≥ 99 %. Full synthetic nights and a night on the
real 2025 geography (182 races) made no wrong call.

## 5. Evidence snapshots

`CallDecision.evidence` is a JSON-serialisable dict, stored in `race_call.evidence_json` for
every state change. It contains:

* `method`, `data_category: "SIMULATED"`, `race_key`, `seq`, `rng_stream`, `basis`, `status`,
  `key`, `prior`
* `retracted` / `retracted_key`
* `reporting`: % of expected ballots, units reported/partial, clusters reporting/outstanding
* `counted` (votes per line, valid, ballots) and `leader` (margin in votes and pp)
* `expected` (race shares, expected ballots)
* `turnout` (raw and shrunk ratio, shrink, SD) and `valid_rate`
* `swing` (raw, estimate, shrink, posterior SD, SD used, differential SD, tail df)
* `outstanding` (estimated ballots, unreported expected ballots, partial remainder, strict
  upper bound, estimated valid)
* `projection` (mean votes; share mean, p05 and p95; margin mean; margin SD)
* `win_probability` and `model_win_probability`, `n_draws`, `math_certain`
* `comeback`
* `thresholds`

For `FINAL`/`RECOUNT` it has `final` (winner, runner-up, margin, tie) and `recount`
(required, reason, fallback margin) instead. The evidence is built lazily on first access,
so evaluations that are never persisted stay cheap.

## 6. Stickiness and retraction

Once a race is `PROJECTED` or `CALLED` for line *k*, it keeps that call for as long as *k*'s
win probability stays ≥ `retraction_threshold` (0.90), or *k* is mathematically certain. A
`PROJECTED` race is upgraded to `CALLED` when the call rules are met. If *k*'s probability
drops below 0.90, the race becomes `TOO_CLOSE_TO_CALL` with `basis = "retraction"`,
`evidence.retracted = true` and `retracted_key = k`, and it can be called again later. The
test suite forces this with an adversarial reporting order: every early precinct favours A far
beyond expectation, and every late one favours B.

The national `PRES` state is derived rather than called. It becomes `CALLED` when a ticket's
decided EV reaches `constitution.presidential_majority`. It becomes `FINAL` when all province
races are locked and the winner's EV from `FINAL` provinces reaches the majority. It is
retracted (`TOO_CLOSE`, `retracted`) if a province retraction drops the winner below the
majority. It is `TOO_CLOSE` with `contingent_election_likely` when no ticket can reach the
majority even if it won every undecided province it is on the ballot in.

## 7. Manual overrides (contract for services)

A producer can override a race at any `seq`: `NightEngine.apply_manual_call(race, status, key, reason)`.
`clear_manual_call(race)` removes the override.

* The override is published with `CallRecord.is_manual = True` and `override_reason`. Services
  store it in `race_call.is_manual` / `override_reason`.
* While the override is active, the model keeps evaluating the race: `decision(race)` holds
  the model's opinion, and `evidence.manual_call_at_risk` flags a manual winner whose model
  probability is below the retraction threshold. The published state does not change.
* Reaching 100 % always replaces a manual state with `FINAL`/`RECOUNT`.
* Clearing re-evaluates the race at once. The model treats the former manual state as its own
  prior, so a manual call it still believes in stays called, and one it does not believe in is
  retracted.
* **Replay.** Overrides are part of the deterministic content. Every override is a
  `ManualCall(race_key, seq, status, key, reason)` and is listed in `engine.manual_calls`.
  Services persist these, for example from the `race_call` rows with `is_manual`, and pass them
  as `NightEngine(..., manual_calls=...)` when they rebuild a night. The engine re-applies each
  one right after its `seq` is applied.
* `superseded` in `race_call` is for services: the latest row per race is the current state.

## 8. Recount status

At 100 % reporting the final tally goes to `recount_check(RecountInput)`, when one is injected
(for example a wrapper around `app.elections.recount.needs_recount`). The check returns either
a bool or `(bool, reason)`; a reason is stored in the evidence as `"recount_check: <reason>"`.
`RecountInput` holds the race key, the race type (`RaceMeta.race_type`, filled in by the night
engine), line keys, totals, valid and ballots counts, the winner and runner-up, and the margin
in votes and pp. Without an injected check, the fallback is
`margin_pct ≤ recount.margin_pct` (0.25 pp). An exact tie at the top always leads to
`RECOUNT`; win probability is then split between the tied lines and the key is `None`.
`RECOUNT` is not a decided status, so its EV and seats stay uncalled until services run the
recount (`app.elections.recount`) and record the outcome.

## 9. National tallies

* **President.** Line keys (tickets) are shared by all `PRESIDENT_PROVINCE` races. For each
  ticket the engine reports:
  * decided EV (province race in `DECIDED_STATUSES`);
  * leading EV (undecided race, counted leader);
  * maximum possible EV;
  * the uncalled EV;
  * the first `seq` at which decided EV ≥ `presidential_majority`
    (`majority_reached_at_seq`, and a `PRES` `CallRecord`);
  * the contingent-election flag, with the first `seq` it was set.

  The popular vote adds up the counted and projected votes of the province races.
* **House.** Per party: called, leading, total, incumbent seats, and net change against
  `incumbent_party` for called seats and for called + leading. The engine records the first
  `seq` at which a party's called seats reach `house_majority`.
* **Senate.** Holdovers + called (decided) and + leading (projected). Control is reached when
  holdover + called ≥ `senate_majority`.
* **Governors.** One status, leader, called key and party per race.

Constitutional numbers always come from `get_constitution()`.

## 10. Evaluation schedule and performance

After each event, only the races whose units are in that event are recounted. Counted votes
and leaders are updated on every event. A touched race is **re-evaluated** in these cases:

* its first results arrive;
* it reaches 100 %;
* its reporting has grown by `min_eval_increment_pct` (0.5 pp) since its last evaluation;
* for races already `CALLED`, which only need retraction checks, by
  `called_eval_increment_pct` (5 pp).

`FINAL`/`RECOUNT` races are locked. The schedule depends only on the event sequence, so it is
deterministic.

Measured on the shared 4-CPU development box:

| Night | Events | Races | Full night | Median event | 95th percentile event |
|---|---|---|---|---|---|
| Real 2025 geography (14,729 units, 342 municipalities) | 2,427 | 182 | 7.4 s | 2 ms | 9 ms |
| Stress test (14.7 k units) | 12.9 k | 182 | ≈ 17 s | – | – |

## 11. Determinism and playback

The content at a given `seq` is a pure function of:

* frame, timeline, races, race metadata, config, seed and manual calls;
* the event sequence.

Stepping one event at a time, jumping with `advance_to_seq`, `advance_to_time`, or rewinding
(which resets and replays) all give identical snapshots and histories. The tests check this.

`PlaybackClock` maps wall-clock time to simulated time:

```
target_sim_time(now) = anchor_sim + (now − anchor_wall) × speed × base_rate
```

`base_rate` is 60 simulated seconds per real second at 1×. The allowed speeds are
1, 2, 5, 10 and 25. The clock has four states: `ready → running ⇄ paused → finished`.

* `set_speed` re-anchors, so simulated time does not jump.
* `step()` moves to the next event time and pauses.
* `seek` and `finish` move to a given time or to the end.
* `tick(now)` finishes the clock when it reaches the end.
* `to_dict`/`from_dict` store the state; `night_session` has `status`, `speed`, `sim_time_s`
  and `current_seq`.

`target_seq(now)` is passed to `NightEngine.advance_to_seq`. Speed changes *when* content
appears, never *what* appears: at 1× the real night plays in about 7.5 minutes, and at 25×
in about 18 seconds.
