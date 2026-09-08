"""Minting and verifying API keys."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.api_key import KEY_PREFIX, PREFIX_LENGTH, ApiKey

# 32 random bytes. A digest is sufficient protection for a secret with this much
# entropy; a password KDF exists to slow down guessing, and there is nothing
# here to guess.
KEY_BYTES = 32


@dataclass
class IssuedKey:
    api_key: ApiKey
    secret: str  # shown once, never stored


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def issue(
    db: Session, *, tenant_id: uuid.UUID, name: str, is_admin: bool = False
) -> IssuedKey:
    secret = KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)
    api_key = ApiKey(
        tenant_id=tenant_id,
        name=name,
        prefix=secret[len(KEY_PREFIX):len(KEY_PREFIX) + PREFIX_LENGTH],
        key_hash=_hash(secret),
        is_admin=is_admin,
    )
    db.add(api_key)
    db.flush()
    return IssuedKey(api_key=api_key, secret=secret)


def verify(db: Session, secret: str) -> ApiKey | None:
    """Return the key if the secret is valid and not revoked, else None."""
    if not secret or not secret.startswith(KEY_PREFIX):
        return None

    prefix = secret[len(KEY_PREFIX):len(KEY_PREFIX) + PREFIX_LENGTH]
    expected = _hash(secret)

    # The prefix narrows to one candidate; the comparison itself is
    # constant-time so a wrong key reveals nothing through timing.
    for candidate in db.scalars(select(ApiKey).where(ApiKey.prefix == prefix)):
        if secrets.compare_digest(candidate.key_hash, expected):
            if candidate.revoked_at is not None:
                return None
            candidate.last_used_at = datetime.now(UTC)
            return candidate
    return None


def revoke(db: Session, api_key: ApiKey) -> None:
    api_key.revoked_at = datetime.now(UTC)
    db.flush()
