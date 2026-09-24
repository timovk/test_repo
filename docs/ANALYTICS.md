# Analytics and export

Pure analytics over the **standard results frame** (ARCHITECTURE.md §7) and exporters with stable,
versioned schemas. Every number these modules produce comes from **SIMULATED** elections held under
a **FICTIONAL** constitution on **REAL** Dutch geography. Exports label this with a `data_category`
(and per-column provenance where it differs).

| Module | Contents |
|---|---|
| `app.analytics.results` | results-frame contract, validation, builder, party keys, race families, contest selection, aggregation |
| `app.analytics.metrics` | margins, two-party share, swing, flips, turnout, lean, elasticity, UNS projection, competitiveness, wasted votes, efficiency gap, seat–vote relationship, EC efficiency, tipping point, EC bias, race summaries |
| `app.analytics.history` | closest races, landslides, EC/PV divergence, series across elections, lineage-aware comparisons |
| `app.analytics.filters` | composable row filters |
| `app.export.schemas` | schema registry, `conform`, `validate` |
| `app.export.writers` | deterministic CSV / JSON-envelope writers, bundles with a manifest |
| `app.export.builders` | turn results frames into schema tables |

Nothing in these modules touches the database or the random number generator. Services load frames
from the database and pass them in.

---

## 1. Conventions

**Input.** One row per race × level × geo × ballot line with the columns `election_id, year,
race_code, race_type, level, geo_code, geo_name, province_code, line_key, candidate, party_code,
votes, share, valid_votes, eligible, ballots_cast, winner`. Services store **every race at every
level** (unit → municipality → [district] → province → national; `HOUSE-NB-07` also has `province`
and `national` rows holding its own totals), and every function here is written for that layout.

- `validate_results_frame(df)` lists contract violations: columns, levels, negative votes,
  `votes ≤ valid_votes ≤ ballots_cast ≤ eligible`, shares in [0, 1] and equal to
  `votes / valid_votes`, duplicate rows, line votes summing to `valid_votes`, and at most one
  `winner` per contest-geo — on a line with the most votes (a tie may be flagged for its lot winner).
- `build_results_frame(rows)` (tests, hand-built frames) derives the missing `share`, `valid_votes`,
  `winner` and other columns; counts must be finite integers (no silent truncation).
- `results_frame_from_draw(draw, races, geography, election_id=, year=, unit_district=None,
  levels=LEVELS)` builds the frame of a simulated `ElectionDraw` without the database, in the
  services layout (engine aggregation, `winner` = unique plurality leader per geo).

**Units.**

- Shares are fractions from 0 to 1.
- Any column ending in `_pp` is in percentage points.
- `_pct` columns are percentages of some other base (for example `vote_change_pct` is relative to the previous vote count).

**Party key.** Parties are identified by `party_code`. Independents have a null code and are pooled
under the key `_IND` (`INDEPENDENT_KEY`). `_IND` cannot collide with a real code, because real codes
must match `^[A-Z][A-Z0-9]{1,11}$`. When two independents run in the same race they are pooled at the
party level. Race-level winner logic still uses individual lines (see below). Export builders turn
`_IND` back into a null party code.

**Race families.** The `PRESIDENT_PROVINCE` contests (`PRES-NB`, …) belong to the `PRESIDENT` family
together with the national parent race `PRES`. Every other race type is its own family.
`PRES` and its province contests count the *same* ballots, so pooling the family would count every
presidential ballot twice where both are stored. Family pooling therefore drops `PRESIDENT_PROVINCE`
rows at an election × level × geo where `PRES` has rows (`dedupe_family_rows`).
`national_party_totals` computes national shares per election × family:

- it uses the contest rows of the family's national race when there is one (`PRES`);
- otherwise it sums the contest rows of all races in the family (all 150 House districts, or the 12
  province contests). The `national` rows that services store for `HOUSE-NB-07` or `PRES-NB` are
  never mistaken for national totals.

**Contest rows and seat contests.**

