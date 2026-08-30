from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class TokenEncryptionError(Exception):
    """Raised when TOKEN_ENCRYPTION_KEY is missing/malformed, or a value fails to decrypt."""


def encrypt(plaintext: str, key: str) -> str:
    """Encrypt a credentials JSON blob for storage in connected_accounts.credentials_json."""
    if not key:
        raise TokenEncryptionError(
            "TOKEN_ENCRYPTION_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"` "
            "and set it in .env before running the web app."
        )
    try:
        fernet = Fernet(key.encode())
    except ValueError as exc:
        raise TokenEncryptionError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key.") from exc
    return fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, key: str) -> str:
    """Decrypt a credentials JSON blob read from connected_accounts.credentials_json."""
    if not key:
        raise TokenEncryptionError("TOKEN_ENCRYPTION_KEY is not set; cannot decrypt stored credentials.")
    try:
        fernet = Fernet(key.encode())
        return fernet.decrypt(ciphertext.encode()).decode()
    except (ValueError, InvalidToken) as exc:
        raise TokenEncryptionError(
            "Failed to decrypt stored credentials — TOKEN_ENCRYPTION_KEY may have changed."
        ) from exc
