"""Strict P-256 public keys and ES256 proofs (no client-selected verification keys)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

PROOF_TYPE = "inhere-box-proof+jwt"
AUDIENCE = "inhere-box-api"


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def parse_public_key(value: dict[str, Any]) -> tuple[str, str]:
    if set(value) != {"kty", "crv", "x", "y"}:
        raise ValueError("public key fields")
    if value["kty"] != "EC" or value["crv"] != "P-256":
        raise ValueError("public key algorithm")
    if any(not isinstance(value[key], str) or len(value[key]) != 43 for key in ("x", "y")):
        raise ValueError("public key coordinates")
    parsed = jwt.PyJWK.from_dict(value, algorithm="ES256")
    if not isinstance(parsed.key, ec.EllipticCurvePublicKey):
        raise ValueError("public key type")
    normalized = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(parsed.key))
    if normalized != value:
        raise ValueError("noncanonical public key")
    return canonical(value), digest(value)


def proof_challenge_id(proof: str) -> str:
    if len(proof) > 8192:
        raise ValueError("proof size")
    header = jwt.get_unverified_header(proof)
    if header != {"alg": "ES256", "typ": PROOF_TYPE}:
        raise ValueError("proof header")
    claims = jwt.decode(proof, options={"verify_signature": False})
    value = claims.get("challenge_id")
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("challenge id")
    return value


def verify_proof(proof: str, public_key_json: str, expected: dict[str, Any]) -> None:
    key = jwt.PyJWK.from_dict(json.loads(public_key_json), algorithm="ES256").key
    claims = jwt.decode(
        proof,
        key,
        algorithms=["ES256"],
        options={"verify_aud": False, "verify_exp": False, "verify_iat": False},
    )
    # The server's saved complete payload is the authority, including expiry,
    # boot id, action and parameter digest. Extra claims are not accepted.
    if canonical(claims) != canonical(expected):
        raise ValueError("proof payload")
