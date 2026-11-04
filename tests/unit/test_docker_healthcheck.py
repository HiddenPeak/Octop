"""Run the shipped Docker health commands against isolated local HTTP servers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = (ROOT / "docker" / "Dockerfile", ROOT / "fnos" / "docker" / "Dockerfile")
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Docker images use a POSIX shell")


def _command(dockerfile: Path) -> list[str]:
    lines = dockerfile.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("HEALTHCHECK "))
    command = json.loads(lines[start + 1].strip().removeprefix("CMD "))
    assert command[:2] == ["sh", "-c"]
    return command


def _environment(ports: dict[str, str]) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"INHERE_BOX_PORT", "OCTOP_PORT", "ENV", "BASH_ENV"}
    }
    env.update(ports)
    # No proxy or external network is involved in these probes.
    env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
    return env


@contextmanager
def _http_server(status: int = 200) -> Iterator[tuple[int, list[str]]]:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            body = b'{"ok":true}'
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, requests
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=["standard", "fnos"])
@pytest.mark.parametrize("selection", ["current", "legacy", "both"])
def test_shipped_healthcheck_probes_the_selected_server(dockerfile: Path, selection: str) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl is required by the shipped Docker command")
    with (
        _http_server() as (current_port, current_calls),
        _http_server() as (
            legacy_port,
            legacy_calls,
        ),
    ):
        ports = {}
        if selection in {"current", "both"}:
            ports["INHERE_BOX_PORT"] = str(current_port)
        if selection in {"legacy", "both"}:
            ports["OCTOP_PORT"] = str(legacy_port)
        result = subprocess.run(
            _command(dockerfile),
            env=_environment(ports),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 0, result.stderr
    assert current_calls == (["/api/health"] if selection != "legacy" else [])
    assert legacy_calls == (["/api/health"] if selection == "legacy" else [])


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=["standard", "fnos"])
def test_non_success_response_is_unhealthy_without_falling_back_to_legacy(dockerfile: Path) -> None:
    if shutil.which("curl") is None:
        pytest.skip("curl is required by the shipped Docker command")
    with (
        _http_server(503) as (current_port, current_calls),
        _http_server() as (
            legacy_port,
            legacy_calls,
        ),
    ):
        result = subprocess.run(
            _command(dockerfile),
            env=_environment(
                {"INHERE_BOX_PORT": str(current_port), "OCTOP_PORT": str(legacy_port)}
            ),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 1
    assert current_calls == ["/api/health"]
    assert legacy_calls == []


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=["standard", "fnos"])
@pytest.mark.parametrize(
    "ports",
    [
        {},
        {"OCTOP_PORT": ""},
        {"INHERE_BOX_PORT": ""},
        {"INHERE_BOX_PORT": "", "OCTOP_PORT": "9099"},
        {"INHERE_BOX_PORT": "8088", "OCTOP_PORT": "9099"},
    ],
)
def test_default_port_selection_without_touching_an_existing_8088_instance(
    tmp_path: Path, dockerfile: Path, ports: dict[str, str]
) -> None:
    # Capture curl's arguments for the fixed default port. The real HTTP cases
    # above use ephemeral ports, so an actual user service on 8088 is untouched.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "curl-arguments"
    curl = bindir / "curl"
    curl.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HEALTHCHECK_TEST_LOG"\n', encoding="utf-8")
    curl.chmod(0o755)
    env = _environment(ports)
    env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
    env["HEALTHCHECK_TEST_LOG"] = str(log)
    result = subprocess.run(
        _command(dockerfile), env=env, capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    assert log.read_text(encoding="utf-8").splitlines() == [
        "-f",
        "http://localhost:8088/api/health",
    ]
