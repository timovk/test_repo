"""HTTP API (FastAPI): JSON under ``/api`` plus static hosting of the single-page UI.

``app.api.main.create_app()`` builds the application; routers live in :mod:`app.api.routers`
(one per area) and get every payload from :mod:`app.services.read` — the API performs no election
mathematics.  The contract is documented in docs/API.md.
"""
