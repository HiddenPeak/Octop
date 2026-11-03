"""inhere-box version command."""

from __future__ import annotations

import click


@click.command("version")
def version() -> None:
    """Show the installed inhere-box version."""
    try:
        from importlib.metadata import version as _v

        v = _v("inhere-box")
    except Exception:
        v = "unknown"
    click.echo(f"inhere-box v{v}")
