# Your own people

Add friends and family to the simulation. Each person lives somewhere, and every now and then
they run for an office there: the school board of their town, a council seat, the mayoralty, the
House district, the province's Senate seat or its governorship. They win or lose like anyone
else, and a winner runs again as the incumbent.

`config/people.yaml` · `app.simulation.people` · check with `python -m app people`

## Step by step

### 1. Open `config/people.yaml`

It starts empty (`people: []`) with commented examples. Replace `people: []` with your list:

```yaml
people:
  - name: Sanne de Vries
    home: Tilburg
    born: 1990
    gender: F
    party: PA
    chance: often
    quality: 0.5

  - name: Tom Jansen
    home: Eindhoven
    born: 1984
    chance: sometimes
    offices: [school_board, water_board, mayor]

  - name: Riet van den Berg
    home: GM0855          # Tilburg, by its CBS code
    born: 1952-03-08
    party: CVU
    chance: rarely
    offices: [council, mayor]
    bio: Retired teacher and the family's best baker.
```

Indentation matters: two spaces before each `-`, four before the fields.

### 2. Fill in the fields

| Field | Required | What it does |
|---|---|---|
| `name` | yes | First and last name. The first word is the first name, the rest the last name ("Riet van den Berg"). Or give `first_name` and `last_name`. |
| `home` | yes | Their municipality: a name ("Tilburg", "'s-Hertogenbosch", "Bergen (NH.)"; case and punctuation don't matter) or a CBS code (`GM0855`). This decides where they can run. |
| `born` | no | Birth year (`1990`) or date (`1990-05-17`). Offices have minimum ages (below). Without it, age is never a reason to skip them. |
| `gender` | no | `F`, `M` or `X`. |
| `party` | no | A party code from `config/parties/fictional.yaml` (`PA`, `VLP`, `CVU`, … or your own party). With a party they run for that party; without one they run in the nonpartisan board races or as an independent. |
| `chance` | no | How likely they run at an election that has a race for them: `never`, `rarely` (15 %), `sometimes` (35 %, the default), `often` (65 %), `always`, or a number from 0 to 1. |
| `quality` | no | How strong a candidate they are, from -3 to 3. 0 is average, 1 is a strong candidate, -1 a weak one. |
| `offices` | no | Only these kinds of office: `school_board`, `water_board`, `council`, `mayor`, `house`, `senate`, `governor`. Leave it out to allow all. |
| `ideology` | no | Their own positions: `{economic: 0.2, social: -0.5, europe: 0.6}`, each from -1 to 1. Default: their party's, with a small personal twist. In nonpartisan board races this decides which voters like them. |
| `bio` | no | One line shown on their candidate page. |
| `key` | no | A stable id (default `person-<name>`). Only needed when two people share a name. |

### 3. Check the file

```bash
python -m app people
```

This lists everyone with their municipality and province, party, chance and allowed offices,
and warns about mistakes: an unknown municipality (with suggestions), an unknown party code, a
missing last name, a bad `chance`. After a few elections it also lists every race they ran in
(won or lost) and the offices they hold.

### 4. Roll the clock

Your people are picked when an election is **created**, and the world clock creates each
election when it reaches its date ([CLOCK.md](CLOCK.md)). So just carry on: on the **Today** page
press *Next election day ▶* and watch or count. The news feed tells you when one of your people
ran (*"Sanne de Vries (PA) won: Tilburg School Board."*); their candidate pages show their career.

Elections that already exist (for example the 2028 general election waiting in the demo) keep
their candidates. To have your people in the whole history, rebuild the demo:
`python -m app demo --force`.

## How they are picked

At every election each person first decides — with their `chance` — whether to run. A person who
runs picks **one** race on that ballot whose area contains their home:

| Kind | Where | Minimum age | Weight |
|---|---|---|---|
| `school_board` | their municipality's school board (local election days) | 18 | 3 |
| `council` | a special election for a vacant seat on their municipal council | 18 | 2 |
| `water_board` | the water authority most of their municipality lies in (elected once every four years) | 18 | 1 |
| `mayor` | their municipality: the midterm mayoral elections, special elections and recall replacements | 21 | 1.5 |
| `house` | a House district covering their municipality (big cities have several) | 25 | 1.5 |
| `senate` | their province's Senate seat when its class is up | 30 | 0.6 |
| `governor` | their province's governorship (general elections) | 30 | 0.5 |

The weight is how likely each kind is picked among the races available that day. So local
offices come up most. Tune both in the file:

```yaml
settings:
  min_age: {school_board: 18, water_board: 18, council: 18, mayor: 21, house: 25, senate: 30, governor: 30}
  weights: {school_board: 3, council: 2, water_board: 1, mayor: 1.5, house: 1.5, senate: 0.6, governor: 0.5}
```

Rules:

- **With a party**, they take that party's line. If the party wasn't going to contest the race, it
  now does. A party never runs someone against its own incumbent.
- **Without a party**, they run in the nonpartisan board races, or as the independent candidate in
  a partisan race.
- A person whose party code is unknown only runs in the board races.
- One race per election per person. Someone who already holds the office on the ballot runs as
  the incumbent instead.
- **Same person every time**: they keep their identity (`person-<name>`) across elections. A
  winner holds the office, shows up as the incumbent and may run again. Editing their party or
  quality in the file applies from the next election they are picked for.
- Everything is seeded: rebuilding the world puts the same people in the same races.

Presidential tickets are not drawn from your people.

## Notes

- `home` is a municipality, not a neighbourhood: a person living in Amsterdam can run in any of
  Amsterdam's House districts.
- Your people are part of the FICTIONAL elections. The results, quality and positions are
  simulated — nothing here says anything about the real people.
- The file is read when an election is created; a broken file stops that election from being
  created, with the error message (`python -m app people` shows it too).
