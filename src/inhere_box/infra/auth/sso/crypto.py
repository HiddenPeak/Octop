"""Fernet encryption for OIDC single sign-on secrets."""

from __future__ import annotations

from inhere_box.infra.crypto.fernet import PURPOSE_SSO, decrypt_str, encrypt_str
from inhere_box.infra.db.repos.secrets import SecretRepo


def encrypt_secret(secret_repo: SecretRepo, plain: str) -> bytes:
    """Encrypt an SSO secret using the installation-specific Fernet key."""
    return encrypt_str(secret_repo, PURPOSE_SSO, plain)


def decrypt_secret(secret_repo: SecretRepo, encrypted: bytes) -> str:
    """Decrypt an SSO secret using the installation-specific Fernet key."""
    return decrypt_str(secret_repo, PURPOSE_SSO, encrypted)
