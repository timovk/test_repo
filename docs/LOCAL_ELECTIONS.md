# In-between local elections

Between the big November elections (the presidential general elections and the midterms), the
FICTIONAL republic holds many small **local elections**: school boards, water boards, ballot
measures, special elections for vacant offices and mayor recalls. They run on the REAL Dutch
geography; every candidate, measure, vote and result is FICTIONAL or SIMULATED.

The regular mayor and municipal council elections stay at the midterms. Local elections add
contests in between.

## The calendar

`config/local.yaml` → `app.elections.local_calendar.LocalCalendar`.

- **Local election days.** Each province holds `days_per_province` (4) local election days a year.
  Each day is the *n*-th Wednesday of a month, drawn from `months` (February–June and September).
  October–January are excluded, so a local day is never close to a November election, and July
  and August are the summer holidays.
- **Each municipality has its own day.** Every municipality is dealt one of its province's days
  (its *slot*), balanced within the province, so it votes on the same day every year. The same
  applies to water boards.
- **One local election = one date.** Every province voting that day is on the same ballot, with
  one combined election night, e.g. *"Local Elections · 7 February 2029"* (Overijssel, Zeeland and
  Noord-Brabant). A date with nothing on the ballot is skipped.
- Everything is a pure function of the configuration, the geography and `seed`, using keyed
  random streams. Dates, ballots and candidates never depend on the order in which elections are
  created.

At the default settings the 48 province days of a year fall on about 17 dates, so there are
about 17 local elections a year, with one to four provinces and typically 15–70 races each
(median about 40). Big provinces (Zuid-Holland, Noord-Brabant) hold larger ballots than Flevoland
or Zeeland.

The world clock ([CLOCK.md](CLOCK.md)) moves from one election day to the next and creates each
local election when it reaches its date.

## What is on the ballot

| Contest | Race type | How it is won |
|---|---|---|
| School board of a municipality | `SCHOOL_BOARD` | nonpartisan, **vote for up to N**, the top N win |
| Board of a water authority (waterschap) | `WATER_BOARD` | nonpartisan, vote for up to N, the top N win |
| Ballot measure ("Proposition 1", "Measure A") | `BALLOT_MEASURE` | Yes/No; passes at the topic's threshold |
| Special election for a vacant mayoralty | `MAYOR` (special) | partisan, plurality, for the rest of the term |
| Special election for one vacant council seat | `COUNCIL_SEAT` | partisan, plurality, until the next council term |
| Mayor recall | `RECALL` + `MAYOR` (special) | Yes/No; if it passes, the replacement race decides |

### School boards

Seats depend on population (`seats_by_population`: 5, 7 or 9). Terms are four years and
staggered:

- the municipality's first school board election fills every seat, half for two years and half
  for four;
- afterwards half the seats are up every two years, in the municipality's even or odd years.

At the first election the top vote-getters take the four-year seats. After that, re-elected
incumbents keep their seat.

### Water boards

The 21 water authorities are REAL areas (Het Waterschapshuis via PDOK; see
[DATA_PROVENANCE.md](DATA_PROVENANCE.md)). A water board crosses municipal and provincial
borders. It is elected in the province holding most of its voters, on that province's local day
in the board's slot, and its voters in other provinces vote too. Every seat is up every four
years, in a board-specific year.

### Ballot measures

Each municipality gets a Poisson number of measures per local day:
`rate × (population / 50,000)^population_elasticity`, at most `max_per_day`.

Measures are drawn from the FICTIONAL topic library in `config/local.yaml` (`topics`): taxes,
bonds, charter amendments, zoning and policy questions. Each topic has:

- a **lean**: the YES side's position on the model's ideology axes (economic, social, Europe);
  the NO side takes the opposite;
- an **appeal**: a logit shift of YES, plus `measures.base_appeal`, which calibrates the overall
  pass rate (about 55% at the default settings);
