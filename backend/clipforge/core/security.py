"""Password hashing and session tokens using only the standard library.

scrypt is memory-hard (resists GPU cracking) and ships with Python, so there is no native
dependency to build. Stored format: ``scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>`` so
parameters can be raised later without invalidating existing hashes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from functools import lru_cache

_N, _R, _P = 2**15, 8, 1
_DKLEN = 32
_MAXMEM = 128 * 1024 * 1024


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN, maxmem=_MAXMEM
    )
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=base64.b64decode(salt_b64),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


@lru_cache(maxsize=1)
def dummy_password_hash() -> str:
    """A throwaway hash verified when an email does not exist, keeping login timing
    constant so response time cannot be used to enumerate accounts."""
    return hash_password(secrets.token_urlsafe(16))


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii", "ignore")).digest()
