"""Electoral mechanics of the (FICTIONAL) federal system — pure engines, no database access.

* :mod:`app.elections.types` — engine contracts (``RaceVotes``, ``TabulatedRace`` …)
* :mod:`app.elections.tabulation` — official counts, level aggregation, reconciliation
* :mod:`app.elections.electoral_college` — EV allocation, tipping point, EV/PV divergence
* :mod:`app.elections.contingent` — contingent election when nobody reaches 88 EV
* :mod:`app.elections.calendar` — election days, cycles, Senate rotation, terms, special elections
* :mod:`app.elections.recount` — automatic-recount triggers and auditable recounts
* :mod:`app.elections.seats` — chamber composition/control and proportional seat allocation

See docs/ELECTORAL_SYSTEM.md.
"""
