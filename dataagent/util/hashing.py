"""Hashing helpers — never log raw secrets."""

from __future__ import annotations

import hashlib
import hmac
import secrets


def content_hash(payload: bytes | str) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def hmac_pseudonym(user_id: str, pepper: str, project_id: str) -> str:
    key = f"{pepper}:{project_id}".encode()
    return hmac.new(key, user_id.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def secret_hash(raw: str) -> str:
    salt = secrets.token_hex(8)
    digest = hashlib.sha256(f"{salt}:{raw}".encode()).hexdigest()
    return f"{salt}${digest}"


def verify_secret(raw: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.sha256(f"{salt}:{raw}".encode()).hexdigest()
    return hmac.compare_digest(check, digest)


def new_credential_token() -> str:
    return secrets.token_urlsafe(32)
