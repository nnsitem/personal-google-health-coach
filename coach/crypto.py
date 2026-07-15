"""Fernet encryption for user credentials stored in SQLite.

All Google tokens and Gemini API keys are encrypted before writing to the DB
and decrypted on read. The encryption key is a single server-side secret
stored in .env as ENCRYPTION_KEY.

If ENCRYPTION_KEY is not set, encryption is disabled (plaintext passthrough)
for backward compatibility with existing unencrypted data.

Generate a key:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import logging
import os

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

_KEY = os.environ.get("ENCRYPTION_KEY", "")
_fernet = None

if _KEY:
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(_KEY.encode() if isinstance(_KEY, str) else _KEY)
    except Exception as e:
        log.warning("ENCRYPTION_KEY is set but invalid (%s) — encryption disabled", e)
        _fernet = None


def encrypt(plaintext: str) -> str:
    """Encrypt a string. Returns the ciphertext as a base64 string.

    If encryption is disabled (no key), returns the plaintext unchanged.
    """
    if not _fernet or not plaintext:
        return plaintext
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a string. Returns the plaintext.

    If encryption is disabled or the value isn't encrypted (legacy plaintext),
    returns the input unchanged.
    """
    if not _fernet or not ciphertext:
        return ciphertext
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except Exception:
        if ciphertext.startswith("gAAAAA"):
            # Fernet ciphertext that our key can't open — almost certainly a
            # rotated/changed ENCRYPTION_KEY. Returning it as-is makes the
            # garbage flow downstream (e.g. 400s from Gemini), so shout.
            log.warning(
                "value looks Fernet-encrypted but failed to decrypt — has "
                "ENCRYPTION_KEY changed? Returning raw ciphertext."
            )
        # Otherwise it's unencrypted legacy data — return as-is
        return ciphertext


def is_encrypted(value: str) -> bool:
    """True if the value is ciphertext our active key can decrypt."""
    if not _fernet or not value:
        return False
    try:
        _fernet.decrypt(value.encode())
        return True
    except Exception:
        return False


def is_enabled() -> bool:
    """Check whether encryption is active."""
    return _fernet is not None
