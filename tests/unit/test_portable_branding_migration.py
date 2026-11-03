"""A one-time marker migration must preserve runtime bytes and be repeatable."""

from __future__ import annotations

import runpy
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "migrate_portable_branding.py"


@pytest.mark.parametrize("archive", [False, True])
def test_marker_migration_is_backed_up_and_idempotent(tmp_path, archive):
    migrate = runpy.run_path(str(SCRIPT))["migrate"]
    marker = b"platform=test\r\noctop_version=1.0.2b4\r\n"
    payload = b"Runtime binary and user-independent package bytes"
    if archive:
        target = tmp_path / "portable.zip"
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.comment = b"existing bundle"
            bundle.writestr("portable/VERSION.txt", marker)
            bundle.writestr("portable/runtime/python", payload)
    else:
        target = tmp_path / "VERSION.txt"
        target.write_bytes(marker)
    original = target.read_bytes()
    assert migrate(target)["status"] == "needs_migration"
    assert target.read_bytes() == original
    assert len(list(tmp_path.iterdir())) == 1
    result = migrate(target, apply=True)
    assert result["status"] == "migrated"
    assert Path(result["backup"]).read_bytes() == original
    if archive:
        with zipfile.ZipFile(target) as bundle:
            migrated = bundle.read("portable/VERSION.txt")
            assert bundle.read("portable/runtime/python") == payload
            assert bundle.comment == b"existing bundle"
    else:
        migrated = target.read_bytes()
    assert migrated == marker.replace(b"octop_version=", b"inhere_box_version=")
    after = target.read_bytes()
    assert migrate(target, apply=True)["status"] == "current"
    assert target.read_bytes() == after
    assert len(list(tmp_path.glob("*.before-inhere-box-*"))) == 1


def test_current_marker_wins_and_empty_legacy_is_left_untouched(tmp_path):
    module = runpy.run_path(str(SCRIPT))
    assert (
        module["migrated_marker"](b"octop_version=1.0.0\ninhere_box_version=1.0.2\n")
        == b"inhere_box_version=1.0.2\n"
    )
    target = tmp_path / "VERSION.txt"
    target.write_bytes(b"octop_version=\n")
    with pytest.raises(ValueError, match="Empty"):
        module["migrate"](target, apply=True)
    assert target.read_bytes() == b"octop_version=\n"
    assert len(list(tmp_path.iterdir())) == 1
