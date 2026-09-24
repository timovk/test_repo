# Political model and election simulation

This document specifies how the NL Federal Election Simulator produces votes.  Votes are never
drawn as independent random percentages: they **emerge** from a probabilistic model of a
simulated electorate living in the REAL Dutch geography.

> **REAL vs FICTIONAL.** Geography, population and demographics are REAL (CBS).  The parties,
> candidates, regions-as-political-cultures, every coefficient, the baselines and all results are
> FICTIONAL or SIMULATED.  Nothing produced by this model is a statement about real Dutch politics.

Code: `src/app/simulation/` (pure engines — NumPy/pandas in, dataclasses out, no SQLAlchemy) and
`src/app/scenarios/` (scenario loading/editing).  Configuration: `config/model.yaml`
(model-wide hyperparameters), `config/regions.yaml` (named regions), `config/parties/*.yaml`
(shared party definitions) and `config/scenarios/*.yaml` (one election each).

---

## 1. Overview

```
REAL CBS demographics z_uk ─┐
FICTIONAL party parameters ─┼─► persistent utility U0[u,p]  ──► preference shares s = softmax(U0)
persistent lean field ──────┘            │                                  │
                                         │ calibration (α, δ, τ0)           ▼
persistent turnout logit τ_u ─────────────────────────────────► vote shares π, turnout T
                                                                            │
election environment + context (campaigns, polls) + ONE DRAW OF SHOCKS ─────┤
                                                                            ▼
                         ballots_u ~ Binomial(eligible_u, T_u)   (election-wide)
                                                                            │
      per race: ballot composition (absent/withdrawn/independent) → candidate effects
                → strategic voting → blank/invalid ~ Binomial → votes ~ Multinomial
```

Levels: provinces (12) → municipalities (M; 342 in the 2025 vintage — never hard-coded) → units
(U ≈ 14.7k CBS *buurten* used as precincts).  All arrays are aligned with
`app.geography.frame.GeographyFrame`.

Notation: `u` unit, `m(u)` its municipality, `v(u)` its province, `p` party, `ℓ` ballot line,
`σ(x) = 1/(1+e^{−x})`, `E_u` eligible voters (DERIVED CBS estimate).

---

## 2. The persistent (structural) model — `structural.py`

`StructuralModel.build(frame, scenario, model_config=None, regions=None, *, strict=False)`

### 2.1 Utility

```
U0[u,p] = α_p                                   party intercept (calibrated)
        + Σ_k β_pk · z̃_uk                       demographics
        + urb[p, c(u)]                           CBS urbanity class c ∈ {1..5}
        + prov[p, v(u)]                          province shift
        + Σ_r reg[p,r] · 1[m(u) ∈ r]             named regions (config/regions.yaml)
        + muni[p, m(u)]                          municipality shift (CBS code)
        + lean[u,p]                              persistent local lean
        + δ[u,p]                                 calibration adjustments (pinned baselines)
```

* `z̃_uk` are the population-weighted z-scores of the REAL CBS indicators
  (`GeographyFrame.unit_demo_z`; variables `DEMOGRAPHIC_VARIABLES`).  Values the geography
  pipeline **imputed** (flagged in `unit_demo_imputed`) are shrunk towards the mean by
  `lean.imputed_shrinkage` (0.5); values that are still missing (NaN) count as the mean and are
  reported in `model.warnings`.
* `β`, `urb`, `prov`, `reg`, `muni` come from `PartySpec.demographics / urbanity / provinces /
  regions / municipalities` (logit points).  Unknown demographic variables, provinces or regions
  are errors; unknown municipality codes are warnings (errors with `strict=True`) so the REAL demo
  scenarios can run on the synthetic test country.

### 2.2 Persistent lean field (FICTIONAL, stable across elections)

Drawn once from the scenario's `political_geography_seed` (all demo scenarios share 1848, so every
municipality keeps its character from election to election):

