# The electoral system

> **This is a FICTIONAL constitution.** The real Kingdom of the Netherlands is a parliamentary
> monarchy: it has no President, no Electoral College, no directly elected governors or mayors, and
> its Tweede Kamer (150 seats) and Eerste Kamer (75 seats) are elected by nationwide/indirect
> proportional representation. The simulator transplants a U.S.-style federal presidential system
> onto the **real** geography of the Netherlands (12 provinces, the current CBS municipalities and
> CBS neighbourhoods). Everything described in this document — offices, terms, chambers, districts,
> the Electoral College, recount and contingent-election rules — is invented, and every vote count
> the application produces is SIMULATED. Only the geography, the population figures and the two
> statutory size tables quoted in §7 are real.

The rules below are implemented in `src/app/elections/` and configured by
`config/constitution.yaml` (`app.core.constitution.ConstitutionConfig`),
`config/calendar.yaml` (`app.elections.calendar.CalendarConfig`),
`config/recount.yaml` (`app.elections.recount.RecountConfig`) and `config/senate.yaml`
(`app.districts.senate.SenateConfig`). All numbers quoted are those of the canonical constitution;
alternative constitutions can be explored by editing the configuration, and every piece of code
reads the numbers from `app.core.constitution` / `get_constitution()` instead of hard-coding them.

## 1. Offices at a glance

