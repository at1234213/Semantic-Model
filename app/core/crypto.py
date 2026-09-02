"""Encryption for warehouse credentials.

Application-side rather than pgcrypto: pgcrypto would put the key into SQL
statements, where it reaches query logs and `pg_stat_activity`.
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

CURRENT_KEY_VERSION = 1


class CredentialEncryptionError(RuntimeError):
    """Raised for a missing, malformed, or non-current encryption key."""


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().credential_encryption_key
    if not key or key == "change-me":
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is unset or still the placeholder. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is not a valid Fernet key (base64-encoded 32 bytes)"
        ) from exc


def encrypt_secret(plaintext: str) -> tuple[bytes, int]:
    """Returns (ciphertext, key_version). Store both."""
    return _fernet().encrypt(plaintext.encode()), CURRENT_KEY_VERSION


def decrypt_secret(ciphertext: bytes, key_version: int) -> str:
    if key_version != CURRENT_KEY_VERSION:
        raise CredentialEncryptionError(
            f"Row was encrypted with key_version {key_version}, current is "
            f"{CURRENT_KEY_VERSION}. Re-encrypt before rotating."
        )
    try:
        return _fernet().decrypt(ciphertext).decode()
    except InvalidToken as exc:
        # Fernet authenticates as well as encrypts, so this also means tampering.
        raise CredentialEncryptionError(
            "Credential failed decryption: wrong key, or the ciphertext was modified"
        ) from exc