```
lean[u,p] = spatial_sd · G[m(u),p] + municipality_sd · I[m(u),p] + unit_sd · η[u,p]

G[:,p] = √ρ · (F_sp · ι_p)/‖ι‖ + √(1−ρ) · g_p        F_sp[:,d], g_p ~ GP(0, K)   (unit variance)
I[:,p] = √ρ · (F_iid · ι_p)/‖ι‖ + √(1−ρ) · i_p       iid N(0,1)
η[u,p] ~ N(0,1)
```

`K` is a Matérn-3/2 kernel (configurable: exponential, squared-exponential) over municipality
centroids (EPSG:28992) with length scale `spatial_length_km`; fields are drawn as `L z` with
`L = chol(K + jitter·I)` (`spatial.gp_cholesky`, cached; jitter grows until positive definite).
`ι_p` is the party's ideology vector: a share `ρ = ideological_share` of the lean variance lies on
three shared ideological axes, so a place that leans left leans towards *all* left parties.
Every party's draws use its own RNG stream, so adding a party does not change the others.

### 2.3 Turnout

```
τ_u   = τ0 + Σ_k γ_k z̃_uk + t_urb[c(u)] + ε^τ_{m(u)} + ε^τ_u          (persistent turnout logit)
q[u,p] = clip(σ(τ_u + t_p), min_probability, max_probability)         (turnout of p's supporters)
s[u,·] = softmax(U0[u,·])                                              (preferences of the eligible)
T_u   = Σ_p s[u,p] q[u,p]                                              (expected turnout)
π[u,p] = s[u,p] q[u,p] / T_u                                           (vote shares among voters)
```

`γ` (`config/model.yaml → turnout.demographics`) encodes the documented demographic turnout
gradient: older, highly educated, higher-income, owner-occupier neighbourhoods vote more; young,
low-education, non-European-origin neighbourhoods less.  `t_p` is `PartySpec.turnout_propensity`,
so the party mix of a neighbourhood also moves its turnout (the "turnout propensity mix").

### 2.4 Calibration

Targets are **vote shares** (turnout-weighted), matching `PartySpec.base_share` (normalised) or
`calibration.national` (listed shares; unlisted parties share the remainder in proportion to their
`base_share`).  Fixed-point iteration (Gauss–Seidel, vectorised):

```
α_p  += log(target_p·V − V_pinned,p) − log(V_unpinned,p)       (national, through unpinned units)
τ0   += (T* − T) / (∂T/∂τ0)                                    (national turnout = turnout_base)
δ_P  += log(target_P·V_P − V_P,pinned munis) − log(V_P,rest)   (pinned provinces)
δ_M  += log(target_M / share_M)                                (pinned municipalities)
```

Each geography is steered through the units it controls: pinned municipalities through their own
units, pinned provinces through their remaining units, the national intercepts through everything
else — so province/municipality targets and national shares are met simultaneously when they are
consistent.  Typical convergence: < 20 iterations to 1e-7 (≈ 0.2 s on the real country).
`model.calibration` (`CalibrationReport`) records targets, achieved values, iterations and the
maximum error; inconsistent targets produce a warning, never silent drift.

Local targets (`calibration.provinces`, `calibration.municipalities`) may list a subset of parties:
listed parties get their target, unlisted ones share the remainder in proportion to their current
shares (rows listing every party are normalised).

### 2.5 Elasticity

Persistent swinginess `e_u` (eligible-weighted mean 1) scales national swings (the scenario's
national environment, events, poll shifts, national shocks and the midterm penalty):

```
raw_u = w_H · z(H_u) + w_S · z(suburban_u) + w_N · ξ_{m(u)}
e_u   ∝ exp(log_sd · z(raw_u)) ^ elasticity_strength,  clipped to [min, max], renormalised to mean 1
```

`H_u` is the normalised entropy of the unit's baseline party shares (support spread over many
parties is less entrenched), `suburban_u` scores CBS urbanity classes (mid-density suburbs swing
most), `ξ` is a persistent municipal draw.  `environment.elasticity_strength = 0` gives uniform swing.

### 2.6 Affinity

Ideological distance `d(a,b) = ‖W(ι_a − ι_b)‖` with dimension weights
`affinity.dimension_weights` (economic, social, europe).  `model.affinity_matrix()` shows the
transfer weights `softmax(−d/temperature)` between parties.

