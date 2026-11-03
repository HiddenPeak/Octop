"""Tests for public OIDC callback URL helpers."""

from __future__ import annotations

from starlette.requests import Request

from inhere_box.api.common.public_base import resolve_public_base
from inhere_box.infra.auth.sso.public_base import build_redirect_uri


def _request(*, scheme: str = "http", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "scheme": scheme,
            "server": ("internal.example", 8080),
            "path": "/",
            "headers": headers or [(b"host", b"internal.example:8080")],
        }
    )


def test_resolve_public_base_uses_forwarded_proxy_headers() -> None:
    request = _request(
        headers=[
            (b"host", b"internal.example:8080"),
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"inhere-box.example"),
        ]
    )

    assert resolve_public_base(request) == "https://inhere-box.example"


def test_resolve_public_base_uses_request_url_without_proxy_headers() -> None:
    request = _request(scheme="https", headers=[(b"host", b"inhere-box.example")])

    assert resolve_public_base(request) == "https://inhere-box.example"


def test_build_redirect_uri_uses_oidc_callback_path() -> None:
    assert (
        build_redirect_uri("https://inhere-box.example/")
        == "https://inhere-box.example/api/auth/oidc/callback"
    )


def test_build_redirect_uri_accepts_oauth_callback_path() -> None:
    assert (
        build_redirect_uri("https://inhere-box.example", "/api/auth/oauth/callback")
        == "https://inhere-box.example/api/auth/oauth/callback"
    )
