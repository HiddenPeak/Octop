"""Rewrite audited installation paths in an unpublished, offline staging copy.

Message bodies, checkpoint payloads and SDK-internal ``.octop`` names are opaque.
Callers own the source/destination fences and discard staging after any failure.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from inhere_box.infra.connectors.crypto import decrypt_credentials, encrypt_credentials
from inhere_box.infra.crypto.fernet import PURPOSE_CONNECTOR
from inhere_box.infra.db.pool import SqlitePool
from inhere_box.infra.db.repos.secrets import SecretRepo
from inhere_box.infra.utils.env_file import format_env_file, parse_env_text

_ENV_PATHS = frozenset(
    {
        "INHERE_BOX_HOME",
        "OCTOP_HOME",
        "INHERE_BOX_DATABASE_SQLITE_PATH",
        "OCTOP_DATABASE_SQLITE_PATH",
        "BROWSER_USE_PROFILES_DIR",
        "BROWSER_USE_SCREENSHOTS_DIR",
        "PLAYWRIGHT_BROWSERS_PATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "NODE_EXTRA_CA_CERTS",
    }
)
_ENV_LISTS = frozenset({"PATH", "PYTHONPATH"})
_ACTIVATORS = frozenset(
    {"activate", "activate.csh", "activate.fish", "activate.bat", "Activate.ps1"}
)


class BrandingPathsError(ValueError):
    """A staging copy cannot be relocated without guessing or losing data."""


def _regular(path: Path) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise BrandingPathsError(f"Expected a regular staging file: {path.name}")


def _sqlite_regular(path: Path) -> None:
    """SQLite may open sidecars even before our first SQL statement executes."""
    _regular(path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            _regular(sidecar)


class _Rewrite:
    def __init__(self, source: Path, destination: Path) -> None:
        self.source_path = source
        self.destination_path = destination
        self.source = str(source).replace("\\", "/").rstrip("/")
        self.destination = str(destination).replace("\\", "/").rstrip("/")
        self.counts: Counter[str] = Counter()
        self.modified_databases: set[Path] = set()

    def path(self, value: Any, *, database: bool = False) -> Any:
        if not isinstance(value, str):
            return value
        if database and value in {"octop.db", "./octop.db"}:
            return "inhere-box.db"
        prefix = "file://" if value.startswith("file://") else ""
        text = value[len(prefix) :].replace("\\", "/")
        source, compared = self.source, text
        if os.name == "nt":
            source, compared = source.casefold(), compared.casefold()
        if compared != source and not compared.startswith(source + "/"):
            return value
        suffix = text[len(self.source) :]
        if ".." in suffix.split("/"):
            raise BrandingPathsError("Parent traversal in a persisted installation path")
        if database and suffix in {"/octop.db", "/inhere-box.db"}:
            suffix = "/inhere-box.db"
        if prefix:
            return prefix + self.destination + suffix
        return str(self.destination_path / Path(suffix.lstrip("/")))

    def env(self, values: dict[str, Any]) -> None:
        for key, value in values.items():
            if not isinstance(value, str):
                continue
            if key in _ENV_PATHS:
                values[key] = self.path(value, database=key.endswith("DATABASE_SQLITE_PATH"))
            elif key in _ENV_LISTS:
                values[key] = os.pathsep.join(self.path(part) for part in value.split(os.pathsep))

    def launch(self, spec: dict[str, Any]) -> None:
        if "command" in spec:
            spec["command"] = self.path(spec["command"])
        args = spec.get("args")
        if isinstance(args, list):
            # Launch argv are configuration; model/tool arguments remain opaque.
            spec["args"] = [self.path(item) for item in args]
        env = spec.get("env")
        if isinstance(env, dict):
            self.env(env)

    def backend(self, spec: Any) -> None:
        if not isinstance(spec, dict):
            return
        kind = spec.get("type")
        if kind in {"filesystem", "local_shell"}:
            if "root_dir" in spec:
                spec["root_dir"] = self.path(spec["root_dir"])
        elif kind == "composite":
            self.backend(spec.get("default"))
            routes = spec.get("routes")
            if isinstance(routes, dict):
                for route in routes.values():
                    self.backend(route)

    def agent(self, config: dict[str, Any]) -> None:
        if "workspace_dir" in config:
            config["workspace_dir"] = self.path(config["workspace_dir"])
        self.backend(config.get("backend"))
        memory = config.get("memory")
        backend = memory.get("backend") if isinstance(memory, dict) else None
        if (
            isinstance(backend, dict)
            and backend.get("type", "sqlite") == "sqlite"
            and "db_path" in backend
        ):
            backend["db_path"] = self.path(backend["db_path"])
        skills = config.get("skills")
        if isinstance(skills, list):
            config["skills"] = [self.path(item) for item in skills]
        elif isinstance(skills, str):
            config["skills"] = self.path(skills)
        acp = config.get("acp")
        if isinstance(acp, dict):
            self.runners(acp)

    def runners(self, document: dict[str, Any]) -> None:
        runners = document.get("runners", document)
        if isinstance(runners, dict):
            for runner in runners.values():
                if isinstance(runner, dict):
                    self.launch(runner)

    def turn(self, config: dict[str, Any]) -> None:
        if "directory" in config:
            config["directory"] = self.path(config["directory"])
        defaults = config.get("project_defaults")
        if isinstance(defaults, dict) and "directory" in defaults:
            defaults["directory"] = self.path(defaults["directory"])

    def config(self, document: dict[str, Any]) -> None:
        database = document.get("database")
        if (
            isinstance(database, dict)
            and database.get("driver", "sqlite") == "sqlite"
            and "sqlite_path" in database
        ):
            database["sqlite_path"] = self.path(database["sqlite_path"], database=True)
        tls = document.get("tls")
        if isinstance(tls, dict):
            for key in ("cert_file", "key_file"):
                if key in tls:
                    tls[key] = self.path(tls[key])


def _json(raw: str, location: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise BrandingPathsError(f"Invalid JSON in {location}") from exc


def _object(raw: str, location: str) -> dict[str, Any]:
    value = _json(raw, location)
    if not isinstance(value, dict):
        raise BrandingPathsError(f"Expected a JSON object in {location}")
    return value


def _rewrite_json_rows(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    change: Callable[[dict[str, Any]], None],
    rewrite: _Rewrite,
) -> None:
    for row in conn.execute(f'SELECT rowid, "{column}" FROM "{table}"'):
        raw = row[1]
        if raw is None or raw == "":
            continue
        document = _object(raw, f"{table}.{column}")
        before = json.dumps(document, sort_keys=True)
        change(document)
        if json.dumps(document, sort_keys=True) != before:
            conn.execute(
                f'UPDATE "{table}" SET "{column}"=? WHERE rowid=?',
                (json.dumps(document, ensure_ascii=False), row[0]),
            )
            rewrite.counts[table] += 1


def _control_database(path: Path, rewrite: _Rewrite) -> None:
    _sqlite_regular(path)
    pool = SqlitePool(path)
    # SqlitePool itself enables WAL, even when no selected JSON value changes.
    rewrite.modified_databases.add(path)
    try:
        secrets = SecretRepo(pool)
        with pool.transaction() as conn:
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            for table, column, change in (
                ("agents", "config_json", rewrite.agent),
                ("project_task_turns", "configuration_json", rewrite.turn),
            ):
                if table in tables:
                    _rewrite_json_rows(conn, table, column, change, rewrite)
            if "projects" in tables:
                for row in conn.execute("SELECT rowid,directory FROM projects"):
                    updated = rewrite.path(row[1])
                    if updated != row[1]:
                        conn.execute(
                            "UPDATE projects SET directory=? WHERE rowid=?", (updated, row[0])
                        )
                        rewrite.counts["projects"] += 1
            if "storage_backends" in tables:
                for row in conn.execute(
                    "SELECT rowid,kind,bucket,config_json FROM storage_backends"
                ):
                    if row[1] not in {"filesystem", "shell"}:
                        continue
                    config = _object(row[3] or "{}", "storage_backends.config_json")
                    before = json.dumps(config, sort_keys=True)
                    for key in ("root_dir", "path"):
                        if key in config:
                            config[key] = rewrite.path(config[key])
                    bucket = rewrite.path(row[2])
                    if bucket != row[2] or json.dumps(config, sort_keys=True) != before:
                        conn.execute(
                            "UPDATE storage_backends SET bucket=?,config_json=? WHERE rowid=?",
                            (bucket, json.dumps(config, ensure_ascii=False), row[0]),
                        )
                        rewrite.counts["storage_backends"] += 1
            if "settings" in tables:
                for row in conn.execute("SELECT key,value FROM settings"):
                    if not row[0].startswith(("acp_runners:user:", "acp_setup:user:")):
                        continue
                    document = _object(row[1], "settings ACP configuration")
                    before = json.dumps(document, sort_keys=True)
                    if row[0].startswith("acp_runners:user:"):
                        rewrite.runners(document)
                    elif isinstance(document.get("managed_launch"), list):
                        document["managed_launch"] = [
                            rewrite.path(item) for item in document["managed_launch"]
                        ]
                    if json.dumps(document, sort_keys=True) != before:
                        conn.execute(
                            "UPDATE settings SET value=? WHERE key=?",
                            (json.dumps(document, ensure_ascii=False), row[0]),
                        )
                        rewrite.counts["settings"] += 1
            if "threads" in tables:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
                if "artifacts" in columns:
                    for row in conn.execute("SELECT rowid,artifacts FROM threads"):
                        document = _json(row[1] or "[]", "threads.artifacts")
                        if not isinstance(document, list):
                            raise BrandingPathsError("Expected a JSON list in threads.artifacts")
                        before = json.dumps(document, sort_keys=True)
                        for index, item in enumerate(document):
                            if isinstance(item, str):
                                document[index] = rewrite.path(item)
                            elif isinstance(item, dict) and "path" in item:
                                item["path"] = rewrite.path(item["path"])
                        if json.dumps(document, sort_keys=True) != before:
                            conn.execute(
                                "UPDATE threads SET artifacts=? WHERE rowid=?",
                                (json.dumps(document, ensure_ascii=False), row[0]),
                            )
                            rewrite.counts["thread_artifacts"] += 1
            if "connectors" in tables:
                for row in conn.execute(
                    "SELECT rowid,credential_blob FROM connectors WHERE kind='custom-mcp' AND credential_blob IS NOT NULL"
                ):
                    try:
                        if "secrets" not in tables or secrets.get(PURPOSE_CONNECTOR) is None:
                            raise ValueError("missing connector encryption key")
                        payload = decrypt_credentials(secrets, bytes(row[1]))
                        before = json.dumps(payload, sort_keys=True)
                        servers = payload.get("servers", payload)
                        if not isinstance(servers, dict):
                            raise ValueError("invalid custom MCP server map")
                        for server in servers.values():
                            if isinstance(server, dict) and server.get("transport") == "stdio":
                                rewrite.launch(server)
                        if json.dumps(payload, sort_keys=True) != before:
                            conn.execute(
                                "UPDATE connectors SET credential_blob=? WHERE rowid=?",
                                (encrypt_credentials(secrets, payload), row[0]),
                            )
                            rewrite.counts["custom_mcp"] += 1
                    except Exception as exc:
                        raise BrandingPathsError(
                            "Cannot safely inspect custom MCP encrypted launch paths"
                        ) from exc
    finally:
        pool.close()


def _history(path: Path, rewrite: _Rewrite) -> None:
    _sqlite_regular(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as conn, conn:
        rows = conn.execute("SELECT rowid,identity FROM archive_meta").fetchall()
        if len(rows) != 1:
            raise BrandingPathsError("History archive must have one control-plane identity")
        for row in rows:
            target = str((rewrite.destination_path / "inhere-box.db").resolve())
            allowed = {
                (rewrite.source_path / "octop.db").resolve(),
                (rewrite.source_path / "inhere-box.db").resolve(),
                Path(target),
            }
            if not isinstance(row[1], str) or Path(row[1]).resolve() not in allowed:
                raise BrandingPathsError("History archive has a different control-plane identity")
            if row[1] != target:
                conn.execute("UPDATE archive_meta SET identity=? WHERE rowid=?", (target, row[0]))
                rewrite.counts["history_identity"] += 1
                rewrite.modified_databases.add(path)


def _text_files(root: Path, rewrite: _Rewrite) -> None:
    config = root / "config.json"
    if config.exists() or config.is_symlink():
        _regular(config)
        original = config.read_text("utf-8")
        document = _object(original, "config.json")
        before = json.dumps(document, sort_keys=True)
        rewrite.config(document)
        if json.dumps(document, sort_keys=True) != before:
            config.write_text(
                json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            rewrite.counts["config"] += 1
    for name in ("env", ".env"):
        path = root / name
        if path.exists() or path.is_symlink():
            _regular(path)
            values = parse_env_text(path.read_text("utf-8"))
            original_values = dict(values)
            rewrite.env(values)
            if values != original_values:
                path.write_text(format_env_file(values), encoding="utf-8")
                rewrite.counts["env"] += 1


def _check_database_driver(root: Path) -> None:
    config = root / "config.json"
    if config.exists() or config.is_symlink():
        _regular(config)
        document = _object(config.read_text("utf-8"), "config.json")
        database = document.get("database")
        if isinstance(database, dict) and str(database.get("driver", "sqlite")).lower() != "sqlite":
            raise BrandingPathsError(
                "External database paths require a separately scoped migration"
            )
    environments = [dict(os.environ)]
    for name in ("env", ".env"):
        path = root / name
        if path.exists() or path.is_symlink():
            _regular(path)
            environments.append(parse_env_text(path.read_text("utf-8")))
    for values in environments:
        driver = values.get("INHERE_BOX_DATABASE_DRIVER", values.get("OCTOP_DATABASE_DRIVER", ""))
        url = values.get("INHERE_BOX_DATABASE_URL", values.get("OCTOP_DATABASE_URL", ""))
        if (driver.strip().lower() not in {"", "sqlite"}) or url.strip().lower().startswith(
            ("postgres:", "postgresql:")
        ):
            raise BrandingPathsError(
                "External database paths require a separately scoped migration"
            )


def _venv(root: Path, rewrite: _Rewrite) -> None:
    venv = root / "venv"
    if not venv.exists():
        return
    if venv.is_symlink():
        raise BrandingPathsError(
            "Staging venv is a symlink; rebuild the environment after migration"
        )
    # Only generated launcher lines and activation assignments are textual paths.
    spellings = {str(rewrite.source_path), rewrite.source}
    pattern = re.compile(
        "(?:" + "|".join(re.escape(value) for value in sorted(spellings)) + r")(?=[/\\\"'\s]|$)",
        re.IGNORECASE if os.name == "nt" else 0,
    )
    for directory in (venv / "bin", venv / "Scripts"):
        if directory.is_symlink():
            raise BrandingPathsError("Staging venv launcher directory is a symlink")
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            raw = path.read_bytes()
            if path.suffix.lower() == ".exe":
                spellings = {rewrite.source, rewrite.source.replace("/", "\\")}
                if any(
                    s.encode(encoding) in raw
                    for s in spellings
                    for encoding in ("utf-8", "utf-16le")
                ):
                    raise BrandingPathsError(
                        "Windows venv executable contains the old root; rebuild the venv"
                    )
                continue
            if not raw.startswith(b"#!") and path.name not in _ACTIVATORS:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise BrandingPathsError(
                    "Cannot decode a generated venv launcher; rebuild the venv"
                ) from exc
            lines = text.splitlines(keepends=True)
            for index, line in enumerate(lines):
                if (
                    (index == 0 and line.startswith("#!"))
                    or (index < 4 and line.startswith("'''exec'"))
                    or (path.name in _ACTIVATORS and "VIRTUAL_ENV" in line)
                ):
                    lines[index] = pattern.sub(lambda _: str(rewrite.destination_path), line)
            updated = "".join(lines).encode()
            if updated != raw:
                path.write_bytes(updated)
                rewrite.counts["venv"] += 1
    cfg = venv / "pyvenv.cfg"
    if cfg.exists() or cfg.is_symlink():
        _regular(cfg)
        original = cfg.read_text("utf-8")
        lines = original.splitlines(keepends=True)
        for index, line in enumerate(lines):
            key, sep, value = line.partition("=")
            if sep and key.strip() in {"home", "executable", "command"}:
                lines[index] = (
                    key + sep + pattern.sub(lambda _: str(rewrite.destination_path), value)
                )
        updated_text = "".join(lines)
        if updated_text != original:
            cfg.write_text(updated_text, encoding="utf-8")
            rewrite.counts["venv"] += 1


def _checkpoint_databases_and_links(root: Path, rewrite: _Rewrite) -> None:
    for directory, dirs, names in os.walk(root, followlinks=False):
        # Backups are immutable originals, not live installation databases.
        if Path(directory) == root:
            dirs[:] = [
                name for name in dirs if name not in {"backups", "branding-migration-backup"}
            ]
        for name in [*dirs, *names]:
            path = Path(directory) / name
            if path.is_symlink():
                original = os.readlink(path)
                updated = rewrite.path(original)
                if updated != original:
                    path.unlink()
                    path.symlink_to(updated, target_is_directory=name in dirs)
                    rewrite.counts["symlinks"] += 1
        for name in names:
            path = Path(directory) / name
            if path.is_symlink() or path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
                continue
            _sqlite_regular(path)
            if path not in rewrite.modified_databases and not Path(str(path) + "-wal").exists():
                continue
            with path.open("rb") as stream:
                if stream.read(16) != b"SQLite format 3\0":
                    continue
            with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as conn:
                if conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise BrandingPathsError("A staging SQLite database is still busy")
                if conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                    raise BrandingPathsError("Cannot make a staging SQLite database self-contained")
                if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise BrandingPathsError("A staging SQLite database failed integrity_check")
            rewrite.counts["sqlite_checkpoints"] += 1


def rewrite_installation_paths(
    staging_root: Path, source_root: Path, destination_root: Path
) -> dict[str, int]:
    """Relocate audited live paths in a disposable staging copy; never touch source.

    Supports either old or new control-plane filename; the caller subsequently
    migrates ``octop.db`` in staging and publishes the entire destination root.
    """
    root = staging_root.absolute()
    source, destination = source_root.absolute(), destination_root.absolute()
    if (
        root.is_symlink()
        or not root.is_dir()
        or root.resolve() in {source.resolve(), destination.resolve()}
    ):
        raise BrandingPathsError("Path rewriting requires a separate regular staging directory")
    rewrite = _Rewrite(source, destination)
    databases = [
        root / name
        for name in ("octop.db", "inhere-box.db")
        if (root / name).exists() or (root / name).is_symlink()
    ]
    if len(databases) > 1:
        raise BrandingPathsError("Both control-plane database filenames exist in staging")
    _check_database_driver(root)
    _text_files(root, rewrite)
    if databases:
        _control_database(databases[0], rewrite)
    history = root / "history_v2.sqlite"
    if history.exists() or history.is_symlink():
        _history(history, rewrite)
    _venv(root, rewrite)
    _checkpoint_databases_and_links(root, rewrite)
    return dict(rewrite.counts)
