"""Opaque workshop invite tokens — store only SHA-256 hash in the database."""

from __future__ import annotations

import hashlib
import secrets


def new_raw_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
