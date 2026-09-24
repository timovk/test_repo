"""REAL geography of the Netherlands: CBS/PDOK download → processed GeoParquet store → frames.

Submodules (imported explicitly; nothing heavy is imported here):

* :mod:`~app.geography.download` — fetch the raw sources into ``data/raw`` with provenance;
* :mod:`~app.geography.build` — build ``data/processed/geo_<year>/`` (units, municipalities,
  provinces, adjacency, web GeoJSON, manifest);
* :mod:`~app.geography.store` — cached readers (``load_frame``, ``load_units_gdf`` …);
* :mod:`~app.geography.loader_db` — persist a vintage into the database;
* :mod:`~app.geography.adjacency`, :mod:`~app.geography.simplify`, :mod:`~app.geography.topology`,
  :mod:`~app.geography.demographics`, :mod:`~app.geography.cbs`, :mod:`~app.geography.lineage`;
* :mod:`~app.geography.frame` / :mod:`~app.geography.synthetic` — the engine contract and toy country.
"""
