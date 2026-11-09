"""HTTP setup helpers for box account integration tests."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec

from inhere_box.infra.server import InhereBoxServer


def _jwk(key: ec.EllipticCurvePrivateKey) -> dict[str, str]:
    numbers = key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": base64.urlsafe_b64encode(numbers.x.to_bytes(32, "big")).rstrip(b"=").decode(),
        "y": base64.urlsafe_b64encode(numbers.y.to_bytes(32, "big")).rstrip(b"=").decode(),
    }


def _proof(key: ec.EllipticCurvePrivateKey, challenge: dict[str, Any]) -> str:
    return jwt.encode(
        challenge["payload"], key, algorithm="ES256", headers={"typ": "inhere-box-proof+jwt"}
    )


async def _candidate(
    client: httpx.AsyncClient,
    grant: dict[str, Any],
    key: ec.EllipticCurvePrivateKey,
    *,
    name: str = "Phone",
    request_id: str = "claim-one",
) -> dict[str, Any]:
    response = await client.post(
        "/api/box/pairings",
        json={
            **({"grant_id": grant["grant_id"]} if "grant_id" in grant else {}),
            "secret": grant["secret"],
            "request_id": request_id,
            "public_key_jwk": _jwk(key),
            "name": name,
            "platform": "test",
        },
    )
    assert response.status_code == 200, response.text
    candidate = response.json()
    response = await client.post(
        f"/api/box/pairings/{candidate['pairing_id']}/proof",
        json={"proof": _proof(key, candidate["challenge"])},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _action(
    client: httpx.AsyncClient,
    key: ec.EllipticCurvePrivateKey,
    client_id: str,
    action: str,
    params: dict[str, str] | None = None,
) -> str:
    response = await client.post(
        "/api/auth/challenges",
        json={"client_id": client_id, "action": action, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    return _proof(key, response.json())


async def _login(
    client: httpx.AsyncClient, key: ec.EllipticCurvePrivateKey, client_id: str
) -> tuple[dict[str, str], dict[str, Any]]:
    proof = await _action(client, key, client_id, "session")
    response = await client.post("/api/auth/sessions", json={"proof": proof})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["expires_in"] == 600
    assert response.headers["cache-control"] == "no-store"
    replay = await client.post("/api/auth/sessions", json={"proof": proof})
    assert replay.status_code == 401
    return {"Authorization": f"Bearer {body['access_token']}"}, body


async def _management_key(server: InhereBoxServer) -> str:
    cached = getattr(server, "_test_management_key", None)
    if cached is None:
        assert server.box_account_service is not None
        result = await asyncio.to_thread(server.box_account_service.initialize_management_key)
        cached = result["management_key"]
        server._test_management_key = cached
    return cached


async def _admin_login(client: httpx.AsyncClient, server: InhereBoxServer) -> dict[str, str]:
    if client.base_url.host == "testserver":
        client.base_url = httpx.URL("http://localhost")
    headers = {"Origin": str(client.base_url).rstrip("/")}
    current = await client.get("/api/auth/browser/session", headers=headers)
    if current.status_code == 200:
        headers["X-Inhere-Box-CSRF"] = current.json()["csrf_token"]
    response = await client.post(
        "/api/auth/browser/sessions",
        headers=headers,
        json={"management_key": await _management_key(server)},
    )
    assert response.status_code == 200, response.text
    return {"Origin": headers["Origin"], "X-Inhere-Box-CSRF": response.json()["csrf_token"]}


async def _primary_candidate(
    client: httpx.AsyncClient,
    management_key: str,
    key: ec.EllipticCurvePrivateKey,
    *,
    request_id: str = "primary-one",
) -> dict[str, Any]:
    response = await client.post(
        "/api/box/owner-claims",
        json={
            "management_key": management_key,
            "request_id": request_id,
            "public_key_jwk": _jwk(key),
            "name": "Phone",
            "platform": "test",
        },
    )
    assert response.status_code == 200, response.text
    candidate = response.json()
    response = await client.post(
        f"/api/box/pairings/{candidate['pairing_id']}/proof",
        json={"proof": _proof(key, candidate["challenge"])},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _claim(
    client: httpx.AsyncClient, server: InhereBoxServer, *, request_id: str = "primary-one"
) -> tuple[ec.EllipticCurvePrivateKey, str, dict[str, str]]:
    management_key = await _management_key(server)
    key = ec.generate_private_key(ec.SECP256R1())
    pairing = await _primary_candidate(client, management_key, key, request_id=request_id)
    client_id = pairing["client_id"]
    auth, _session = await _login(client, key, client_id)
    return key, client_id, auth


def _legacy_account(service: Any) -> int:
    """Seed v61 credentials and trust exactly as preserved by migration 062."""
    from inhere_box.infra.auth.box_accounts.crypto import canonical

    with service._services.db.transaction() as conn:
        owner_id = service._repo.create_owner(conn, 1)
        conn.execute(
            "INSERT INTO box_paired_clients(client_id,owner_user_id,name,platform,public_key_json,public_key_fingerprint,state,created_at) VALUES('legacy',?,'old browser','web','{}','old','active',1)",
            (owner_id,),
        )
        conn.execute(
            "INSERT INTO box_auth_sessions(session_id,client_id,owner_user_id,auth_revision,boot_id,created_at,expires_at) VALUES('old-session','legacy',?,1,'old-boot',1,9999999999)",
            (owner_id,),
        )
        conn.execute("UPDATE box_account SET auth_model_version=1,auth_revision=1")
        record = service._record(conn, legacy=True)
        service._repo.save_trust(conn, revision=1, digest=record["digest"])
    service._trust.path.write_text(canonical(record))
    return int(owner_id)