| Office | Number | Constituency | Electoral system | Term | Control / to win |
|---|---|---|---|---|---|
| President + Vice-President (joint ticket) | 1 | the nation, via the Electoral College | provinces' electoral votes, winner-take-all by default | 4 years | **88 of 174 electoral votes** |
| Member of the House (Tweede Kamer) | 150 | single-member districts nested in provinces | first past the post (plurality) | 2 years | **76 seats** |
| Senator (Eerste Kamer) | 24 (2 per province) | the province | first past the post, one seat at a time | 6 years, 3 staggered classes of 8 | **13 seats** |
| Governor (+ Lieutenant Governor on the same ticket) | 12 | the province | first past the post | 4 years | — |
| Provincial legislator (Provinciale Staten) | 39–55 per province (§7) | the province | proportional (D'Hondt) | 4 years | majority of the legislature |
| Mayor | one per municipality | the municipality | first past the post | 4 years | — |
| Municipal council member | 9–45 per municipality (§7) | the municipality | proportional (D'Hondt) or wards | 4 years | majority of the council |

The politics are multiparty (Dutch-style), not two-party: plurality contests routinely have three
to six serious candidates, winners often poll well below 50 %, hung chambers are normal and the
Electoral College can fail to produce a majority (§4).

## 2. Constitutional arithmetic

* **Provinces:** 12 (Groningen, Fryslân, Drenthe, Overijssel, Flevoland, Gelderland, Utrecht,
  Noord-Holland, Zuid-Holland, Zeeland, Noord-Brabant, Limburg) — the U.S. states' analogue.
* **House:** 150 seats apportioned among the provinces by population with the Huntington–Hill
  method (U.S. method of equal proportions), at least 1 seat per province
  (`app.districts.apportionment`). Each province is divided into as many single-member districts
  as it has seats; districts never cross provincial borders.
* **Senate:** 2 senators per province → 12 × 2 = **24**.
* **Electoral College:** each province casts `House seats + 2` electoral votes (EV), so the total
  is **150 + 24 = 174**. A President needs an absolute majority, `174 // 2 + 1 = 88`.
* **Control:** House `150 // 2 + 1 = 76`; Senate `24 // 2 + 1 = 13`.

`majority_of(n) = n // 2 + 1` everywhere. With the 2025 CBS population the apportionment is
(illustrative; recomputed from the data):

| Province | GR | FR | DR | OV | FL | GE | UT | NH | ZH | ZE | NB | LI | Total |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| House seats | 5 | 6 | 4 | 10 | 4 | 18 | 12 | 25 | 32 | 3 | 22 | 9 | 150 |
| Electoral votes | 7 | 8 | 6 | 12 | 6 | 20 | 14 | 27 | 34 | 5 | 24 | 11 | 174 |

## 3. The presidential election and the Electoral College

Parties nominate **tickets** (President + Vice-President). In every province a separate
`PRESIDENT_PROVINCE` contest (`PRES-<PV>`) decides that province's electoral votes; the national
`PRESIDENT` race (`PRES`) is the parent that sums them. Ballot lines are identified by stable
`line_key`s, so a ticket is the same line in every province (a ticket without ballot access in a
province simply receives no votes there).

### 3.1 Allocation methods (`app.elections.electoral_college.allocate`)

`ConstitutionConfig.ev_allocation` / the scenario's `electoral_college.allocation`:

* **`winner_take_all` (default).** The province's popular-vote plurality winner receives *all* its
  electoral votes. Example: Noord-Brabant, 24 EV, ticket A 35.81 % vs ticket B 35.79 % → A 24, B 0.
* **`district`** (Maine/Nebraska analogue). 2 EV (= senators per province) go to the province-wide
  plurality winner and 1 EV to the plurality winner of the presidential vote within each House
  district. Requires `EV − 2` districts per province, which holds by construction.
* **`proportional`.** The province's EV are split among the tickets by largest remainder (Hamilton,
  Hare quota) computed with exact integer arithmetic; equal remainders go to the ticket with more
  votes, then by seeded lot (recorded as `decided_by = 'lot'` only when the lot actually decided
  the last electoral vote). A province-wide first-place tie is irrelevant under this method: the
  tie rule below never withholds proportional electoral votes (it only applies when a province
  has no valid votes at all).

Every method awards exactly the province's EV (or withholds them under the tie rule below); the
engine asserts `allocated + withheld = 174`.

### 3.2 Ties in the Electoral-College contests

An exact first-place tie in a province (or, for `district`, in a district) is first subject to an
automatic recount (§10). If it survives the recount, `ConstitutionConfig.province_tie_rule`
applies:

* **`lot` (default)** — decided by a seeded drawing of lots (`app.core.rng.stable_choice_order`,
  keyed by the contest). A lot already drawn when the (recounted) race was tabulated is honoured by
  the allocation; otherwise the allocation draws it. The outcome records `decided_by = 'lot'`.
* **`contingent`** — the province's electoral votes are *withheld*. Withheld votes still count in
  the denominator: a ticket still needs 88 of 174, so withholding can force a contingent election.

### 3.3 Winning

The ticket with at least 88 electoral votes is elected. Otherwise — an 87–87 tie, or a multiparty
race in which nobody reaches 88 — the **contingent election** (§4) decides.

The 88 is `ConstitutionConfig.presidential_majority`, used whenever the map passed to `allocate`
is the whole Electoral College. A map whose total differs from the constitution (a hand-built
example) uses the majority of that map and logs a warning; a *partial* map (e.g. the provinces
decided so far on election night) must pass `majority=88` explicitly so that nobody "wins" a
majority of a subset. An explicit majority must exceed half of the map total.

### 3.4 Derived statistics (descriptive only)

* **Tipping-point province** (`tipping_point`): sort the provinces by the winner's margin over
  their strongest opponent (percentage points, descending) and accumulate their electoral votes;
  the province that brings the total to 88 is the tipping point.
* **Electoral-vote / popular-vote divergence** (`ev_pv_divergence`): flags when the national
  popular-vote plurality winner differs from the EV winner (or EV leader when nobody reached 88).
  It is reported as a fact of the count, not as a judgement.
* Closest province, largest victory, EV margin, national popular-vote margin.

## 4. The contingent election (`app.elections.contingent.run_contingent_election`)

Configured by `ConstitutionConfig.contingent` (`ContingentElectionConfig`). Defaults:
`mode = province_delegations`, `finalists = 3`, `max_ballots = 10`,
`deadlock_fallback = popular_vote`, `vice_president_by_senate = true`,
`vice_president_finalists = 2`.

### 4.1 Finalists

The finalists are the top `finalists` (3) tickets by electoral votes. Tickets with equal electoral
votes are ordered by national popular vote, then by seeded lot. Tickets without any electoral vote
are not eligible — except that when fewer than two tickets received electoral votes (possible only
when EV were withheld) the list is completed to two with the national popular-vote leaders.

### 4.2 Modes

**`province_delegations` (default; U.S. 12th-Amendment analogue).**

1. The *newly elected* House votes by province delegation; each of the 12 provinces has one vote.
2. Each member votes for the finalist of their own party. A member whose party has no finalist
   votes for the ideologically closest finalist (Euclidean distance between the party ideology
   vectors — economic, social, Europe; ties by national popular vote). Members with unknown
   ideology (independents, parties without a profile) rank the finalists by national popular vote.
3. A delegation casts its vote for a finalist supported by a **strict majority** of its members.
   Otherwise the delegation is **divided** and casts no vote.
4. A finalist needs the votes of a majority of **all** provinces: `majority_of(12) = 7`
   (divided delegations therefore count against everyone).
5. If nobody reaches 7, the finalist with the fewest delegation votes is eliminated (ties: fewer
   individual member votes, then fewer national popular votes, then seeded lot) and its supporters
   move to their next preference. Elimination stops when two finalists remain — balloting between
   the final two can deadlock (e.g. 6–6, or divided delegations).
6. After `max_ballots` (10) ballots without a winner the **deadlock fallback** applies.

**`house_members`.** As above, but every member votes individually and a finalist needs a majority
of all members of the House, `majority_of(150) = 76`.

**`national_popular_vote`.** The finalist with the most national popular votes is elected (exact
tie: seeded lot).

**`national_runoff`.** A national runoff between the top two finalists, simulated by the services
layer (the engine receives `runoff_fn(finalists)` returning the winner or the runoff vote counts).
If no usable runoff result is available, the top two are decided by the general-election popular
vote.

### 4.3 Deadlock fallback

* **`popular_vote` (default)** — the finalist with the most national popular votes becomes
  President (`outcome = 'fallback_popular_vote'`).
* **`vice_president_acts`** — no President is elected; the Vice-President-elect acts as President
  (U.S. 20th-Amendment analogue; `outcome = 'vp_acts'`, `acting_president` names the ticket).

### 4.4 Vice-President

With `vice_president_by_senate`, the **Senate** chooses the Vice-President from the running mates
of the top two tickets by electoral votes (ties by popular vote, then lot). Each senator votes for
their own party's ticket, otherwise for the ideologically closest; a majority of the **whole**
Senate, `majority_of(24) = 13`, is required (with more than two VP finalists the same elimination
rounds apply). If the Senate fails within `max_ballots`, the running mate of the ticket with the
most national popular votes becomes Vice-President. The Senate may choose a Vice-President from a
different ticket than the President.

Without `vice_president_by_senate`, the Vice-President is the running mate of the elected President
(or, if no President was elected, of the national popular-vote leader among the finalists).

### 4.5 Audit trail

Every ballot is stored as a `ContingentRound`: candidates still in contention, tallies, the
required number, each delegation's vote and member breakdown, divided delegations, the eliminated
finalist and a note. `ContingentResult.to_dict()` is the JSON stored in
`contingent_election.ballots_json`. The procedure is fully deterministic for a given seed.

## 5. The House (Tweede Kamer)

150 members elected every two years in single-member districts by plurality. Districts are drawn
inside each province with near-equal population (`app.districts`); they are fictional. A House
office is keyed by its district code (`HOUSE-NB-07`) so it survives redistricting. The chamber is
controlled by a party holding 76 seats; otherwise it is **hung** and
`app.elections.seats.chamber_control` reports the largest party, the seats it is short and the
minimal winning coalitions (descriptive arithmetic only). Members elected without a party (or for a
party that has since dissolved) are counted as `independent` and never "control" a chamber.

## 6. The Senate (Eerste Kamer)

Two senators per province, elected province-wide by plurality, for six-year terms. The 24 seats form
three classes of 8; a province's two seats are always in **different classes**, so after the founding
election (which fills every seat) a province never elects both senators at once
(`config/senate.yaml`). One class is elected at every regular (biennial)
election; control requires 13 seats. A new Senate is the holdover senators plus the seats just
elected (`seats.senate_composition`).

## 7. Provinces and municipalities

* **Governors** are elected province-wide by plurality for four-year terms, together with a
  **Lieutenant Governor** on the same ticket (`ConstitutionConfig.lieutenant_governors`).
* **Provincial legislatures** (fictional powers) are elected by D'Hondt proportional
  representation. Their size follows the **real** Provinciewet, art. 8 lid 1:

  | Inhabitants | ≤ 400,000 | ≤ 500,000 | ≤ 750,000 | ≤ 1,000,000 | ≤ 1,250,000 | ≤ 1,500,000 | ≤ 1,750,000 | ≤ 2,000,000 | > 2,000,000 |
  |---|---|---|---|---|---|---|---|---|---|
  | Seats | 39 | 41 | 43 | 45 | 47 | 49 | 51 | 53 | 55 |

  With the 2025 CBS populations this reproduces the real Staten sizes elected in 2023 (Zeeland 39,
  Flevoland 41, Groningen/Fryslân/Drenthe 43, Overijssel/Limburg 47, Utrecht 49, Gelderland,
  Noord-Holland, Zuid-Holland and Noord-Brabant 55 — 572 seats in total).
* **Mayors** are elected directly (fictional — real Dutch mayors are appointed) by plurality in
  every current CBS municipality, for four-year terms.
* **Municipal councils** are elected by D'Hondt (or, optionally, single-member wards). Their size
  follows the **real** Gemeentewet, art. 8 lid 1:

  | Inhabitants | ≤ 3,000 | ≤ 6,000 | ≤ 10,000 | ≤ 15,000 | ≤ 20,000 | ≤ 25,000 | ≤ 30,000 | ≤ 35,000 | ≤ 40,000 |
  |---|---|---|---|---|---|---|---|---|---|
  | Seats | 9 | 11 | 13 | 15 | 17 | 19 | 21 | 23 | 25 |

  | Inhabitants | ≤ 45,000 | ≤ 50,000 | ≤ 60,000 | ≤ 70,000 | ≤ 80,000 | ≤ 100,000 | ≤ 200,000 | > 200,000 |
  |---|---|---|---|---|---|---|---|---|
  | Seats | 27 | 29 | 31 | 33 | 35 | 37 | 39 | 45 |

  (Statutory population: the CBS count on 1 January of the year before the election. With 2025
  data, three municipalities get 9 seats and eight get 45, as in reality.)

Proportional allocation (`seats.dhondt`, `seats.sainte_lague`, `seats.largest_remainder`) is exact
and deterministic; equal averages competing for the last seat are decided by seeded lot, as the
real Kieswet decides equal averages by lot. An optional vote threshold (share of all votes) can be
applied.

Sources for the two real tables: Gemeentewet art. 8 lid 1 (wetten.overheid.nl, BWBR0005416; 17
brackets) and Provinciewet art. 8 lid 1 (wetten.overheid.nl, BWBR0005645; 9 brackets), both checked
against the statute text, and cross-checked against the real council and Staten sizes. A commonly circulated simplified 7-bracket Provinciewet table (≤ 600k: 41, ≤ 800k: 43,
≤ 1.5M: 47, ≤ 2M: 49) is **not** the statute and is not used.

## 8. Election calendar (`app.elections.calendar.ElectionCalendar`)

* **Election day:** the Wednesday after the first Monday of November (Dutch elections are held on
  Wednesdays) — e.g. 6 Nov 2024, 4 Nov 2026, **8 Nov 2028**. Polls open 07:30 and close 21:00
  Europe/Amsterdam. The rule (month, anchor weekday and occurrence, target weekday) is configurable.
* **Founding general election (2024)** elects the President, the entire House, **all 24 senators**
  and all governors and provincial legislatures. As in the first U.S. Senate, the founding senators
  of class 1 serve 2 years, class 2 4 years and class 3 6 years (`senate.initial_terms`), so that
  from then on exactly one class is up at every regular election.
* **Cycles** (`founding_year + first_year_offset + k × every_years`):

  | Office | Cycle | First | Examples |
  |---|---|---|---|
  | President & Vice-President | 4 years | 2024 | 2028, 2032 … |
  | House | 2 years | 2024 | 2026, 2028 … |
  | Senate class *c* | when its term ends (initial 2*c* years, then 6) | 2024 (all) | class 1: 2026, 2032 · class 2: 2028, 2034 · class 3: 2030, 2036 |
  | Governors, provincial legislatures | 4 years (presidential years) | 2024 | 2028, 2032 … |
  | Mayors, municipal councils | 4 years, offset 2 | 2026 | 2030, 2034 … |

  | Year | Type | On the ballot |
  |---|---|---|
  | 2024 | general (founding) | President, House, Senate classes 1–3, governors, provincial legislatures |
  | 2026 | midterm | House, Senate class 1, mayors & councils |
  | 2028 | general | President, House, Senate class 2, governors, provincial legislatures |
  | 2030 | midterm | House, Senate class 3, mayors & councils |
  | 2032 | general | President, House, Senate class 1, governors, provincial legislatures |

  All regular elections of a year are held on that year's election day. The Senate rotation is
  derived from the configured initial terms (not a lookup table) and validated: every class is up
  every 6 years and exactly one class is up at each biennial election after the founding.
* **Terms** begin on 15 January after the election for federal offices (President, Vice-President,
  House, Senate) and on 1 January for provincial and municipal offices; a term ends when the
  successor's begins (`term_bounds`). `term_bounds` for a Senate seat takes the seat's class
  (required at the founding election) and refuses a class that is not up that year — a special
  election fills only the remainder of the current term.
* **Terms are constitutional, the calendar only chooses the years.** `config/calendar.yaml` leaves
  cycle and term lengths to `config/constitution.yaml` (presidential, House, Senate, governor and
  mayor terms). Explicit values that contradict the constitution, or an office elected more or
  less often than its term lasts, are rejected when the calendar is loaded
  (`ElectionCalendar.validate`). Changing `house_term_years` to 3 therefore moves the whole
  rotation (House every 3 years, Senate classes up after 3/6/9 years) without editing the calendar.

## 9. Tabulation and certification (`app.elections.tabulation`)

* Votes are counted per CBS neighbourhood (buurt, the precinct substitute) and aggregated with exact
  integer sums to municipality → (House district) → province → national. `reconcile` proves that
  every level sums exactly to the next (line votes, valid votes, ballots cast, eligible, blank,
  invalid) and that `valid + blank + invalid = ballots cast ≤ eligible` everywhere.
* A race is won by the plurality of valid votes. Margins are reported in votes and in percentage
  points of valid votes. An exact first-place tie triggers a recount (§10) and, if it persists, a
  seeded drawing of lots (`decided_by = 'lot'`). A race with no valid votes at all is a tie of all
  its lines; election-night code tabulating partial counts passes `resolve_ties=False` so that such
  a race has no winner yet.
* Inputs are validated rather than coerced: duplicate line keys, a unit listed twice, negative
  counts, fractional ("non-whole") ballot counts and inconsistent `RaceVotes`
  (`valid + blank + invalid ≠ ballots cast`, or more ballots than eligible voters) raise
  `ElectionError` instead of being silently truncated or double-counted.
* A withdrawn candidate's name stays on the printed ballot: those votes count and the candidate can
  still win (the office then falls vacant, §11). A line whose party has disappeared is tabulated
  normally and its winner is counted as independent in chamber compositions.

## 10. Recounts (`app.elections.recount`, `config/recount.yaml`)

Automatic recount when the margin between the top two lines is within the threshold (percentage
points of valid votes; optionally also/alternatively a vote threshold) or the race is an exact tie:

| Race type | Automatic recount if margin ≤ |
|---|---|
| `PRESIDENT_PROVINCE` (a province's EV contest) | 0.25 pp |
| `HOUSE` | 0.5 pp |
| `SENATE` | 0.25 pp |
| `GOVERNOR` | 0.25 pp |
| `MAYOR` | 0.5 pp |
| any of the above | exact tie |

The national `PRESIDENT` race and proportional legislatures/councils have no automatic recount.

A recount re-examines **the same ballots** — it never re-simulates the election. A seeded sample of
units (10 %, at least 5, at most 250) is re-examined; 60 % of them are confirmed unchanged and the
others receive one small correction of 1–3 ballots: *misread tally* (ballots moved between lines),
*uncounted ballot found* (never above the number of eligible voters), *ballot ruled invalid*, or
*invalid ballot ruled valid*. Every correction is recorded (unit, line, before, after, delta, reason)
and the recounted counts satisfy all invariants. If the recounted race is still an exact tie it is
decided by seeded lot; otherwise the recounted result is certified (`decided_by = 'recount'`).
The lot uses `tie_seed` (pass the seed of the official tabulation, so the provisional winner of a
tied race is reproduced exactly and `outcome_changed` only reports genuine changes). Each
`RecountAdjustment` unpacks as `(unit_index, line_index, votes_before, votes_after, delta, reason)`;
`line_index = -1` denotes the invalid pile.

## 11. Vacancies and special elections

A vacancy (death, resignation, a withdrawn candidate who won, …) is filled by a special election on
the next regular election day that is at least 90 days after the vacancy occurred; if the next
election day is closer, the special election is held on the following one
(`ElectionCalendar.special_election_date`). The winner serves the remainder of the term. A Senate
special election for a seat whose class is not up is held alongside the regular election.

## 12. Determinism

All randomness (lots, recount samples, contingent-election tie-breaks) flows from
`app.core.rng.make_rng(seed, *stream_keys)` / `stable_choice_order`, keyed by the contest, so the
same seed and configuration always produce identical results and unrelated contests never perturb
each other.
