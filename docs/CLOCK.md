# The world clock

The simulated world has one **today**. Elections happen on their dates, and you roll from one
election day to the next, like real life:

1. On an election day you choose: **watch** its election night, or **count** it instantly.
2. When today's election is finished, **Next election day ▶** moves the clock to the next date
   with anything on the ballot and shows what happened in between.
3. Repeat — forever: after the built-in scenarios (2024, 2026, 2028) every November election is
   generated from the elections before it.

Everything political is FICTIONAL and SIMULATED; the geography is REAL.

`app.services.clock` · `app.services.continuation` · state in `app_meta.world_date`.

## Election days

Two kinds of election day:

- **Local election days** — between February and June and in September, every province holds
  four local days a year ([LOCAL_ELECTIONS.md](LOCAL_ELECTIONS.md)). All provinces voting on a
  date form **one local election with one combined election night**, e.g. *"Local Elections ·
  7 February 2029"* with Overijssel, Zeeland and Noord-Brabant.
- **Regular election days** — the Wednesday after the first Monday of November in even years:
  general elections (President, House, a Senate class, governors) and midterms (House, a Senate
  class, mayors and councils).

Future elections are *planned*: the agenda shows them, but they are created only when the clock
reaches their date. So changes to `config/people.yaml` (or a party you add) still apply to them.

## Watch or count

- **Watch** simulates the election's hidden result and opens its election night at polls closing
  (21:00). Calls come in as the votes are counted; at the end the election is certified.
- **Count instantly** simulates it and runs the whole night at once. Every race call is stored, so
  the results pages, race pages and call histories look the same as after a watched night.

Either way, office holders are installed (school board members, mayors, presidents …) and the
next elections build on them: incumbents run again, recalls target the sitting mayor, and the
national mood follows the results.

To replay a night, use **↺ Replay** (or `python -m app reset`): the clock stays where it is.

## Skip ahead

**Skip to a date** counts every election day before that date instantly and moves the clock
there. If that date is itself an election day, its election is created and waits for you. A skip
of a year takes about a minute or two on the REAL geography (≈ 16 local election days and a
regular election). The clock only moves forward.

## News

Between two dates the clock reports:

- vacancies and recall petitions (*"The Mayor of Maassluis, Wilma van Rijn (NVB), faces a recall
  … Recall election on 15 March 2028."*),
- the results of the elections held (who became President, which party controls each chamber,
  how many ballot measures passed),
- how **your own people** ([PEOPLE.md](PEOPLE.md)) did: *"Sanne de Vries (PA) won: Tilburg
  School Board."*

## Endless elections

When the clock reaches a November election day that no built-in scenario covers, it generates a
*continuation scenario* (`app.services.continuation`):

- the latest stored scenario of the same type (general or midterm) is moved to the new year;
- the parties carry over (including any you add to `config/parties/fictional.yaml`);
- the **national mood** (`calibration.national`) is the previous House popular vote pulled back
  towards each party's long-run `base_share` (55 % last result, 45 % base) plus a random drift;
- the year-specific story (party swings, named events, explicit candidates) is replaced by a few
  generic, randomly drawn events: the economy reflecting on the President's party, a party
  scandal, a turnout surge — each happens only in some draws;
- **office holders run again** as incumbents; open seats get new FICTIONAL candidates (and
  possibly your own people);
- **presidential tickets** (general elections): the sitting President runs again with the
  Vice-President unless they have served two terms; every other ticket is led by one of the
  party's prominent office holders (governors first, then senators, then House members) or by a
  new FICTIONAL politician; parties with at least 10 % nationally field a ticket.

The midterm penalty for the President's party and incumbency advantages come from the
scenario's `environment.incumbency`, as for the built-in elections. Everything is seeded by the
template's seed and the year, so rebuilding the world generates the same elections.

## Interfaces

- **UI** — the **Today** page (the home page): today's election with *Watch the night* /
  *Count instantly*, *Next election day ▶* with the arrival dialog (watch or count?), *Skip
  ahead*, the news feed, the agenda and your people. The top bar shows today's date everywhere.
- **API** ([API.md](API.md))
  - `GET /api/clock` — today, today's elections, the next election day, the running job
  - `GET /api/clock/agenda?start=&end=` · `GET /api/clock/news?since=&until=`
  - `POST /api/clock/next` — go to the next election day (409 while today is unfinished)
  - `POST /api/clock/watch` — simulate today's election for its night
  - `POST /api/clock/count` · `POST /api/clock/skip {date}` — background jobs (`GET /api/clock/job`)
- **CLI** ([CLI.md](CLI.md))
  ```bash
  python -m app clock                    # today, today's elections, the next election day
  python -m app clock next               # go to the next election day; asks: watch or count?
  python -m app clock next --count       # … and count it instantly
  python -m app clock watch --speed 25   # watch today's night in the terminal
  python -m app clock count              # count today's election
  python -m app clock skip --to 2031-01-01
  python -m app clock agenda --months 12
  python -m app clock news --days 365
  ```

## The demo

`python -m app demo` holds every election up to the 2028 general election and leaves the clock
on **8 November 2028**: the 2028 general election waits at polls closing. Watch or count it, then
press *Next election day ▶* — the first local election day of 2029 follows.
