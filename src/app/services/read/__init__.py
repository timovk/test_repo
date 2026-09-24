"""Read models of the HTTP API: plain ``dict`` / ``list`` payloads built from the database.

The API layer (:mod:`app.api`) gets every piece of data from these modules and performs no
election mathematics itself.  Each function takes a SQLAlchemy ``Session`` (the caller owns it)
and returns JSON-ready Python structures (NumPy scalars are allowed: the API serialises with
``orjson``).  Every payload carries a ``data_category`` (or a ``provenance`` map) so the UI can
badge REAL / DERIVED / FICTIONAL / SIMULATED content.

**Hidden-until-reported rule.**  For elections whose status is ``scheduled``, ``simulated`` or
``live`` no stored final result is ever read: during an election night the only source of
results is the night service (live counted votes, :mod:`app.services.read.live`); once the
election is FINAL the stored results are used.  Every election-scoped payload states its
``results_source`` (``final`` | ``live`` | ``hidden``).

Modules (import explicitly):

* :mod:`~app.services.read._base` — election resolution (``latest`` / ``demo`` aliases),
  caching, ballot lines, party identities;
* :mod:`~app.services.read.live` — bridge to the election-night service;
* :mod:`~app.services.read.meta` — meta, health, settings, provenance, validation;
* :mod:`~app.services.read.geography` — provinces, municipalities, apportionment, GeoJSON;
* :mod:`~app.services.read.districts` — House plan, districts, Senate seats;
* :mod:`~app.services.read.elections` — election results pages (President, Electoral College,
  provinces, municipalities, House, Senate, governors, mayors, races, calls, timeline);
* :mod:`~app.services.read.history` / :mod:`~app.services.read.analytics` — cross-election
  records and per-election metrics (via :mod:`app.analytics`);
* :mod:`~app.services.read.people` — parties and candidates;
* :mod:`~app.services.read.polls`, :mod:`~app.services.read.campaigns`,
  :mod:`~app.services.read.forecast` — SIMULATED inputs and model estimates;
* :mod:`~app.services.read.scenarios` — scenario editor (read + save);
* :mod:`~app.services.read.exports` — stable-schema CSV / JSON exports (:mod:`app.export`);
* :mod:`~app.services.read.actions` — the write actions exposed by the API (create / simulate /
  finalize elections, UI settings, manual polls), each one a committed unit of work.
"""
