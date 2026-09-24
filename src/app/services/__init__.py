"""Services: orchestration and persistence around the pure engines.

Services translate ORM rows ⇄ engine types and own the transactions (they flush, the caller
commits).  Modules (import them explicitly; this package imports nothing heavy):

* :mod:`app.services.bootstrap` — schema migrations, REAL geography, apportionment, House plan,
  Senate seats, offices and legislatures (``setup_system``; ``setup_synthetic_system`` for tests);
* :mod:`app.services.runtime` — cached engine inputs rebuilt from the database (frame, plan
  mapping, structural model, ``election_inputs``, final results, reporting timeline);
* :mod:`app.services.elections` — create → simulate → finalize an election, summaries;
* :mod:`app.services.results` — the standard results frame (reported elections only);
* :mod:`app.services.validation` — constitutional and integrity validation (spec §33);
* :mod:`app.services.sandbox` — a complete small world on the synthetic geography.

See docs/DATABASE.md for the schema, the lifecycle and the hidden-until-reported rule.
"""
