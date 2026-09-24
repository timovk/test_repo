"""Command-line interface (``python -m app`` / ``nlfed``).

:mod:`app.cli.main` assembles the Typer application from the command modules (``system``,
``geography``, ``districts``, ``elections``, ``night``, ``scenario``, ``db``); every command calls
the services layer and renders the result with rich.  See docs/CLI.md.
"""
