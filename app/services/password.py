"""Secure password generator for Workday account creation."""

from __future__ import annotations

import secrets
import string


_SAFE_SYMBOLS = "!@#$%^&*-_=+"

_LOWERCASE = string.ascii_lowercase
_UPPERCASE = string.ascii_uppercase
_DIGITS = string.digits


def generate_password(length: int = 20) -> str:
    """Generate a cryptographically secure password.

    Guarantees at least one lowercase, one uppercase, one digit, and one safe
    symbol. Uses ``secrets`` for character selection and ``SystemRandom`` for
    final shuffle ordering.
    """
    if length < 4:
        raise ValueError("length must be >= 4 to satisfy all character classes")

    # Guarantee one of each required class.
    required = [
        secrets.choice(_LOWERCASE),
        secrets.choice(_UPPERCASE),
        secrets.choice(_DIGITS),
        secrets.choice(_SAFE_SYMBOLS),
    ]

    # Fill remainder from the full allowed alphabet.
    alphabet = _LOWERCASE + _UPPERCASE + _DIGITS + _SAFE_SYMBOLS
    rest = [secrets.choice(alphabet) for _ in range(length - len(required))]

    chars = required + rest
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
