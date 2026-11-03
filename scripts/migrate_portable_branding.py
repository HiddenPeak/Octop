#!/usr/bin/env python3
"""One-time VERSION.txt migration for extracted desktop runtimes or portable ZIPs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any

OLD_KEY = "octop_version"
NEW_KEY = "inhere_box_version"


def migrated_marker(data: bytes) -> bytes | None:
    lines = data.decode("utf-8").splitlines(keepends=True)
    old = [line for line in lines if line.partition("=")[0].strip() == OLD_KEY]
    if not old:
        return None
    current = [line for line in lines if line.partition("=")[0].strip() == NEW_KEY]
    version = (current or old)[0].partition("=")[2].strip()
    if not version:
        raise ValueError("Empty portable version marker")
    replacement = []
    inserted = bool(current)
    for line in lines:
        if line.partition("=")[0].strip() != OLD_KEY:
            replacement.append(line)
        elif not inserted:
            ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
            replacement.append(f"{NEW_KEY}={version}{ending}")
            inserted = True
    return "".join(replacement).encode("utf-8")


def migrate(path: Path, *, apply: bool = False) -> dict[str, Any]:
    source = path / "VERSION.txt" if path.is_dir() else path
    source = source.resolve(strict=True)
    archive = zipfile.is_zipfile(source)
    changes: dict[str, bytes] = {}
    if archive:
        with zipfile.ZipFile(source) as bundle:
            for info in bundle.infolist():
                if Path(info.filename).name == "VERSION.txt":
                    updated = migrated_marker(bundle.read(info))
                    if updated is not None:
                        changes[info.filename] = updated
    else:
        if source.name != "VERSION.txt":
            raise ValueError("Expected a runtime directory, VERSION.txt or portable ZIP")
        updated = migrated_marker(source.read_bytes())
        if updated is not None:
            changes[source.name] = updated
    result: dict[str, Any] = {
        "path": str(source),
        "status": "needs_migration" if changes else "current",
        "entries": list(changes),
    }
    if not changes or not apply:
        return result
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    backup = source.with_name(source.name + ".before-inhere-box-" + digest[:12])
    if backup.exists():
        with backup.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError("Existing migration backup does not match the source")
    else:
        shutil.copy2(source, backup)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".inhere-box-migrate-", dir=source.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        if archive:
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(temporary, "w") as output:
                output.comment = original.comment
                for info in original.infolist():
                    if info.filename in changes:
                        output.writestr(info, changes[info.filename])
                    else:
                        with (
                            original.open(info) as src,
                            output.open(
                                info, "w", force_zip64=info.file_size > zipfile.ZIP64_LIMIT
                            ) as dst,
                        ):
                            shutil.copyfileobj(src, dst)
            with zipfile.ZipFile(temporary) as check:
                if check.testzip() is not None:
                    raise ValueError("Migrated ZIP failed its CRC check")
        else:
            temporary.write_bytes(changes[source.name])
        with temporary.open("rb+") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(stat.S_IMODE(source.stat().st_mode))
        os.replace(temporary, source)
    finally:
        temporary.unlink(missing_ok=True)
    return {**result, "status": "migrated", "backup": str(backup), "before_sha256": digest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="Back up and migrate; default is preview"
    )
    args = parser.parse_args()
    for path in args.paths:
        print(json.dumps(migrate(path, apply=args.apply), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
