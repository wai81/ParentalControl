"""Device tokens and pairing codes."""

import hashlib
import secrets


def new_device_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Tokens are long and random, so a plain SHA-256 is enough (no salt/scrypt needed)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_pairing_code() -> str:
    return f"{secrets.randbelow(10**8):08d}"