### 2.7 Deterministic election environment

`environment.national` (× `e_u`), `environment.provinces`, and `environment.events`
(`{name, national, provinces, turnout, probability}` — see `structural.EventSpec`) form the known
environment of the scenario's election.  An event with `probability < 1` enters the expectation as
`probability × effect` and occurs or not in each draw.

### 2.8 Expectations (no shocks)

* `model.party_state(units)` — preferences, turnout, vote shares;
* `model.expected_shares(units, parties)` — valid-vote shares when only `parties` are on the ballot;
* `model.aggregate_expected_shares(level, parties)` / `national_shares` / `province_shares` /
  `municipality_shares` — weighted by expected valid votes (eligible × turnout × non-abstention);
* `model.unit_expected_turnout()`, `model.expected_turnout_by(level)`,
  `model.jurisdiction_shares(units, parties)`.

---

## 3. One election draw — `voting.py`

`simulate_election(model, races, seed, context=None) -> ElectionDraw`

### 3.1 Context

`ElectionContext` carries what is election-specific but not structural: year, election type,
`president_party` (defaults to the party of the scenario candidate with `incumbent_office: PRES`),
incumbents per race (`IncumbentInfo`), the candidate lookup (key → `CandidateSpec`),
`campaign_effects` (race key → {party|line: shift}, or `(level, code)` / `"level:code"` →
{party: shift} for every race in a national/province/municipality area), `turnout_effects`
(`(level, code)` → turnout logit shift) and `national_shifts` (e.g. from polls; × `e_u`).

### 3.2 Shocks and their correlation structure

Every component has its own named RNG stream (`make_rng(seed, "shock", …, party)`), so unrelated
changes never perturb other draws.  SDs come from the scenario (`environment.shocks`):

| component | distribution | scope | notes |
|---|---|---|---|
| national | `national_sd · (√(1−ρ)·t_ν,p·√((ν−2)/ν) + √ρ·ι_p·ξ/‖ι‖)` | per party | Student-t tails (`tail_df`), common ideological swing share `ρ = shocks.ideological_swing_share`; × `e_u` |
| province | `N(0, province_sd²)` | province × party | |
| municipality | `N(0, municipality_sd²)` | municipality × party | |
| spatial | `spatial_sd · L z`, `z ~ N(0, I)` | municipality × party | GP over centroids (`spatial_length_km`), cached Cholesky → neighbouring municipalities move together |
| unit | `N(0, unit_sd²)` | unit × party | |
| turnout | national `N(0, turnout_national_sd²)`, municipal `N(0, turnout_local_sd²)`, unit `N(0, (unit_shock_share·turnout_local_sd)²)` | | plus a differential mobilisation `N(0, party_turnout_shock_sd²)` of each party's supporters |
| events | Bernoulli(`probability`) | per event | deviation from the expectation |
| race line | `N(0, race_line_sd²)` | race × line | candidate-specific campaign performance |

`ElectionDraw.environment` records all realised components (national, ideological swing,
province, municipal iid/spatial/turnout arrays, events, turnout, race-line shocks, strategic
defections) as JSON-serialisable data for audit.

### 3.3 Turnout is election-wide

```
ballots_u ~ Binomial(E_u, T_u(shocked))
```

shared by every race: one voter casts one ballot paper per race, so a unit's `ballots_cast` is the
same in all races (`RaceVotes.check()` reconciles votes + blank + invalid = ballots).

### 3.4 Ballot composition (per race)

Party vote shares `π` (n, P) are mapped to line masses by a linear routing matrix
(`structural.routing_matrix`): `m = π R + m_ind`, undervote `a = π · a_p`.

* a party with active lines splits its support over them;
* a party **absent** from the ballot transfers its support by affinity,
  `softmax_ℓ(−d(p, ℓ)/temperature)`, except `absent_abstain_share` which abstains in that race
  (undervote → counted as **blank** in that race, because turnout is election-wide);
* a **withdrawn** line keeps `withdrawn_residual_share` of its party's support (votes for a name
  still printed on the ballot); the rest is transferred like an absent party;
