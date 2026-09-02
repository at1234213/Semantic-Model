"""Step 16b: warehouse credential encryption."""

import pytest

from app.core.crypto import (
    CURRENT_KEY_VERSION,
    CredentialEncryptionError,
    decrypt_secret,
    encrypt_secret,
)


def test_round_trip() -> None:
    ciphertext, version = encrypt_secret("hunter2")
    assert version == CURRENT_KEY_VERSION
    assert decrypt_secret(ciphertext, version) == "hunter2"


def test_ciphertext_does_not_contain_the_plaintext() -> None:
    ciphertext, _ = encrypt_secret("hunter2")
    assert b"hunter2" not in ciphertext


def test_same_plaintext_encrypts_differently_each_time() -> None:
    """Fernet includes a random IV, so identical passwords are not correlatable."""
    first, _ = encrypt_secret("hunter2")
    second, _ = encrypt_secret("hunter2")
    assert first != second


def test_tampered_ciphertext_is_rejected() -> None:
    """Fernet authenticates as well as encrypts."""
    ciphertext, version = encrypt_secret("hunter2")
    tampered = bytearray(ciphertext)
    tampered[-1] ^= 0x01
    with pytest.raises(CredentialEncryptionError):
        decrypt_secret(bytes(tampered), version)


def test_unknown_key_version_is_rejected() -> None:
    ciphertext, _ = encrypt_secret("hunter2")
    with pytest.raises(CredentialEncryptionError):
        decrypt_secret(ciphertext, CURRENT_KEY_VERSION + 1)


def test_unicode_and_url_hostile_passwords_survive() -> None:
    nasty = "p@ss:w/rd?#[]€ 'quoted'"
    ciphertext, version = encrypt_secret(nasty)
    assert decrypt_secret(ciphertext, version) == nasty