- The *contest rows* of a race are its rows at its jurisdiction level, fixed by the race type
  (`JURISDICTION_LEVELS`): `PRESIDENT` → `national`; `PRESIDENT_PROVINCE`, `SENATE`, `GOVERNOR`,
  `PROVINCIAL_LEGISLATURE` → `province`; `HOUSE` → `district`; `MAYOR`, `MUNICIPAL_COUNCIL` →
  `municipality`. The levels present do not matter (services store every level). Fallbacks per
  race: without the jurisdiction level, the finest level that wholly contains it (a House race
  fetched without district rows → its `province` row, which holds the race's full totals); with only
  finer levels (a frame filtered to `municipality`), the coarsest of those; an unknown race type
  uses its coarsest level.
- *Seat contests* are the contest rows of single-winner races: minus the national `PRES` parent race
  (it awards no seat: its electoral votes are won in the province contests) and minus the
  proportional party-list races (`PROPORTIONAL_RACE_TYPES`: councils and provincial legislatures
  elect several members by D'Hondt; the frame does not carry their seat counts).

**Grouping mode `by`.**

- `"race"` (the default in `metrics`) analyses one race × level × geo at a time. Two elections are matched on `race_code`.
- `"family"` pools a family's races per level × geo. For example, a municipality split between two House districts is analysed as a whole, with valid votes, eligible voters and ballots summed over the race-geos. Two elections are matched on the family.
- The history functions default to `"family"`.

**Ranking and ties.** Within a contest-geo, lines are ranked by:

1. votes (descending);
2. the `winner` flag, so a line that won an exact tie by lot or recount ranks first;
3. `line_key` (ascending).

The leader is the winner only if it received votes or is flagged. A contest-geo with no valid votes
has no winner and a NaN margin. *Caveat:* the services frame flags only a *unique* plurality leader
(`app.services.results`), so an exact tie decided by lot is unflagged there and falls to the lowest
line key here; check `tied` (margins, EV tallies, `electoral_votes_export`'s `decided_by = lot`)
before quoting such a winner. In `"family"` mode the winner is the plurality of pooled party
votes, with the same tie-breaks (a flagged line's party first, then the party key).

**Determinism.** Outputs are sorted by explicit keys with stable sorts and do not depend on the
input row order (the tests shuffle frames and compare). Labels taken from one of several pooled rows
come from a fixed row (lowest source code / line key). The only randomness in this area lives in the
tests, which use `app.core.rng.make_rng`.

**Scale.** Everything is vectorised (group-bys, `np.lexsort`, no per-row Python). A realdata test
runs the history, seat, Electoral College and export paths on four full elections stored at every
level down to the 14,729 buurten (> 1M rows).

---

## 2. Metrics (`app.analytics.metrics`)

### 2.1 Margin: `margin_table(frame, level=None)`, `top_two_margin(votes)`

`margin_pp = (v₁ − v₂) / V × 100`, where v₁ and v₂ are the leader's and runner-up's votes and V is
the valid votes at that level. `margin_votes = v₁ − v₂`.

- An uncontested race has `v₂ = 0`, so its margin is the winner's full share (100 pp); `contested` is false.
- `tied` marks an exact top-two tie. The winner then comes from the flag, otherwise the lowest line key.
- With `V = 0` the margin is NaN and there is no winner.

*Multiparty caveat.* A small top-two margin in a three-way race (40/38/22) is not the same kind of
contest as 51/49. Read margins together with the effective number of candidates (§2.9).

### 2.2 Two-party share: `two_party_share(frame, a=None, b=None, level=, by=)`

`two_party_share_a = a / (a + b)` and `two_party_margin_pp = (a − b)/(a + b) × 100`. Both are NaN
where neither party received votes. `margin_pp = (s_a − s_b) × 100` uses all valid votes. If no pair
is given, the national top two of the frame are used (`national_top_two`, independents excluded,
ties broken by code). The frame must then hold exactly one election and one family.

*Caveat.* Two-party measures ignore third parties. In a district a third party wins, the
two-party share still describes the a/b contest, not the outcome.

### 2.3 Swing: `swing`, `two_party_swing`, `national_swing`, `swing_ratio`

For each geo × party: `swing_pp = (s_curr − s_prev) × 100`, `vote_change = v_curr − v_prev` and
`vote_change_pct = vote_change / v_prev × 100` (NaN if `v_prev = 0`). The `status` column says how
the geo and party matched between the two elections:

| status | meaning | values |
|---|---|---|
| `both` | the party ran at the geo in both elections | both |
| `new_party` / `dropped_party` | the geo exists in both, the party ran in only one | missing side = 0 % |
| `new_geo` / `dropped_geo` | the geo exists in only one election | missing side = NaN |

A *missing previous election* never becomes silent zeros. Geos present in only one election are
flagged NaN. If a municipality merged between cycles, remap it with the lineage first (§3.4).

The two-party swing is the change in `a/(a+b)` in pp. `national_swing` is the change in each party's
national share in pp; a party absent from one election counts as 0 % there.

*Caveat.* When a party fields no candidate in a district, its apparent swing there is mechanical
(`new_party` or `dropped_party`), not a shift in opinion. Filter on `status == "both"` for
like-for-like swings.

### 2.4 Flips: `flips(prev, curr, level=, by=)`, `flip_summary(flips, weights=None)`

`flips` compares the winning party of each geo between the two elections. `status` is:

- `hold` — same winning party;
- `flip` — different party (`gained_by` and `lost_by` are set);
- `new` — the geo exists only in the current election;
- `dropped` — the geo exists only in the previous election;
- `undecided` — at least one side has no winner (no votes).

`flip_summary` gives per party `wins_prev, wins_curr, gains, losses, holds, net`. It accepts optional
geo weights, for example electoral votes, to count "EV flipped".

*Caveat.* After a redistricting the same district code can cover different territory. A district
"flip" then mixes boundary effects with opinion change.

### 2.5 Turnout: `turnout`, `turnout_change`

`turnout = ballots_cast / eligible` (NaN when `eligible = 0`) and `blank_or_invalid = ballots_cast −
valid_votes`. `turnout_change_pp = (t_curr − t_prev) × 100`. Eligible voters are a **DERIVED**
estimate from CBS age bands (see DATA_PROVENANCE).

### 2.6 Partisan lean: `partisan_lean`, `margin_lean`

- **Share lean.** `lean_pp = (s_geo − s_nat) × 100` per geo × party, using the party's national share in the same election and family. Weighted by valid votes, lean averages to exactly 0 over a level that covers the country. The tests check this.
- **Margin lean for a pair.** `lean_pp = m_geo − m_nat`, where `m = (a − b)/(a + b) × 100` (`two_party=True`, the default) or `m = (s_a − s_b) × 100`. Positive values lean toward `a`, and `leans` names the favoured party. This is a margin-based analogue of the Cook PVI, which uses shares and is therefore half this value in a two-party world.

*Caveat.* Parties that did not run at a geo have no lean there, and they are not listed. That is
deliberate: "−18 pp" for a party with no candidate would be meaningless.

### 2.7 Elasticity: `elasticity(frame, level=, by=)`, `elasticity_from_draws(geo, nat)`

Elasticity is the slope of a geo's party share on the party's national share.

- **≥ 3 elections.** For each geo × party, OLS over the elections in which the party ran at the geo gives `ŝ_geo = α + β·s_nat`. β is the elasticity, returned with `intercept` and `r_squared`, `method = "ols"`.
- **2 elections.** `β = Δs_geo / Δs_nat`, the ratio of swings, `method = "swing_ratio"`. `swing_ratio(geo, nat)` computes the same thing for arrays.
- **Simulation draws.** `elasticity_from_draws(geo (S, G), nat (S,))` returns `cov(geo, nat) / var(nat)` for each geo.

Reading the result: β > 1 means the geo swings more than the nation (elastic, persuadable); β < 1
means inelastic; β ≈ 0 means the geo is insulated from national tides.

The result is NaN when the national share varies by less than `min_national_range`: 0.1 pp across
elections, or 1e-6 in share units across draws. A near-zero national swing would make the ratio
explode.

*Caveats.* With only two or three elections the estimate is noisy. The OLS pools every election in
the frame, so check `n_obs`. Elasticity is not causal. With many parties, a party's national share
moves partly because of other parties' entries and exits.

### 2.8 Uniform national swing: `uniform_swing_projection(prev, swing_pp, level=None, seat_weights=None, renormalize=True, contested_only=True)`

The projection starts from the previous contest shares `S` (contests × parties) and a national swing
vector `σ` in pp (for example from `national_swing` or a polling average):

1. `P = max(0, S + σ ⊙ mask)`. With `contested_only`, `mask = S > 0`, so a party gains nothing where it did not run.
2. With `renormalize`, each contest's projected shares are rescaled to the sum of its original shares.
3. Each contest goes to the ballot *line* with the largest projected share. A line keeps its fraction
   of its party's share, so independents pooled under `_IND` never win as a bloc (30 % + 25 % does
   not beat 45 %); a party projected into a contest where it has no line counts as one line. Exact
   ties go to the line flagged `winner` (a tie won by lot), then the lowest line key.
4. Seats per race family × party are summed with `seat_weights` (geo code → seats; use EV per province for the Electoral College). The default weight is 1. Seats of different families are never added up.

The contests are the seat contests of `prev`, or with `level=` every race-geo at that level (a geo
where the `PRES` parent and a `PRES-<PV>` contest both have rows counts once).

The result has three parts: `shares` (contest × party), `contests` (previous and projected winner,
projected margin over the runner-up line, `flipped`, `weight`) and `seats` (`race_family, party,
seats_prev, seats_projected, change`). A zero swing reproduces the previous winners exactly, lot
winners included.

*Caveats.* UNS assumes every district moves by the same number of points. Real multiparty swings
are proportional in strongholds and bounded near zero. Clipping and renormalising keep shares
valid but change the effective national swing slightly. Consider elasticity-weighted swings for
strongly heterogeneous maps.

### 2.9 Competitiveness: `competitiveness(frame, level=None, threshold_pp=20)`, `competitiveness_summary`, `probability_competitiveness`

- **Index.** `C = clip(1 − margin_pp / threshold_pp, 0, 1)`. A dead heat scores 1; a margin of at least the threshold (20 pp by default) scores 0.
- **Rating.** Rating bands use |margin| in pp: under 3 is `tossup`, under 8 `lean`, under 15 `likely`, and anything else `safe`.
- **Effective number of candidates.** `ENC = 1 / Σ sᵢ²` (Laakso–Taagepera). A 50/50 race has ENC 2; 34/33/33 has ENC ≈ 3.
- **From a win probability.** `1 − |2p − 1|`, useful with forecast or race-call probabilities.

By default each race is scored at its jurisdiction level. `level="municipality"` (or `province`, and
so on) scores every geo of every race at that level instead. `competitiveness_summary(comp, by)`
aggregates the scores per province, race type or election: count, mean index, median margin, count
per rating and mean ENC.

*Multiparty caveat.* A low top-two margin with a high ENC is a fragmented race, where a small shift
among minor parties can decide the seat. Report the margin and ENC together.

### 2.10 Wasted votes and efficiency gap (multiparty FPTP): `wasted_votes`, `efficiency_gap`, `party_efficiency`

These are computed over seat contests.

- **Losing lines** waste all their votes.
- **The winner** wastes `max(0, v₁ − (v₂ + 1))`, its votes beyond runner-up + 1. An uncontested winner needed one vote. A tie won by lot wastes 0. A contest with no votes has no winner and no wasted votes.

**Pairwise efficiency gap.** `EG(a, b) = (W_b − W_a) / V`, where V is **all** valid votes in the
seat contests. **Positive EG favours `a`**, because `a` wastes fewer votes. The table also reports
`efficiency_gap_pair = (W_b − W_a)/(V_a + V_b)`. The sign convention is spelled out here because
the literature uses both signs. The default pair is the top two parties by votes in each election
and family.

**Multiparty efficiency gap per party.**

`EG_p = (V_p · W / V − W_p) / V`

This is the difference between the wasted votes party p would have at the overall waste rate
(`W/V`) and the wasted votes it actually has, as a share of all votes. It sums to 0 over parties,
and positive values mean the map is efficient for p. With two parties of equal vote share it equals
half the classic two-party gap.

*Caveats.*

- With three or more parties, "wasted" losing votes include votes for parties that could never win anywhere. EG then partly measures fragmentation, not districting.
- Turnout differences between districts affect EG.
- Pooling different chambers is meaningless, so results are grouped by family.
- Do not compare EG across maps with very different numbers of contests.

### 2.11 Vote efficiency: `party_efficiency` columns

| column | formula |
|---|---|
| `votes_per_seat` | `V_p / seats_p` (NaN without seats) |
| `waste_rate` | `W_p / V_p` |
| `wasted_share` | `W_p / V` |
| `seat_bonus_pp` | `(seat share − vote share) × 100` |

`seat_weights` counts EV instead of contests.

### 2.12 Seat–vote relationship: `seat_vote_table`, `seat_vote_fit`, `seat_vote_from_draws`, `uns_seat_vote_curve`, `partisan_bias`

- **`seat_vote_table`** gives each party's `vote_share` (of all seat-contest votes) and `seat_share` (of all contests, or weighted) per election × family.
- **`seat_vote_fit(table, sample_col, method, reference_share)`** fits the relationship across elections (`sample_col="election_id"`) or Monte Carlo draws (`seat_vote_from_draws(...)`, `sample_col="draw"`). There are two methods:
  - `linear`: `S = α + β·V`. Responsiveness is β, in seat points per vote point.
  - `logit` (King–Browning): `logit S = λ + ρ·logit V`. Responsiveness is ρ; ρ ≈ 3 is the "cube law" typical of two-party FPTP. Seat shares of 0 or 1 are clipped by half a seat.
  - Bias is `(Ŝ(v*) − v*) × 100`: seats above proportionality at the reference vote share v\*. The default v\* is the party's mean vote share; pass 0.5 for the classic two-party bias.
- **`uns_seat_vote_curve(frame, a, b, grid)`** works on one election. For each target two-party share `t`, it adds `s = t·(A+B) − A` to `a` and subtracts it from `b` in every contest where they ran, with other parties unchanged. Each contest goes to the plurality line, as in §2.8. The output is `seat_share_a/b/other(t)`.
- **`partisan_bias`** reads that curve:
  - `bias = (S_a − S_b)/2` at equal national a/b votes (t = 0.5). With two parties this is the classic `S_a(50 %) − 50 %`, and positive values favour `a`.
  - `responsiveness = (S_a(t₀+h) − S_a(t₀−h)) / 2h` at the observed `t₀`, with h = 2.5 pp by default.

*Caveats.* Bias across real elections confounds map effects with changing party systems. Estimate
it from simulation draws of one map for a cleaner answer. Seat curves are step functions: choose `h`
wide enough to cross several seats.

### 2.13 Electoral College efficiency: `ec_efficiency(pv, ev)`, `electoral_college_tally(frame, ev_by_province, by="party"|"line")`

`efficiency_pp = (EV share − PV share) × 100` per ticket. Positive values mean the ticket turned
its popular vote into more than a proportional share of electoral votes. The values sum to 0 when
every EV is awarded. `electoral_college_tally` derives winner-take-all EV from the
`PRESIDENT_PROVINCE` contests; a lot winner's flag counts. The popular vote comes from the national
`PRES` rows. The function raises if the province set differs from `ev_by_province`. For
non-winner-take-all allocations, pass the EV mapping to `ec_efficiency` directly.

### 2.14 Tipping point and EC bias: `tipping_point`, `ec_bias`, `tipping_point_from_frame`

This is implemented locally, without the elections package, and agrees exactly (province and
margin) with `app.elections.electoral_college.tipping_point` — same ordering, tie rule, default
majority and margin arithmetic; a test cross-checks every ticket on random canonical maps,
including exactly tied margins. One deliberate difference: a province with no valid votes has an
undefined margin here and sorts last, where the engine treats it as 0 pp.

1. Sort provinces by the candidate's margin over the strongest opponent (pp, descending). Equal margins keep the `ev_by_province` order; NaN margins sort last.
2. Accumulate electoral votes in that order.
3. The province that brings the running total to `majority` is the tipping point. The default majority is `majority_of(sum EV)`, which is 88 of 174 canonically.

`EC bias = tipping-point margin − national popular-vote margin` (pp). Positive values mean the
Electoral College is more favourable to the candidate than the popular vote.
`tipping_point_from_frame` defaults to the EV leader, and to province margins over the strongest
opponent in each province contest.

*Multiparty caveat.* The margin is taken over the strongest opponent in each province, so a
candidate can "trail" to different rivals in different provinces. The tipping point answers "how
far could this candidate fall uniformly before losing the majority" only approximately, when the
opposition is fragmented.

### 2.15 Race summaries: `race_summaries(frame, prev=None, incumbents=None)`

This gives one row per race: winner, runner-up, margins, turnout, `previous_winner_party`,
`flip_status` (`hold | flip | new | undecided`), `incumbent_*`, `is_open_seat` and `incumbent_won`.

- The previous holder is the winner of the same race code in the most recent election in `prev` *before* the row's election (by `(year, election_id)`), so `prev` may be the whole history, current and later elections included. Failing that, it is `incumbents.incumbent_party`, which is the right source for Senate seats last contested six years earlier.
- `incumbent_won` is null when the incumbent candidate is unknown or the seat is open.

---

## 3. History (`app.analytics.history`)

`frames` arguments accept one frame that holds several elections, or a list of frames.

### 3.1 Records

`closest_races(frame, n=10, race_types=None)` and `largest_landslides(frame, n=10, race_types=None)`
work at each race's jurisdiction level.

- `closest_races` orders by `margin_pp`, then `margin_votes`, `year` and `race_code`, all ascending.
- `largest_landslides` orders by `margin_pp` and `margin_votes` descending, then `year` and `race_code` ascending.
- Races without votes are excluded. Uncontested races (100 pp by definition) are excluded unless `include_uncontested=True`.
- `race_types` is a strict race-type filter. Without it, proportional party-list races are left out: the top-two list margin of a council decides no seat.

### 3.2 EC and popular vote: `ec_pv_divergence(long_df, majority=None)`

The input has one row per election × ticket with `year, key, popular_votes, electoral_votes` and an
optional `election_id`; `electoral_college_tally` output fits directly. The output is **descriptive
only**:

- the PV leader and its share and margin;
- the EV leader and its EV;
- whether the EV majority was reached;
- `diverged`, true when both leaders exist and differ;
- a factual one-line `description`.

Exact ties at the top give a null leader.

### 3.3 Series

| function | returns |
|---|---|
| `province_trend(frames, province, party, race_type="PRESIDENT")` | share, national share, lean and change per election |
| `party_support_series(frames, geo_code, party, since_year=None, race_type=None, level=None)` | any geo, level inferred (e.g. Tilburg `GM0855` since 2028); elections where the party did not run show 0 |
| `district_history(frames, "NB-07")` | the House race of a district code across elections, with `first/hold/flip/undecided` |
| `seat_totals(frames, "HOUSE")` | seats per election × party, `majority` (default: majority of the contests held) and `has_majority`; Senate control also depends on holdover seats that are not in the frame; proportional race types raise `ValueError` (D'Hondt seats are not derivable from the frame) |

### 3.4 Lineage-aware comparison

`remap_lineage(frame, lineage, level="municipality", names=None, province_codes=None,
round_votes=True)` re-expresses an older election's rows at `level` on the newer code set. The
lineage DataFrame has columns `from_code, to_code, population_weight` — one transition from the old
code set to the new one, as `app.geography.lineage.compute_lineage` produces it (compose several
transitions first). A weight is the share of the old unit's population that moved to the new one:
1.0 for a clean merger, fractions for splits.

- Votes, valid votes, eligible voters and ballots are multiplied by the weight and summed per new code.
- With `round_votes`, counts are rounded and `valid_votes` is recomputed as the sum of the rounded line votes. Clean mergers are exact. With `round_votes=False` the weighted counts stay fractional (float columns).
- Affected units get recomputed shares and plurality winners. Unaffected units, and all other levels, pass through unchanged.
- Race codes that embed a remapped municipality code (`MAYOR-GM0001`) are rewritten to the dominant successor. When that merges races (`MAYOR-GM0001` + `MAYOR-GM0002` → `MAYOR-GM0100`), their rows at the other levels (the `province` and `national` rows services store) are pooled too, so the result is still a valid results frame.
- The lineage is validated: finite, non-negative weights, no duplicate pairs, and the weights of a `from_code` must sum to at most 1. Sums within `LINEAGE_WEIGHT_TOLERANCE` (1e-4) of 1 are rounding (`compute_lineage` rounds weights to 6 decimals, so three pieces can sum to 1.000001) and are rescaled to exactly 1, which keeps splits exact.

`compare_elections(prev, curr, level, race_type=None, lineage=None, parties=None, by="family")`
returns a tidy swing table: the §2.3 columns plus `winner_prev`, `winner_curr`, `flip_status`,
`turnout_prev`, `turnout_curr` and `turnout_change_pp`. With `lineage`, the previous election is
remapped onto the current municipalities first, so a merger shows as `both` rather than
`dropped_geo` + `new_geo`. `municipality_flips(prev, curr, race_type="PRESIDENT", lineage=None)`
returns the flips table at municipality level.

*Caveat.* Split weights are population shares, which assumes that votes were spread like
population within the old municipality.

---

## 4. Filters (`app.analytics.filters`)

Constructors return a `Filter`, a named, vectorised row predicate. Filters compose with `&`, `|` and
`~`, and are applied by calling them:

```python
from app.analytics import filters as F
f = F.years(since=2028) & F.parties("PA") & F.levels("municipality") & ~F.provinces("ZE")
rows = f(frame)
```

| constructor | selects |
|---|---|
| `years(*ys, since=, until=)` | election years |
| `elections(*ids)` | election ids |
| `parties(*codes)` | party lines (`_IND` = independents) |
| `candidates(*names, contains=False, case_sensitive=False)` | candidates by name |
| `provinces(*codes)` | rows with that `province_code`, plus the province rows themselves |
| `municipalities(*codes, include_units=False, include_races=False)` | CBS `BU` prefix for units; `MAYOR-/COUNCIL-GMxxxx` races |
| `districts(*codes)` | every row of `HOUSE-<code>` plus district-level rows |
| `race_types(*types, family=False)` | race types, or whole families |
| `race_codes(*codes, prefix=False)` | race codes |
| `levels(*levels)` | geographic levels |
| `geos(*codes)` | geo codes |
| `election_types(*types)` | the `election_type` column if present, otherwise inferred: presidential → general, House → midterm, governor/legislature only → provincial, mayor/council only → municipal, else special |
| `winners_only()` | winning lines |

`filter_results(frame, *filters, year=, since_year=, until_year=, election_id=, party=,
candidate=, province=, municipality=, district=, race_type=, family=, race_code=, level=,
geo_code=, election_type=, winners=)` combines all its arguments with AND.

---

## 5. Export (`app.export`)

### 5.1 Schema contract

Each dataset has an `ExportSchema` with:

- a name and a **version**;
- a dataset **data_category**;
- ordered `Column`s, each with a logical type (`int, float, str, bool, date, datetime`), nullability, output decimals, allowed values, a range, a description and optional per-column provenance (for example REAL CBS codes inside a SIMULATED table);
- a **key** that defines row order and must be unique.

`fingerprint()` hashes the name, version, ordered column names, types and nullability. The tests
pin every column list and fingerprint, so **changing a schema requires bumping its version**.
`get_schema(name)` also accepts the API dataset aliases (`provinces`, `calls`, `timeline`, …).

- `conform(df, schema, allow_unknown=False)` orders the columns and casts them to the pandas dtypes (`Int64`, `float64`, `string`, `boolean`, `datetime64[ns]`). It adds missing nullable columns as nulls and rejects missing required columns. Unknown columns are an error unless `allow_unknown=True`, which drops them.
- `validate(df, schema)` lists every problem: column order, dtypes, nulls in non-nullable columns, allowed values, ranges and key uniqueness. `assert_valid` raises `SchemaError`.

### 5.2 Formats (`app.export.writers`)

Every writer conforms, validates and sorts by key before writing. Files are written in chunks of
`CHUNK_ROWS` rows (compact JSON too), with bytes identical to the in-memory `to_csv_text` /
`dumps_envelope` output, so a ~1M-row unit export never holds all rows as Python objects.
Integers are never passed through float64 (64-bit seeds and ids survive CSV and JSON round trips);
dates and datetimes are read as ISO 8601 of any precision, and datetimes with mixed UTC offsets
(an election night across the end of daylight saving time) are converted to UTC.

- **CSV** (`write_csv`, `to_csv_text`, `read_csv`):
  - UTF-8 with `\n` line endings, and a header in schema order;
  - an empty field means null (so an empty string is indistinguishable from null);
  - `true`/`false` for booleans, `YYYY-MM-DD` for dates and ISO 8601 for datetimes;
  - floats rounded to the column's decimals and written in shortest round-trip form, with `-0.0` normalised to `0.0`.
- **JSON** (`write_json`, `to_json_envelope`, `read_json`) writes one envelope:

  ```json
  {"schema": "province_results", "schema_version": 1, "data_category": "SIMULATED",
   "generated_at": "2032-11-04T06:00:00+00:00", "metadata": {"election": 12, "seed": 42},
   "columns": [{"name": "election_id", "type": "int", "nullable": false, "description": "…",
                "data_category": "SIMULATED"}, …],
   "rows": [[12, 2032, "PRES-NB", …], …]}
  ```

  Rows are arrays aligned with `columns`. Nulls and non-finite floats become `null`. Metadata keys are
  sorted (enum keys by value). NumPy scalars (including `np.bool_`), `None`/`pd.NA`/`NaT` (→ `null`),
  dates, datetimes and `np.datetime64`, durations (ISO 8601), enums, paths, sets (sorted), sequences,
  arrays and pandas Series/Index are converted; anything else (e.g. `bytes`) raises `TypeError`.
- **Bundles.** `export_bundle(frames, out_dir, formats=("csv","json"), metadata, generated_at=None)` validates every dataset first (an invalid dataset leaves no partial bundle), then writes `<schema>.csv` and `<schema>.json` for each dataset. It also writes `manifest.json` with the bundle version, `generated_at`, metadata, data categories and, for each file, its schema, version, fingerprint, format, category, row count and SHA-256. It returns the written paths.
- **Determinism.** The same data give byte-identical CSVs, whatever the input row or column order. JSON output is also byte-identical once `generated_at` is pinned.

### 5.3 Builders (`app.export.builders`)

| function | output schema |
|---|---|
| `results_export(frame, level, unit_municipality=None)` | `<level>_results` |
| `house_results_export(frame, prev=None, incumbents=None)` | `house_results` (`district_code` from the race code, `district_name` null, when the frame has no district rows) |
| `senate_results_export(…, senate_classes=None, special_races=None)` | `senate_results` (`province_code` from the race code without province rows) |
| `governor_results_export(…)` | `governor_results` (likewise) |
| `electoral_votes_export(frame, ev_by_province, decided_by=None)` | `electoral_votes` (ties default to `lot`) |
| `swing_export(prev, curr, level, race_type=None, lineage=None)` | `swing` |
| `build_all_results(frame)` | `{schema: frame}` for every level present |

Datasets that come straight from database rows (timeline, calls, forecasts, polls, district plans,
apportionment) only need `conform`.

### 5.4 Schema reference (generated from the registry)

| schema | v | category | key (sort order) | aliases | fingerprint |
|---|---|---|---|---|---|
| `national_results` | 1 | SIMULATED | election_id, race_code, geo_code, line_key | national | `c8e6355ad48fddb4` |
| `province_results` | 1 | SIMULATED | election_id, race_code, geo_code, line_key | provinces | `144b26c7a6d6efa3` |
| `municipality_results` | 1 | SIMULATED | election_id, race_code, geo_code, line_key | municipalities | `144112fda081d0e7` |
| `unit_results` | 1 | SIMULATED | election_id, race_code, geo_code, line_key | units, precinct_results | `c810ea8a1b320416` |
| `district_results` | 1 | SIMULATED | election_id, race_code, geo_code, line_key | district_lines | `055e8c5543f33b9b` |
| `house_results` | 1 | SIMULATED | election_id, district_code | house | `723c9eab543eefa3` |
| `senate_results` | 1 | SIMULATED | election_id, race_code | senate | `6bbeb93a1b00e0c3` |
| `governor_results` | 1 | SIMULATED | election_id, race_code | governors | `8cda429a05c11886` |
| `electoral_votes` | 1 | SIMULATED | election_id, province_code | ev, electoral_college | `c8e4cf9706eb817e` |
| `reporting_timeline` | 1 | SIMULATED | election_id, seq | timeline | `4afc85a31af4f437` |
| `race_calls` | 1 | SIMULATED | election_id, seq, race_code, status, is_manual | calls | `695c134a420e1e3b` |
| `montecarlo_summary` | 1 | SIMULATED | run_id, race_code, line_key | — | `14d24c00a0cb2c18` |
| `montecarlo_distribution` | 1 | SIMULATED | run_id, subject, key, value | — | `31482a73deb874b6` |
| `polling_averages` | 1 | SIMULATED | election_id, as_of, poll_type, geo_code, label | — | `ae0d3c80927bd2a5` |
| `polls` | 1 | SIMULATED | poll_id, label | — | `5aebf733c0ccf47e` |
| `districts` | 1 | FICTIONAL | plan_id, district_code | district_plan | `d2108fffe3341000` |
| `apportionment` | 1 | FICTIONAL | apportionment_id, province_code | — | `389ec5ca0414ffa8` |
| `swing` | 1 | SIMULATED | election_id_prev, election_id_curr, race_family, race_code, level, geo_code, party | compare | `4fc00f1324914823` |

#### Result tables — `national_results`, `province_results`, `municipality_results`, `district_results`, `unit_results`

One row per race × geographic unit × ballot line (the standard results frame plus `turnout`). `level` is fixed per schema (`national`, `province`, `municipality`, `district`, `unit`). `unit_results` inserts `municipality_code` (str, nullable, REAL) after `province_code`. In `district_results` the `geo_code`/`geo_name` are FICTIONAL district identifiers; elsewhere they are REAL CBS codes/names.

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `election_id` | int | no | Election id (database key of the simulated election). |
| 2 | `year` | int | no | Election year. |
| 3 | `race_code` | str | no | Race code, e.g. PRES-NB, HOUSE-NB-07, SEN-NB-1, GOV-NB. |
| 4 | `race_type` | str | no | Race type (RaceType).; allowed: RaceType values |
| 5 | `level` | str | no | Geographic level of the row. |
| 6 | `geo_code` | str | no | National code 'NL'.; **REAL** |
| 7 | `geo_name` | str | yes | Name of the geographic unit.; **REAL** |
| 8 | `province_code` | str | yes | Province code (null for national rows).; **REAL** |
| 9 | `line_key` | str | no | Stable key of the ballot line within the race. |
| 10 | `candidate` | str | yes | Ballot name (FICTIONAL candidate or ticket).; **FICTIONAL** |
| 11 | `party_code` | str | yes | FICTIONAL party code (null for independents).; **FICTIONAL** |
| 12 | `votes` | int | no | Valid votes for the line at this level.; ≥ 0 |
| 13 | `share` | float | no | Share of valid votes at this level (0–1).; 6 dp; ≥ 0 ≤ 1 |
| 14 | `valid_votes` | int | no | Valid votes cast in the race at this level.; ≥ 0 |
| 15 | `eligible` | int | yes | Eligible voters (DERIVED estimate from REAL CBS data).; **DERIVED**; ≥ 0 |
| 16 | `ballots_cast` | int | yes | Ballots cast in the race at this level (valid + blank + invalid).; ≥ 0 |
| 17 | `turnout` | float | yes | ballots_cast / eligible.; 6 dp; ≥ 0 ≤ 1 |
| 18 | `winner` | bool | no | True if the line won at this level (plurality). |

#### Race summaries — `house_results`, `senate_results`, `governor_results`

Leading columns:

* `house_results`: `election_id, year, race_code, district_code` (FICTIONAL, not null), `district_name`, `province_code`
* `senate_results`: `election_id, year, race_code, province_code, province_name, seat_number` (int ≥ 1), `senate_class` (int ≥ 1), `is_special` (bool)
* `governor_results`: `election_id, year, race_code, province_code, province_name`

followed by the shared summary block:

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `winner_line_key` | str | yes | Line key of the winner (null without votes). |
| 2 | `winner_candidate` | str | yes | Winner's ballot name.; **FICTIONAL** |
| 3 | `winner_party` | str | yes | Winner's party code (null for an independent).; **FICTIONAL** |
| 4 | `winner_votes` | int | yes | Winner's votes.; ≥ 0 |
| 5 | `winner_share` | float | yes | Winner's share of valid votes.; 6 dp; ≥ 0 ≤ 1 |
| 6 | `runner_up_candidate` | str | yes | Runner-up's ballot name.; **FICTIONAL** |
| 7 | `runner_up_party` | str | yes | Runner-up's party code.; **FICTIONAL** |
| 8 | `runner_up_votes` | int | yes | Runner-up's votes (0 when uncontested).; ≥ 0 |
| 9 | `margin_votes` | int | yes | Winner votes − runner-up votes.; ≥ 0 |
| 10 | `margin_pp` | float | yes | Top-two margin in percentage points of valid votes.; 4 dp |
| 11 | `tied` | bool | yes | Exact tie between the top two (winner decided by lot/recount). |
| 12 | `valid_votes` | int | yes | Valid votes in the race.; ≥ 0 |
| 13 | `eligible` | int | yes | Eligible voters (DERIVED estimate).; **DERIVED**; ≥ 0 |
| 14 | `ballots_cast` | int | yes | Ballots cast in the race.; ≥ 0 |
| 15 | `turnout` | float | yes | ballots_cast / eligible.; 6 dp; ≥ 0 ≤ 1 |
| 16 | `previous_winner_party` | str | yes | Party that held the seat before (previous election or incumbent).; **FICTIONAL** |
| 17 | `flip_status` | str | yes | hold / flip / new (no previous holder) / undecided.; allowed: `hold`, `flip`, `new`, `undecided` |
| 18 | `flipped` | bool | yes | True when the seat changed party. |
| 19 | `incumbent_candidate` | str | yes | Incumbent's ballot name if running.; **FICTIONAL** |
| 20 | `incumbent_party` | str | yes | Incumbent's party code.; **FICTIONAL** |
| 21 | `is_open_seat` | bool | yes | No incumbent on the ballot. |
| 22 | `incumbent_won` | bool | yes | Incumbent re-elected (null when unknown or open seat). |

#### `electoral_votes` (v1, SIMULATED)

Electoral votes per province: EV, winner and margin of the province contest (SIMULATED).

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `election_id` | int | no | Election id (database key of the simulated election). |
| 2 | `year` | int | no | Election year. |
| 3 | `race_code` | str | no | Race code, e.g. PRES-NB, HOUSE-NB-07, SEN-NB-1, GOV-NB. |
| 4 | `province_code` | str | no | Province code.; **REAL** |
| 5 | `province_name` | str | yes | Province name.; **REAL** |
| 6 | `electoral_votes` | int | no | Electoral votes of the province (House seats + senators).; **FICTIONAL**; ≥ 0 |
| 7 | `winner_line_key` | str | yes | Line key of the ticket that carried the province. |
| 8 | `winner_candidate` | str | yes | Ticket that carried the province.; **FICTIONAL** |
| 9 | `winner_party` | str | yes | Party of that ticket.; **FICTIONAL** |
| 10 | `winner_votes` | int | yes | Winner's votes in the province.; ≥ 0 |
| 11 | `winner_share` | float | yes | Winner's share of valid votes.; 6 dp; ≥ 0 ≤ 1 |
| 12 | `runner_up_party` | str | yes | Runner-up's party code.; **FICTIONAL** |
| 13 | `margin_votes` | int | yes | Winner votes − runner-up votes.; ≥ 0 |
| 14 | `margin_pp` | float | yes | Top-two margin in percentage points.; 4 dp |
| 15 | `decided_by` | str | yes | popular_vote / lot / recount / contingent (null when unknown).; allowed: `popular_vote`, `lot`, `recount`, `contingent` |

#### `reporting_timeline` (v1, SIMULATED)

Election-night reporting batches in order (SIMULATED).

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `election_id` | int | no | Election id (database key of the simulated election). |
| 2 | `seq` | int | no | Sequence number of the batch (1-based).; ≥ 0 |
| 3 | `sim_time_s` | float | no | Seconds after the first poll closing.; 3 dp; ≥ 0 |
| 4 | `time` | datetime | yes | Simulated local wall-clock time of the batch. |
| 5 | `municipality_code` | str | no | CBS municipality code reporting.; **REAL** |
| 6 | `municipality_name` | str | yes | Municipality name.; **REAL** |
| 7 | `province_code` | str | yes | Province code (null for national rows).; **REAL** |
| 8 | `kind` | str | yes | batch / final / correction.; allowed: `batch`, `final`, `correction` |
| 9 | `ballots_in_batch` | int | no | Ballots counted in this batch.; ≥ 0 |
| 10 | `cumulative_ballots` | int | yes | Ballots counted nationally after this batch.; ≥ 0 |
| 11 | `municipality_fraction_after` | float | yes | Fraction of the municipality's ballots counted after the batch.; 6 dp; ≥ 0 ≤ 1 |
| 12 | `national_fraction` | float | yes | Fraction of all ballots counted nationally after the batch.; 6 dp; ≥ 0 ≤ 1 |

#### `race_calls` (v1, SIMULATED)

Race-call log: every change of a race's call state with its evidence (SIMULATED).

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `election_id` | int | no | Election id (database key of the simulated election). |
| 2 | `seq` | int | no | Reporting-event sequence number at the call.; ≥ 0 |
| 3 | `sim_time_s` | float | yes | Seconds after the first poll closing.; 3 dp; ≥ 0 |
| 4 | `called_at` | datetime | yes | Simulated local time of the call. |
| 5 | `race_code` | str | no | Race code, e.g. PRES-NB, HOUSE-NB-07, SEN-NB-1, GOV-NB. |
| 6 | `race_type` | str | yes | Race type (RaceType).; allowed: RaceType values |
| 7 | `status` | str | no | Call state (RaceStatus).; allowed: RaceStatus values |
| 8 | `line_key` | str | yes | Line key of the projected/called candidate (null when none). |
| 9 | `candidate` | str | yes | Ballot name (FICTIONAL candidate or ticket).; **FICTIONAL** |
| 10 | `party_code` | str | yes | FICTIONAL party code (null for independents).; **FICTIONAL** |
| 11 | `reporting_pct` | float | yes | Percent of expected ballots counted in the race.; 4 dp; ≥ 0 ≤ 100 |
| 12 | `leader_margin_pct` | float | yes | Leader's margin in percentage points at the call.; 4 dp |
| 13 | `win_probability` | float | yes | Calling engine's win probability for the leader.; 6 dp; ≥ 0 ≤ 1 |
| 14 | `is_manual` | bool | no | Manual override. |
| 15 | `superseded` | bool | yes | Superseded by a later call state. |

#### `montecarlo_summary` (v1, SIMULATED)

Monte Carlo forecast per race × line (SIMULATED model estimates, not predictions).

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `run_id` | int | no | Forecast run id. |
| 2 | `election_id` | int | no | Election id (database key of the simulated election). |
| 3 | `seed` | int | yes | Root seed of the run. |
| 4 | `n_simulations` | int | yes | Number of simulated elections.; ≥ 1 |
| 5 | `race_code` | str | no | Race code, e.g. PRES-NB, HOUSE-NB-07, SEN-NB-1, GOV-NB. |
| 6 | `race_type` | str | yes | Race type (RaceType).; allowed: RaceType values |
| 7 | `line_key` | str | no | Stable key of the ballot line within the race. |
| 8 | `candidate` | str | yes | Ballot name (FICTIONAL candidate or ticket).; **FICTIONAL** |
| 9 | `party_code` | str | yes | FICTIONAL party code (null for independents).; **FICTIONAL** |
| 10 | `win_probability` | float | no | Share of simulations won.; 6 dp; ≥ 0 ≤ 1 |
| 11 | `mean_share` | float | yes | Mean vote share.; 6 dp; ≥ 0 ≤ 1 |
| 12 | `p05_share` | float | yes | 5th percentile of vote share.; 6 dp; ≥ 0 ≤ 1 |
| 13 | `p50_share` | float | yes | Median vote share.; 6 dp; ≥ 0 ≤ 1 |
| 14 | `p95_share` | float | yes | 95th percentile of vote share.; 6 dp; ≥ 0 ≤ 1 |
| 15 | `mean_votes` | float | yes | Mean votes.; 2 dp; ≥ 0 |

#### `montecarlo_distribution` (v1, SIMULATED)

Monte Carlo outcome distributions (EV, seats, popular vote …) as histograms (SIMULATED).

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `run_id` | int | no | Forecast run id. |
| 2 | `election_id` | int | no | Election id (database key of the simulated election). |
| 3 | `subject` | str | no | ev / house_seats / senate_seats / popular_vote_share / governor_wins.; allowed: `ev`, `house_seats`, `senate_seats`, `popular_vote_share`, `governor_wins` |
| 4 | `key` | str | no | Party code or ticket key. |
| 5 | `value` | float | no | Outcome value (EV, seats, share …).; 6 dp |
| 6 | `probability` | float | no | Probability of the value.; 6 dp; ≥ 0 ≤ 1 |

#### `polling_averages` (v1, SIMULATED)

Polling averages of FICTIONAL polls (SIMULATED).

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `election_id` | int | no | Election id (database key of the simulated election). |
| 2 | `as_of` | date | no | Date of the average. |
| 3 | `poll_type` | str | no | Poll type (national_president, province_president, house_district …). |
| 4 | `geo_code` | str | yes | Province/district code (null = national). |
| 5 | `label` | str | no | Party code or candidate label. |
| 6 | `party_code` | str | yes | FICTIONAL party code (null for independents).; **FICTIONAL** |
| 7 | `average_pct` | float | no | Weighted average (percent).; 4 dp; ≥ 0 ≤ 100 |
| 8 | `lower_pct` | float | yes | Lower bound of the uncertainty band (percent).; 4 dp |
| 9 | `upper_pct` | float | yes | Upper bound of the uncertainty band (percent).; 4 dp |
| 10 | `n_polls` | int | yes | Number of polls in the average.; ≥ 0 |
| 11 | `effective_sample_size` | float | yes | Effective sample size after weighting.; 1 dp; ≥ 0 |
| 12 | `trend_pct_per_week` | float | yes | Estimated trend (percentage points per week).; 4 dp |

#### `polls` (v1, SIMULATED)

Individual FICTIONAL polls, one row per poll × result line (SIMULATED).

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `poll_id` | int | no | Poll id. |
| 2 | `election_id` | int | yes | Election id (null for polls not tied to an election). |
| 3 | `pollster` | str | yes | FICTIONAL pollster name.; **FICTIONAL** |
| 4 | `poll_type` | str | no | Poll type. |
| 5 | `geo_code` | str | yes | Province code (null = national). |
| 6 | `district_code` | str | yes | House district code for district polls.; **FICTIONAL** |
| 7 | `start_date` | date | yes | Fieldwork start. |
| 8 | `end_date` | date | no | Fieldwork end. |
| 9 | `sample_size` | int | yes | Sample size.; ≥ 0 |
| 10 | `population` | str | yes | LV / RV / A.; allowed: `LV`, `RV`, `A` |
| 11 | `method` | str | yes | online / phone / mixed / ivr / panel. |
| 12 | `margin_of_error` | float | yes | Reported margin of error (percentage points).; 2 dp; ≥ 0 |
| 13 | `undecided_pct` | float | yes | Undecided (percent).; 2 dp; ≥ 0 ≤ 100 |
| 14 | `label` | str | no | Party code or candidate label. |
| 15 | `party_code` | str | yes | FICTIONAL party code (null for independents).; **FICTIONAL** |
| 16 | `value_pct` | float | no | Result (percent).; 2 dp; ≥ 0 ≤ 100 |
| 17 | `is_fictional` | bool | yes | Always true: polls are generated. |

#### `districts` (v1, FICTIONAL)

House district plan statistics (FICTIONAL districts over REAL geography).

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `plan_id` | int | no | District plan id. |
| 2 | `district_code` | str | no | District code (e.g. NB-07). |
| 3 | `province_code` | str | no | Province code.; **REAL** |
| 4 | `number` | int | yes | District number within the province.; ≥ 1 |
| 5 | `name` | str | yes | Descriptive district name. |
| 6 | `population` | int | yes | Population (REAL CBS counts summed over units).; **REAL**; ≥ 0 |
| 7 | `eligible_voters_est` | int | yes | Estimated eligible voters.; **DERIVED**; ≥ 0 |
| 8 | `target_population` | float | yes | Ideal district population in the province.; **DERIVED**; 1 dp; ≥ 0 |
| 9 | `deviation_pct` | float | yes | (population − target) / target × 100.; **DERIVED**; 4 dp |
| 10 | `area_km2` | float | yes | Area in km².; **DERIVED**; 4 dp; ≥ 0 |
| 11 | `polsby_popper` | float | yes | Polsby–Popper compactness (0–1).; 6 dp; ≥ 0 ≤ 1 |
| 12 | `reock` | float | yes | Reock compactness (0–1).; 6 dp; ≥ 0 ≤ 1 |
| 13 | `convex_hull_ratio` | float | yes | Area / convex-hull area (0–1).; 6 dp; ≥ 0 ≤ 1 |
| 14 | `n_units` | int | yes | CBS buurten in the district.; ≥ 0 |
| 15 | `n_municipalities` | int | yes | Municipalities touched.; ≥ 0 |
| 16 | `n_split_municipalities` | int | yes | Municipalities split with other districts.; ≥ 0 |
| 17 | `urban_share` | float | yes | Population share in CBS urbanity classes 1–2.; 6 dp; ≥ 0 ≤ 1 |
| 18 | `rural_share` | float | yes | Population share in CBS urbanity classes 4–5.; 6 dp; ≥ 0 ≤ 1 |
| 19 | `is_contiguous` | bool | yes | District is one connected piece (water links allowed). |
| 20 | `n_components` | int | yes | Connected components.; ≥ 0 |
| 21 | `centroid_lon` | float | yes | Centroid longitude (EPSG:4326).; **DERIVED**; 6 dp |
| 22 | `centroid_lat` | float | yes | Centroid latitude (EPSG:4326).; **DERIVED**; 6 dp |

#### `apportionment` (v1, FICTIONAL)

House seats and electoral votes per province (FICTIONAL constitution applied to REAL population).

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `apportionment_id` | int | no | Apportionment id. |
| 2 | `method` | str | no | Apportionment method (huntington_hill, hamilton, …). |
| 3 | `province_code` | str | no | Province code.; **REAL** |
| 4 | `province_name` | str | yes | Province name.; **REAL** |
| 5 | `population` | int | no | Apportionment population (REAL CBS).; **REAL**; ≥ 0 |
| 6 | `quota` | float | yes | Exact proportional share of House seats.; 6 dp; ≥ 0 |
| 7 | `seats` | int | no | House seats.; ≥ 0 |
| 8 | `senators` | int | yes | Senators (2 per province canonically).; ≥ 0 |
| 9 | `electoral_votes` | int | no | Electoral votes = seats + senators.; ≥ 0 |
| 10 | `persons_per_seat` | float | yes | Population per House seat.; 2 dp; ≥ 0 |

#### `swing` (v1, SIMULATED)

Swing between two elections per geo × party (lineage-aware; SIMULATED).

| # | column | type | null | notes |
|---|---|---|---|---|
| 1 | `election_id_prev` | int | no | Previous election id. |
| 2 | `year_prev` | int | no | Previous election year. |
| 3 | `election_id_curr` | int | no | Current election id. |
| 4 | `year_curr` | int | no | Current election year. |
| 5 | `race_code` | str | yes | Race code (null when races of a family are pooled). |
| 6 | `race_family` | str | no | Race family (PRESIDENT includes its province contests). |
| 7 | `level` | str | no | Geographic level. |
| 8 | `geo_code` | str | no | Code of the geographic unit (current code set). |
| 9 | `geo_name` | str | yes | Name of the geographic unit. |
| 10 | `province_code` | str | yes | Province code (null for national rows).; **REAL** |
| 11 | `party` | str | no | Party code (_IND = independents pooled).; **FICTIONAL** |
| 12 | `votes_prev` | int | yes | Votes in the previous election.; ≥ 0 |
| 13 | `votes_curr` | int | yes | Votes in the current election.; ≥ 0 |
| 14 | `vote_change` | int | yes | votes_curr − votes_prev. |
| 15 | `vote_change_pct` | float | yes | vote_change / votes_prev × 100.; 4 dp |
| 16 | `share_prev` | float | yes | Share in the previous election.; 6 dp; ≥ 0 ≤ 1 |
| 17 | `share_curr` | float | yes | Share in the current election.; 6 dp; ≥ 0 ≤ 1 |
| 18 | `swing_pp` | float | yes | (share_curr − share_prev) × 100.; 4 dp |
| 19 | `status` | str | no | both / new_party / dropped_party / new_geo / dropped_geo.; allowed: `both`, `new_party`, `dropped_party`, `new_geo`, `dropped_geo` |
| 20 | `winner_prev` | str | yes | Winning party at the geo in the previous election.; **FICTIONAL** |
| 21 | `winner_curr` | str | yes | Winning party at the geo in the current election.; **FICTIONAL** |
| 22 | `flip_status` | str | yes | hold / flip / new / dropped / undecided.; allowed: `hold`, `flip`, `new`, `dropped`, `undecided` |
| 23 | `turnout_prev` | float | yes | Turnout in the previous election.; 6 dp; ≥ 0 ≤ 1 |
| 24 | `turnout_curr` | float | yes | Turnout in the current election.; 6 dp; ≥ 0 ≤ 1 |
| 25 | `turnout_change_pp` | float | yes | (turnout_curr − turnout_prev) × 100.; 4 dp |

