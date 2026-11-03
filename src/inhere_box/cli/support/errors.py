"""CLI error helpers."""

from __future__ import annotations

from typing import NoReturn

import click

from inhere_box.infra.errors import InhereBoxError


def fail_octop(exc: InhereBoxError) -> NoReturn:
    click.echo(f"error: {exc.message}", err=True)
    raise SystemExit(1) from exc
