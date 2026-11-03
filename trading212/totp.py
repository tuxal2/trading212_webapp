"""TOTP codes (RFC 6238) for the 2FA of unattended logins.

The secret is the one shown when setting up the authenticator app (base32 text) or the
whole ``otpauth://totp/...`` URI from the QR code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, urlparse

from .exceptions import ConfigError


def _parse(secret: str) -> tuple[bytes, int, int, str]:
    digits, period, algorithm = 6, 30, "sha1"
    s = secret.strip()
    if s.lower().startswith("otpauth://"):
        q = {k.lower(): v[0] for k, v in parse_qs(urlparse(s).query).items()}
        s = q.get("secret", "")
        digits = int(q.get("digits", digits))
        period = int(q.get("period", period))
        algorithm = q.get("algorithm", algorithm).lower()
    s = s.replace(" ", "").replace("-", "").upper()
    try:
        key = base64.b32decode(s + "=" * (-len(s) % 8))
    except (ValueError, base64.binascii.Error) as exc:
        raise ConfigError("totp_secret is not a valid base32 secret / otpauth URI") from exc
    if not key or algorithm not in ("sha1", "sha256", "sha512"):
        raise ConfigError("totp_secret is not a valid base32 secret / otpauth URI")
    return key, digits, period, algorithm


def totp(secret: str, at: float | None = None) -> str:
    """Current code for ``secret`` (or the code valid at Unix time ``at``)."""
    key, digits, period, algorithm = _parse(secret)
    counter = int((time.time() if at is None else at) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algorithm)).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)
