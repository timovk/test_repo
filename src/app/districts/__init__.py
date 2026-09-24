"""FICTIONAL electoral geography over the REAL Netherlands (see docs/DISTRICTING.md).

Modules (import them directly; this package keeps no imports so worker processes stay light):

* :mod:`~app.districts.apportionment` — House seats per province (Huntington-Hill & co.), EV totals
* :mod:`~app.districts.config` — ``config/districts.yaml`` schema (:class:`DistrictConfig`)
* :mod:`~app.districts.generator` — :func:`generate_plan` (150 single-member districts)
* :mod:`~app.districts.partition` — per-province bisection + local-search engine (pure NumPy/SciPy)
* :mod:`~app.districts.plan` — :class:`GeneratedPlan`
* :mod:`~app.districts.naming` — serpentine numbering and descriptive names
* :mod:`~app.districts.overrides` — manual overrides (``config/districts/overrides.yaml``)
* :mod:`~app.districts.validation` — :func:`validate_plan`
* :mod:`~app.districts.stats` — statistics, compactness, geometries, adjacency, fragments
* :mod:`~app.districts.senate` — Senate classes (``config/senate.yaml``)
* :mod:`~app.districts.service` — persistence (the only module importing SQLAlchemy)
"""