* an **independent** line (party `None`) has mass `independent_base_share` (per race type:
  `independent_base_share_by_race`, larger for mayors), ideology from the candidate (neutral
  otherwise) and an extra transfer distance `independent_extra_distance`.

### 3.5 Candidate and race effects

```
W[u,ℓ] = log m[u,ℓ] + Δ[u,ℓ] (+ race-line shock),     sincere shares = softmax_ℓ(W)
Δ = quality_utility[race type] · quality_ℓ           (independents: independent_quality_utility)
  + incumbency[race type] · 1[incumbent]             (environment.incumbency)
  + open_seat_party_bonus · 1[party held the open seat]
  − midterm_penalty · e_u · 1[midterm ∧ president's party ∧ House/Senate]
  + home_municipality_bonus[race type] · 1[m(u) = home]
  + home_province_bonus · 1[v(u) = home province]    (tickets: president.home_province_bonus)
  + running mate: quality share, home municipality and president.vp_home_province_bonus
  + campaign effects for this race (by party or line key)
```

### 3.6 Strategic voting (FPTP and winner-take-all, single seat)

From the **expected** (no-shock) jurisdiction shares `S_ℓ` — what voters know before the
election — each active line more than `viability_gap_start` behind the second-placed line loses a
fraction

```
f_ℓ = strategic_voting · race_type_weight · clip((S_2nd − S_ℓ − gap_start)/(gap_full − gap_start), 0, 1)
```

of its supporters to the viable lines (the top `viable_lines`, default 2) by affinity
(`strategic.temperature`).  This is a linear map `G`: `shares = sincere @ G`.  Proportional
multi-seat races (councils) are never subject to it.

### 3.7 Counting

```
invalid ~ Binomial(ballots, invalid_rate · m_inv,u)        (m_inv: +15 %/SD low education, mean 1)
blank   ~ Binomial(ballots − invalid, p_blank / (1 − p_invalid)),
          p_blank = blank_rate · m_blank,u + (1 − …) · undervote_u
votes   ~ Multinomial(ballots − invalid − blank, shares)    (vectorised Generator.multinomial)
```

`RaceVotes.expected_shares` / `expected_turnout` hold the deterministic no-shock expectation (§2.8
plus ballot composition, candidate effects and strategic voting), which the race-calling and
forecasting engines use as the pre-election model view.  `expected_race_shares(model, race,
context)` returns the same (n, L) array without simulating; `race_expectation(...)` returns the
full `RacePlan` (unit shares, turnout, jurisdiction shares, strategic defection fractions), whose
`shares(vote_share, line_shock)` method can be reused across Monte Carlo draws.

A `PRESIDENT` parent race passed together with its `PRESIDENT_PROVINCE` children is assembled
from the children, so the national popular vote is exactly the sum of the province contests.

### 3.8 Performance

Everything is vectorised over units; per race the work is a few (n × P)·(P × L) products.
Measured on the REAL country (14,729 units): model build ≈ 0.2–1.0 s (first call includes the
Cholesky factor), one full general election with 183 races (12 + 1 presidential, 150 House,
8 Senate, 12 governors) ≈ 1.1 s, a midterm with 500 races (incl. 342 mayors) ≈ 0.9 s, the 13
presidential races alone ≈ 0.05 s.

---

## 4. Latent variables and where they live

| latent variable | where | category |
|---|---|---|
| neighbourhood demographics (age, household, origin, education, income, housing, density, urbanity) | `GeographyFrame.unit_demo` | REAL (CBS; imputed gaps DERIVED + flagged) |
| eligible voters | `GeographyFrame.unit_eligible` | DERIVED |
| party ideology, base share, turnout propensity | `PartySpec` (`config/parties/`) | FICTIONAL |
| demographic / urbanity / province / region / municipality effects | `PartySpec` | FICTIONAL |
| named political regions (Bible Belt, Catholic south …) | `config/regions.yaml` | FICTIONAL grouping of REAL places |
| persistent local lean (spatial GP + iid) | `StructuralModel.components`, seeded by `political_geography_seed` | FICTIONAL |
| persistent turnout propensity of places | `StructuralModel.tau`; `config/model.yaml → turnout` | FICTIONAL coefficients on REAL inputs |
| elasticity (swinginess) | `StructuralModel.elasticity` | FICTIONAL |
| baseline calibration targets | `PartySpec.base_share`, `calibration.*` | FICTIONAL (or DERIVED when imported, §6) |
| national environment, events | `environment.*` | FICTIONAL |
| candidate quality, home, incumbency | `CandidateSpec`, `BallotLine`, `environment.incumbency` | FICTIONAL |
| campaign effects | `ElectionContext.campaign_effects` (from the campaigns engine) | SIMULATED |
| election shocks, turnout, votes | `ElectionDraw` | SIMULATED |
| affinity, abstention, strategic voting, withdrawn residual | `config/model.yaml`, `environment.strategic_voting` | FICTIONAL |

