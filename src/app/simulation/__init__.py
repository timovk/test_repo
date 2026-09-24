"""Probabilistic political model and election simulation (pure engines).

* :mod:`app.simulation.config` — model hyperparameters (``config/model.yaml``)
* :mod:`app.simulation.regions` — named political regions over REAL municipalities
* :mod:`app.simulation.structural` — persistent unit × party utilities, calibration, turnout,
  elasticity, affinity (``StructuralModel.build``)
* :mod:`app.simulation.voting` — one election draw (``simulate_election``) and the no-shock
  expectation (``expected_race_shares``)
* :mod:`app.simulation.candidates` — FICTIONAL candidate generation
* :mod:`app.simulation.races` — RaceSpec builders for presidential / House / Senate / governor races
* :mod:`app.simulation.baselines` — historical-results import and model-based analysis

See docs/SIMULATION.md.
"""
