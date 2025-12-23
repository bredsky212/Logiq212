"""
Helpers for AI key encryption and fingerprinting.
"""

import base64
import hashlib
import os
from typing import Dict

from nacl import secret, utils

ENC_SECRET_ENV = "LOGIQ_AI_KEY_ENC_SECRET"


def _derive_key(secret_value: str) -> bytes:
    return hashlib.sha256(secret_value.encode("utf-8")).digest()


def _get_secret_key() -> bytes:
    secret_value = os.getenv(ENC_SECRET_ENV)
    if not secret_value:
        raise ValueError(f"{ENC_SECRET_ENV} is not set")
    return _derive_key(secret_value)


def encrypt_api_key(api_key: str) -> Dict[str, str]:
    """Encrypt an API key using SecretBox."""
    box = secret.SecretBox(_get_secret_key())
    nonce = utils.random(secret.SecretBox.NONCE_SIZE)
    encrypted = box.encrypt(api_key.encode("utf-8"), nonce)
    return {
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(encrypted.ciphertext).decode("ascii"),
    }


def decrypt_api_key(payload: Dict[str, str]) -> str:
    """Decrypt an encrypted API key payload."""
    box = secret.SecretBox(_get_secret_key())
    nonce = base64.b64decode(payload["nonce"])
    ciphertext = base64.b64decode(payload["ciphertext"])
    return box.decrypt(ciphertext, nonce).decode("utf-8")


def fingerprint_api_key(api_key: str) -> str:
    """Create a short fingerprint for display."""
    last4 = api_key[-4:] if len(api_key) >= 4 else api_key
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:8]
    return f"{digest}:{last4}"