- a **threshold**: the YES share needed to pass.
  - Simple majority (0.5): YES must get *more* votes than NO, so a tie fails.
  - Supermajorities (0.6 for bonds, two thirds for charter amendments): YES must reach *at least*
    the threshold.

Measure labels follow the municipality's style ("Proposition 1, 2, …", "Measure A, B, …" or
"Proposition A, B, …").

### Special elections and recalls

`LocalCalendar.office_events` draws, per municipality and year, the following events:

- mayor vacancies (`vacancies.mayor_rate`, with reasons resigned / died / appointed elsewhere /
  removed);
- one-seat council vacancies (`vacancies.council_rate`);
- qualifying mayor recall petitions (`recalls.mayor_rate`; a share of them follow a scandal).

The services decide which events reach a ballot:

- the office must be held the day before the event;
- a mayor must have served at least `min_days_in_office` before being recalled;
- the regular midterm must not come within `regular_election_window_days` after the special
  election date;
- an office can have only one pending special election or recall at a time.

The special election is held on the municipality's first local day at least `min_days_before`
(90, or 60 for recalls) days after the event.

- **Special elections** fill the office for the rest of the term. The holder whose office became
  vacant has their term ended on the vacancy date when the special election is certified.
- **A recall ballot** holds the Yes/No question plus a replacement race (`MAYOR`, `is_special`,
  with the recall as its parent).
  - If the recall passes, the replacement winner takes office and the recalled mayor's term ends
    with `end_reason = "recalled"`.
  - If it fails, the replacement race is counted but marked moot and elects nobody.

## How the votes are simulated

The ordinary structural model ([SIMULATION.md](SIMULATION.md)) simulates local elections. The
national environment comes from the scenario of the latest regular election before the local
election.

- **Turnout** is off-cycle: a logit shift of `turnout_logit_shift` (−1.25), giving about 30–40%
  instead of about 75%.
- **Nonpartisan candidates and the YES/NO sides** are ballot lines without a party, each with a
  position. Every party's supporters reach them by ideological closeness (the routing used for
  independents). A nonpartisan candidate's position is drawn around a party supported in the
  jurisdiction; that party is not shown on the ballot.
- **Vote for up to N.** Every valid ballot marks one to N candidates, on average
  `1 + at_large_mark_fill × (N − 1)`. Mark shares are flatter than first preferences, and a
  candidate gets at most one mark per ballot. In stored results, `valid_votes` counts valid
  ballots; shares are of all marks.
- **Uncertainty.** Measures and recalls get large race-specific shocks
  (`race_line_sd_by_race`). Voters skip contests lower down the ballot (`undervote_by_race`).

## Election night and calling

A local election has its own short election night. Only the voting municipalities report, and
most local nights are counted by about 01:00.

The race caller ([RACE_CALLING.md](RACE_CALLING.md)) handles the contest rules:

- **Vote for up to N.** Each draw ranks the lines. A line's probability is its chance of being
  elected (top N). The race is projected or called when its *weakest* projected winner clears the
  threshold. Mathematical certainty compares the last winner with the first loser.
- **Yes/No questions** compare YES·(1−t) with NO·t, which handles supermajorities. A remaining
  ballot moves that gap by at most max(t, 1−t).

Automatic recounts (`config/recount.yaml`) cover the new race types too.

- A vote-for-N race is close when its last winner and first loser are close; its top two are both
  elected anyway. That field is bunched (a water board elects 9 of about 18 candidates, each with
  4–6 % of the marks), so its thresholds are much tighter (0.05 points of all marks for school
  boards, 0.003 for water boards). About 5 % of races are recounted, as for the other race types.
- A Yes/No question is close when few votes would have to switch to cross its threshold.
- A vote-for-N recount only corrects misread tallies and found ballots, because a ruling on a
  ballot would move several marks at once.

## Strict date order

Elections are certified in date order, as with the regular elections. An election can only be
finished (and its night only started) when every earlier election is finished.

Once local elections are in use, the planned local elections before it must also have been
created and held. With the world clock this happens by itself: it only moves to the next election
day once today's election is finished.

