"""Helpers for gated live-PostgreSQL tests.

Prefer a dedicated database (tests may ``DROP SCHEMA public CASCADE``)::

    export INHERE_BOX_TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:15432/inhere_box_test'
"""

from __future__ import annotations

import os

import pytest

requires_postgresql = pytest.mark.skipif(
    not os.environ.get("INHERE_BOX_TEST_DATABASE_URL"),
    reason="set INHERE_BOX_TEST_DATABASE_URL to run PostgreSQL tests",
)
