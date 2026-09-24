# Polling

Every pollster in this application is **FICTIONAL** (invented names) and every poll is
**SIMULATED** (`data_category = "SIMULATED"`, `poll.is_fictional = true`). No real polling
organisation is modelled or named. The configuration schema rejects names of real pollsters
(for example Peil.nl, Ipsos I&O, Verian or EenVandaag) as whole-word matches.

The package `app.polling` has three pure engines and one persistence service:

| Module | Purpose |
|---|---|
| `polling/config.py` | Pydantic schema of `config/polling.yaml`: pollsters, aggregation and generation settings |
| `polling/generate.py` | generates fictional polls from a simulated "true" opinion |
| `polling/aggregate.py` | poll averages: weights, house effects, trend line, uncertainty |
| `polling/blend.py` | conditions the structural model on the poll average (Bayesian, logit space) |
| `polling/service.py` | SQLAlchemy persistence (`pollster`, `pollster_house_effect`, `poll`, `poll_result`) |
| `polling/types.py` | poll types, frame schemas, race ↔ poll-group mapping |

The pure engines never import SQLAlchemy. `import app.polling` loads only the pure engines;
import `app.polling.service` explicitly when you need the database.

## 1. Poll schema

### Poll types and geography

| `poll_type` | `geo_code` | Race polled |
|---|---|---|
| `national_president` | `NL` | `PRES` (national popular vote) |
| `province_president` | province code, for example `NB` | `PRES-NB` (the province's electoral-vote contest) |
| `house_district` | district code, for example `NB-07` | `HOUSE-NB-07` |
| `senate` | province code `NB` (or seat code `NB-1`) | `SEN-NB-<class>` |
| `governor` | province code | `GOV-NB` |
| `generic_house` | `NL` | none; the national party vote for the House |
| `favorability` | `NL` | none; keys such as `favorable` and `unfavorable` |

`race_code_for(poll_type, geo_code)` and `poll_group_for_race(race_code)` convert between the
two. A province-level Senate poll has no race code because the class is not part of a province
code. When exactly one `SEN-<PV>-*` race exists, the service links the poll to it.

### Engine frames (input of `aggregate_polls`)

`polls` has one row per poll:

| column | notes |
|---|---|
| `poll_id` | unique id (database id, or a sequence number for generated polls) |
| `pollster` | pollster name |
| `poll_type`, `geo_code` | see the table above; a missing `geo_code` means `NL` |
| `start_date`, `end_date` | fieldwork window; recency and the trend use `end_date` |
| `sample_size` | total respondents |
| `population` | `LV` (likely voters), `RV` (registered voters) or `A` (adults) |
| `method` | `phone`, `mixed`, `panel`, `online`, `ivr` or any other label |
| `undecided_pct` | percentage of respondents who are undecided (nullable) |
| `quality_rating` | manual weight override for one poll (nullable) |
| `pollster_rating` | optional; the pollster's rating from the database |

`results` has one row per poll and option, with columns `poll_id, key, value_pct`. `key` is a
party code or ticket key, and `value_pct` is the percentage of *all* respondents. The values of
one poll plus `undecided_pct` sum to about 100.

### Database

`pollster` stores `name`, `rating`, `rating_label`, `default_method` and `is_fictional`.
`pollster_house_effect` stores the prior lean of a pollster toward a party, in percentage
points. `poll` stores one row per poll, linked to its `election`, `race`, `province` and
`district_code`. `district_code` holds any sub-province geo code (a House district such as
`NB-07` or a Senate seat such as `NB-1`); it also holds a province code when that province has no
`province` row, so the geography of a poll is never lost. `poll_result` stores one row per
option, with `party_id` when the key is a party code; the key itself is always kept in `label`.

Service API:

* `upsert_pollsters(session, specs, party_code_to_id) -> dict[name, Pollster]`: insert or update
  pollsters by name and replace their house-effect priors. Party codes that are not in the
  mapping are skipped.
* `store_polls(session, election_id, polls, party_code_to_id, race_code_to_id=None, province_code_to_id=None, *, pollsters=None) -> list[Poll]`:
  stores generated polls, or any objects with the same attributes. Every poll is validated
  before anything is written (known poll type, `LV`/`RV`/`A` population, positive sample size,
  geo codes and result keys that fit their columns); free-text methods longer than the column are
  shortened. Province ids come from `province_code_to_id`, else from the `province` table.
  Pollsters that are not in the database yet are created from the matching entry of `pollsters`
  (for example the scenario's pollsters), else of `config/polling.yaml`, else with rating 1.0.
  This matters because the aggregate prefers the stored rating over the configured one.
* `polls_frames(session, election_id, poll_type=None) -> (polls_df, results_df)`: returns the
  engine frames above, plus `margin_of_error`, `pollster_rating`, `race_id` and `is_fictional`.
* `load_pollster_configs(session) -> list[PollsterConfig]`: returns the database ratings and
  priors (a stored rating outside 0–5 is clamped, with a warning). Pass the result as
  `pollsters=` to `aggregate_polls`.

The service functions flush but never commit; the caller owns the transaction.

## 2. Pollster ratings are data

How much a pollster is trusted is a subjective judgement, so it is **data, never code**. You
can edit it in `config/polling.yaml`, in a scenario's `polling.pollsters`, or in the `pollster`
table. Each pollster has these fields:

* `rating`: aggregation weight multiplier (1.0 is average, 0 excludes the pollster from every
  average). `rating_label` (A+ … C) is for display only.
* `method`, `typical_sample`, `population`: the defaults used for generation.
* `house_effects`: prior lean in percentage points per party code. Parties that are not listed
  get a prior of 0; the aggregate can still estimate an effect from the data.
* `activity`: relative share of the generated polls; `poll_types` limits which types the
  pollster fields; `undecided_offset_pp` shifts its reported undecided share.

The shipped configuration has nine invented pollsters, including Polderpeil, Kompas Research,
Deltametrie, Lage Landen Panel, Horizon Opinie, Randstad Survey Group, Waddenzee Analytics,
Duinzicht Data and Grachtengordel Onderzoek. Their house effects are expressed for the demo
party codes `PA, SAP, VLP, DM, CVU, NVB, PLB, RV`. Scenario pollsters (`PollingSpec.pollsters`)
replace the configured list when a scenario provides any (see `resolve_pollsters`).

## 3. Generation (`generate_polls`)

```python
generate_polls(truth, pollsters, spec, election_date, seed, competitiveness=None, *, config=None)
    -> GeneratedPolls  # a Sequence[GeneratedPoll] with .frames() and audit data
```

`truth` maps each `(poll_type, geo_code)` to the true shares on election day, for example the
model's expected shares. Options with a zero share (withdrawn lines, parties not on the ballot)
are not polled, and groups with fewer than two positive options (uncontested races) are skipped
with a warning; their polls go to the other geos of the same poll type. Negative or non-finite
shares raise `ValueError`. `spec` is the scenario's `PollingSpec`, or a mapping
`{poll_type: count}`. The model has six parts.

1. **True opinion path.** Opinion is a path in multinomial-logit space, indexed by days before
   the election. For each key, one random-walk *bridge* is shared by all poll groups (national
   movement), and a smaller walk is specific to each group. Each bridge is pinned to 0 on
   election day and to a random offset `N(0, start_offset_sd)` at campaign start. The shares on
   day *d* are `softmax(log truth + walk(d))`, so they equal the truth on election day and wander
   plausibly before it.
2. **Industry-wide correlated error.** One draw `z_k ~ N(0, 1)` per key is shared by **every
   poll of the run** and applied as a logit shift of `z_k × true_polling_error_sd / 0.25`. At a
   50 % share this has an SD of `true_polling_error_sd` share points. A smaller province error
   (`geo_error_fraction × sd`) is shared by all polls in the same province, across the presidential,
   Senate, governor and district polls there. This is why a polling average can miss in the same
   direction everywhere.
3. **House effects.** A pollster's true lean is its configured prior plus seeded jitter
   (`house_effect_jitter_pp`). The lean is applied in share space as
   `q + h − Σh·q`, which keeps the total at 1.
4. **Sampling.** The sample size is `typical_sample × sample_factor[poll_type] × lognormal`,
   rounded to tens. The decided respondents are drawn **multinomially** from the biased shares,
   with an effective size of `n × (1 − undecided) / design_effect[method]`.
5. **Undecided share.** It declines from `undecided_start_pct` at campaign start to
   `undecided_end_pct` on election day. It is adjusted by the pollster offset and by population
   (`A` > `RV` > `LV`), plus noise. The reported values are the decided shares × (100 − undecided).
6. **Fieldwork and allocation.** Fieldwork windows last 2–5 days and end at least one day before
   the election. End dates follow `Beta(1, late_bias)` in time-to-election, so polls cluster late
   in the campaign. Each poll type's count is spread over its geos with weight
   `1 + boost × competitiveness^power`, so competitive provinces and districts get more polls.
   Competitiveness comes from the argument, or from the truth's top-two margin when it is omitted
   (or not finite).

The **margin of error** is `1.96 × sqrt(0.25 × deff / n) × 100`, in points at a 50 % share.

**Determinism.** Every random component has its own `make_rng(seed, "polling", …)` stream,
keyed by key, province, pollster or (poll type, geo, index). Identical inputs therefore give
identical polls, independent of dictionary order and of the order in which pollsters are listed.
The result also records the audit data: `industry_error`, `geo_error`, the realised
`house_effects`, `allocation`, and each poll's `true_shares`.

**Audit data reveals the truth.** `true_shares`, `industry_error` and `true_industry_error_pp`
are derived from the election-day truth. When the truth is the simulated result, they give away
the outcome. They are never stored by `store_polls` and must not be shown by the API or UI
before the election is `FINAL`. The polls themselves (`frames()`, the `poll` tables) are safe to
show.

## 4. Aggregation (`aggregate_polls`)

```python
aggregate_polls(polls, results, config, as_of, group_by=("poll_type", "geo_code"), *, pollsters=None)
    -> dict[tuple, PollAverage]
```

The average for each group is built in seven steps.

1. **Selection.** `poll_id` must be unique, otherwise `ValueError` is raised. Polls whose
   fieldwork ends after `as_of`, or earlier than `max_age_days` before it, are ignored, so on
   election night nothing that ends after `as_of` can influence the mean, trend or house effects.
   Several kinds of poll are also dropped:
   * polls without any positive reported value (for example 100 % undecided);
   * polls whose configured weight is 0: a pollster or `quality_rating` of 0, or a population or
     method weight of 0. This is how a poll or pollster is deliberately excluded.

   Options with no weighted information are removed rather than averaged to NaN; this happens
   when every poll that asked about them has a recency weight that underflowed to 0. Groups with
   fewer than `min_polls` remaining polls are omitted. Population labels are case-insensitive. If
   no result row matches a poll (for example string vs integer `poll_id`), a warning is logged.
2. **Undecided handling** (`undecided`). `proportional` (the default) rescales the options by
   `100 / (100 − undecided_pct)`, which reallocates the undecided in proportion and keeps the
   ratios between options. `normalize` rescales each poll to sum to 100. `keep` uses the raw
   values. Poll types in `undecided_exempt_poll_types` (favorability) are never rescaled.
3. **Weights.** Every factor is listed in the `PollAverage.weights` transparency table:

   | factor | formula |
   |---|---|
   | recency | `0.5 ** (age_days / recency_half_life_days)` |
   | sample | `(min(n, sample_size_cap) / sample_size_cap) ** sample_size_exponent` |
   | rating | `rating ** rating_exponent`, where the rating is the poll's `quality_rating`, else the database `pollster_rating`, else the configured rating, else `default_rating` (the first non-negative value wins; 0 excludes the poll) |
   | population | `population_weights[LV/RV/A]` |
   | method | `method_weights[method]` |
   | volume | `1 / max(1, recency-weighted poll count of the pollster) ** pollster_volume_exponent`, which stops one pollster from flooding the average |

4. **House effects** (`house_effects`). The estimate is iterated:
   * compute the trend line of the house-adjusted values, evaluated at each poll's end date;
   * take the residual of the raw value from that trend;
   * take the pollster's weighted mean residual, `raw`;
   * shrink it toward the prior: `(n·raw + shrinkage·prior) / (n + shrinkage)`, where `n` is the
     pollster's poll count for that key.

   Pollsters with fewer than `min_pollster_polls` polls keep their prior. Polls only identify
   effects *relative to each other*, so for each key the rating-weighted mean effect over
   pollsters (the "average pollster", not the average poll) is pinned to the same mean of the
   priors. With `estimate: false`, the priors are applied as fixed adjustments; with
   `enabled: false`, no adjustment is made. Estimates are clamped to ±`max_abs_pp`.
5. **Mean and uncertainty.** The average is the weighted mean of the house-adjusted values. Its
   standard error combines two parts:
   * binomial sampling error, `p(1−p)/n_decided × design_effect` for each poll;
   * the excess *between-poll dispersion*, `τ² = max(weighted variance − mean sampling variance, nonsampling_sd_pp²)`.

   These combine as `SE² = Σ w² (σ² + τ²) / (Σ w)²`. The reported interval is
   `mean ± z × SE`, with `z` from `interval_level` (90 % by default). `effective_n = (Σw)² / Σ(w²/n)`
   and `effective_polls = (Σw)² / Σw²` measure how much information the average holds.
   `total_se` adds the correlated industry error that no average can remove
   (`systematic_error_sd_pp × 2·sqrt(p(1−p))`). Use it when the average is treated as evidence
   about the *true* vote.
6. **Trend.** The daily trend line is a Gaussian-kernel local-linear regression on end dates,
   with bandwidth `trend_bandwidth_days` and the base weights (all factors except recency and
   volume). A small ridge penalty on the local slope (`trend_slope_ridge`) keeps the edges stable
   when `as_of` is later than the last poll. `trend_se` comes from the smoother's
   equivalent-kernel weights, and `trend_latest` is the value on `as_of`. Unlike the
   recency-weighted mean, the trend follows movement right up to the end of the series.
7. **Output.** A `PollAverage` holds `mean`, `se`, `lower`, `upper`, `total_se`, `trend`,
   `trend_se`, `trend_latest`, `n_polls`, `effective_n`, `weights`, `house_effects`
   (`pollster, key, prior_pp, estimated_pp, n_polls`), `poll_values` (adjusted value per poll)
   and `undecided_mean`. `to_dict()` returns the JSON form for the API, and `averages_frame()`
   returns a tidy table. `generic_ballot_average(...)` is the national generic House ballot by party.

## 5. Conditioning the model on polls (`blend.py`)

`poll_environment_shift(poll_average, poll_se, model_expected, model_prior_sd)` returns a logit
(utility) shift for each key that is present in both the polls and the model. The vote model is
a multinomial logit, and adding `log(y/m)` to the utilities turns the model shares `m` into the
poll shares `y` exactly. The blend is precision-weighted in that space:

* prior: `θ_k ~ N(log m_k, σ²)`, with `σ = model_prior_sd`;
* likelihood: `log y_k ~ N(θ_k, s_k²)`, with `s_k = se_k / y_k`;
* shift: `w_k · log(y_k / m_k)`, with `w_k = σ² / (σ² + s_k²)`.

Keys whose poll share is missing, zero or not finite, or whose standard error is NaN, get no
shift. An infinite standard error gives a zero weight. Before the log ratios are taken, the poll
shares are rescaled so that they sum to the model's total over the common keys. Options that were not polled therefore keep their model share, and
the result is the same whether shares are given as 0–1 or as percentages. `blend_polls_with_model`
also returns the weights, the posterior SDs and the posterior shares.
`shift_from_poll_average(avg, model_expected, model_prior_sd)` uses `total_se` by default, so a
large pile of polls that share an industry error never fully overrides the model.

## 6. Configuration reference (`config/polling.yaml`)

* `pollsters[]`: see section 2.
* `aggregation`: `recency_half_life_days`, `max_age_days`, `sample_size_cap`,
  `sample_size_exponent`, `rating_weighting`, `rating_exponent`, `default_rating`,
  `population_weights`, `method_weights`, `pollster_volume_exponent`,
  `house_effects {enabled, estimate, shrinkage, min_pollster_polls, max_iterations, tolerance, max_abs_pp}`,
  `trend_bandwidth_days`, `trend_slope_ridge`, `trend_max_days`, `undecided`,
  `undecided_exempt_poll_types`, `min_polls`, `interval_level`, `design_effect`,
  `nonsampling_sd_pp`, `systematic_error_sd_pp`.
* `generation`: `default_campaign_days`, `fieldwork_days_min/max`, `late_bias`,
  `drift_sd_per_day`, `local_drift_sd_per_day`, `start_offset_sd`, `geo_error_fraction`,
  `house_effect_jitter_pp`, `undecided_*`, `population_mix`, `sample_factor`, `sample_sd`,
  `min_sample`, `design_effect`, `competitiveness_boost`, `competitiveness_power`,
  `tossup_margin_scale`, `report_decimals`.

Poll counts per type, the campaign start date and `true_polling_error_sd` come from the
scenario's `polling` block (`PollingSpec`).

## 7. Limitations

* House effects are estimated separately in each poll group. A pollster's lean in national polls
  does not inform its province polls, except through the shared prior.
* The industry error is a single draw per key. It is not modelled as correlated with
  demographics, such as a miss among low-education voters.
* The standard error treats the estimated house effects as known; their own uncertainty is not
  propagated.
* The generator does not model RV/A polls as leaning systematically differently from LV polls;
  screened population only changes the undecided share and the aggregation weight.
* A province-level Senate poll (`geo_code` `NB`) and a seat-level one (`NB-1`) form different
  groups. `poll_group_for_race("SEN-NB-1")` returns the province-level group
  `("senate", "NB")`.