"Finish earlier" holds every unfinished election before a given one, oldest first: it creates,
simulates and runs an instant election night for each, so every race call is stored.

- UI: a banner on the night page offers a "Finish earlier elections" button.
- API: `POST /api/elections/{id}/finish-earlier`; `GET /api/elections/{id}/earlier` tells what is
  missing.
- CLI: `python -m app finish-earlier --election 2030`.

Only the most recent reported election can be reset ([CLI.md](CLI.md), `reset`). A reset undoes
local certifications too: board seats, special-election terms and recalled mayors are restored.

## The demo

`python -m app demo` holds every local election from the founding election up to the 2028
general election in date order, each through an instant election night:

1. local elections between 2024 and 2026;
2. the 2026 midterm;
3. local elections between 2026 and 2028;
4. the 2028 general election (simulated, ready at polls closing).

The world clock then waits on 8 November 2028; later local elections are created as the clock
reaches them. The roughly 64 local elections it holds add several minutes to the build;
`--no-local-elections` leaves them out.

## Interfaces

- **UI**
  - *Today* (`#/today`, [CLOCK.md](CLOCK.md)): today's election day with *Watch the night* /
    *Count instantly*, the next election day, skip ahead, news and the agenda.
  - *Local results* (`#/local`): every race of a local election as a searchable list with filter
    chips (school boards, measures, water boards, specials and recalls), a province and a
    municipality filter. Rows show the leader or winners ("+N candidates", "vote for N"), Yes % against the
    threshold with Passed / Failed, the recalled mayor, and LIVE / Projected / Called / Final.
  - *Local calendar* (`#/calendar`): the local election dates twelve months at a time, with the
    provinces voting, what is on each ballot and a link to the results or the night.
  - *Election night* of a local election: races called, measures passing, reporting, the live
    race list and the call feed. A scheduled election shows "Prepare the night" (simulates it).
    When earlier elections are unfinished, a banner offers "Finish earlier elections".
  - *Race pages* for measures and recalls (the question, a Yes/No bar with the threshold
    marker), vote-for-N boards (every elected candidate with a seat number) and special elections
    (vacancy, term).
  - The election picker (top left) filters All / Regular / Local and searches by province or
    date. Pages that only exist for the November elections (President, House, Forecast …) say so
    for a local election.
- **API**
  - `GET /api/local/calendar` (one entry per date)
  - `GET|POST /api/local/elections` (create by `date`)
  - `GET /api/elections/{id}/races?q=&type=&municipality=`
  - `GET /api/elections/{id}/earlier`
  - `POST /api/elections/{id}/finish-earlier`
  - See [API.md](API.md).
- **CLI**
  - `python -m app local calendar | list | create -d DATE | schedule`
  - `python -m app clock next` — the usual way: go to the next election day ([CLOCK.md](CLOCK.md))
  - `python -m app finish-earlier`
  - See [CLI.md](CLI.md).

## Database

- `election.election_type = "local"` and `election.provinces` (the provinces voting that day,
  `"OV,ZE,NB"`; migration `9c1d2e3f4a5b`). `election.province_id` is no longer used.
- `race.threshold` (Yes/No) and `race.details_json`:
  - contest data: measure title, summary and topic; board seats up and terms; recall target;
    vacancy reason; term end;
  - after certification: the winners of a vote-for-N race, whether a question passed, the seat
    assignment.
- Office rows per school board and water board seat (`SB-GM0363-1`, `WB-WS33-1`) and per council
  (`COUNCIL-GM0363`, holding the winners of council-seat special elections).
- `water_board` and `geo_unit.water_board_id`.

Migration `572107020ab6` adds the new columns. A database from an older version is upgraded
automatically when the app or the CLI starts, but it holds no local elections. A geography store
built by an older version has no water boards: `demo` and `init --with-geography` rebuild it
(`app.geography.store.is_current`). To get the full local history, run `python -m app demo
--force`.
