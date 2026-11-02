"""Symmetric encryption for credentials stored in the database (Settrade secrets, PIN).

The key comes from FIELD_ENCRYPTION_KEY in .env (a Fernet key). If it is not set, a key is
derived from DJANGO_SECRET_KEY, so changing the secret key makes stored credentials unreadable
(you would simply re-enter them on the Settings page).
"""
import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.db import models

PREFIX = "enc1:"


@lru_cache(maxsize=4)
def _fernet(key: str, secret: str) -> Fernet:
    if key:
        return Fernet(key.encode())
    derived = hashlib.sha256(f"algo-trader-fields:{secret}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def fernet() -> Fernet:
    return _fernet(getattr(settings, "FIELD_ENCRYPTION_KEY", ""), settings.SECRET_KEY)


def encrypt(value: str) -> str:
    if not value or value.startswith(PREFIX):
        return value
    return PREFIX + fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value or not value.startswith(PREFIX):
        return value
    try:
        return fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        # Key changed: treat as missing so the user is asked to re-enter it.
        return ""


class EncryptedTextField(models.TextField):
    """TextField that is encrypted at rest and transparently decrypted on load."""

    def from_db_value(self, value, expression, connection):
        return decrypt(value) if value else value

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        return encrypt(value) if value else value