---

## 5. Candidates — `candidates.py`

`generate_race_candidates(model, race_key, race_type, unit_index, rules, seed, …)` and
`generate_down_ballot(model, slots, seed, …)`:

* a party contests when its `ContestRule` says so: `always`, or expected jurisdiction share ≥
  `min_expected_share` and (optionally) the race's province is listed and/or ≥
  `region_overlap_threshold` (25 %) of the jurisdiction's eligible voters live in a listed region;
  at most `max_candidates` lines, never fewer than two;
* the sitting office holder runs again with probability `incumbent_runs_again_prob`;
* `explicit_candidates[race]` overrides generation;
* an independent joins with probability `independents_prob`;
* people are FICTIONAL: `fictional_name(rng, gender, province=…, heritage_prob=…)` combines common
  Dutch given names and surnames (with *tussenvoegsels*; regional Frisian/Groningen and
  Brabant/Limburg pools; migrant-heritage names with probability tied to the home municipality's
  non-European-origin share); surnames of prominent politicians are excluded and known full names
  are blocked; quality `N(0, candidate_quality_sd)`; home municipality drawn by eligible voters
  within the jurisdiction; birth date from `candidates.age_range`; a short "Fictional …" bio.
* keys are stable slugs with a seeded suffix, unique across a batch; RNG streams are keyed by
  `(seed, "candidates", race_key, party)`.

`races.py` builds `RaceSpec`s: `ticket_lines`, `presidential_races`, `house_slots`,
`senate_slots`, `governor_slots`, `mayor_slots`, `races_from_candidates`.  Party-list
(council) races use lines with `key = party code` and `electoral_system =
proportional_dhondt`; the voting engine supports them unchanged (no strategic voting).

---

## 6. Baselines — `baselines.py`

### 6.1 Defining baselines by province or municipality

```yaml
calibration:
  national: {PA: 0.25, VLP: 0.21}          # overrides base_share (others fill the rest)
  provinces:
    NB: {CVU: 0.30, NVB: 0.18}             # pinned province baseline
  municipalities:
    GM0855: {PA: 0.33}                     # Tilburg
```

### 6.2 Importing historical results (clearly labelled)

`import_historical_results(csv_path, mapping_yaml, frame)` reads a CSV of REAL municipality-level
results (`municipality_code, party, votes`) and a YAML mapping from real party names to model
parties with weights (one real party can be split over several model parties).  The output
(`ImportedBaseline`) is labelled

> **DERIVED from real election results via mapping — not a result of this fictional system**

(`data_category = DERIVED`); municipality codes of other vintages are reported and skipped,
unmapped parties dropped (or an error with `unmapped: error`).  It is only used when a scenario
sets `calibration.imported_baseline: <mapping.yaml>` (the mapping names its `source_csv`); the
municipality targets are then `w · imported + (1 − w) · model` with
`imported_baseline_weight = w`.  `config/baselines/example/` ships a tiny **synthetic** example
(invented party names and numbers, synthetic-country municipality codes) for tests and docs.

### 6.3 Analysis helpers (spec §13)

All return DataFrames of model expectations tagged `data_category = SIMULATED`:
`province_lean(model, parties)`, `municipality_vs_rest_of_province(model, "Tilburg")`,
`urban_coalition_index(model)`, `elasticity(model, level)`, `elasticity_ranking(model, top)`,
`province_competitiveness(model, parties)` (pass the presidential tickets' parties to see the
Electoral College battlegrounds).

---

## 7. Scenarios — `app.scenarios.loader` / `app.scenarios.editor`

* `load_scenario(path_or_slug)` resolves `config/scenarios/<slug>.yaml` or any file whose
  `scenario.slug` matches, applies the loader directives `parties_file`, `candidates_file` and
  `party_overrides`, and validates (`ScenarioError` on failure);
* `dump_scenario(doc)` writes the fully merged document (round-trips exactly);
  `list_scenarios()`, `duplicate_scenario(doc, new_slug, new_seed=None)`, `save_scenario`;
* `validate_scenario(doc, frame, regions)` lists unknown province/municipality codes, undefined
  regions, inconsistent candidate homes, malformed race codes, Senate classes outside the
  constitution, missing imported baselines …;
* `apply_edits(doc, edits, frame=None)` applies editor operations (`set_national_environment`,
  `set_province_environment`, `set_candidate_quality`, `set_party_base_share`,
  `set_party_field`, `add_ticket`, `remove_ticket`, `withdraw_ticket`, `set_ev_allocation`,
  `set_seed`, `set_political_geography_seed`, `add_candidate`, `remove_candidate`, generic
  `set` with dotted paths) to a copy and re-validates.

### 7.1 The demo scenarios (FICTIONAL)

Eight fictional parties (`config/parties/fictional.yaml`) modelled on Dutch political families:
PA (green-left), SAP (left-populist), VLP (economic-liberal), DM (social-liberal), CVU
(Christian-democratic), NVB (national-populist), PLB (agrarian; renamed *Plattelands- en
Regiopartij* in 2027) and RV (orthodox-Protestant).  All share `political_geography_seed: 1848`.

| scenario | slug | contests | design (measured on the REAL country, shocks only, 600–1000 draws) |
|---|---|---|---|
| `founding_2024.yaml` | `founding-2024` | President, all 150 House seats, all 24 Senate seats (classes 1–3), 12 governors | VLP ticket wins outright ≈ 57 %, contingent election ≈ 42 %; CVU carries Overijssel and contests Brabant; NVB the periphery |
| `midterm_2026.yaml` | `midterm-2026` | House, Senate class 1, mayors + councils | president's party (VLP) pays the midterm penalty; agrarian surge; turnout ≈ 63 % |
| `general_2028.yaml` | `demo-2028` | President, House, Senate class 2, 12 governors | PA ≈ 47 % outright, incumbent VLP ≈ 15 %, contingent ≈ 38 %; ZH/UT/GE are toss-ups, CVU favoured in Brabant, NVB carries LI/DR/FR/ZE |

(Probabilities move once campaigns and polls are layered on; they are design targets, not
promises.)

---

## 8. Reproducibility

* All randomness flows from `app.core.rng.make_rng(seed, *keys)`; no global RNG state.
* Persistent draws use `political_geography_seed`, election draws the election seed; every
  component, party, race and line has its own stream, so unrelated edits do not perturb other draws
  (tested: removing races leaves the remaining races' votes unchanged).
* Same model config + scenario + frame + seed ⇒ bit-identical `ElectionDraw`;
  `model.fingerprint` hashes scenario + model config + frame size and is stored in
  `ElectionDraw.environment`.

---

## 9. Limitations

* The vote model is a multinomial logit at neighbourhood level; within-unit heterogeneity and
  individual-level voting are not modelled.
* Absent-party supporters abstain at one configurable rate everywhere; strategic viability uses
  the jurisdiction's expected shares only (no poll-driven information asymmetry).
* Expected shares are the softmax at zero shocks (the median-like path), not the mean over shocks.
* Local parties in municipal elections are represented only by independents; council races need
  party-list lines built by the caller.
* Candidate names are random combinations; although known politicians' names are blocked, an
  accidental match with a real private person's name is possible (all people are fictional).
* The demo coefficients are qualitative choices inspired by general knowledge of Dutch electoral
  geography; they are not estimated from real results and should not be read as such.
