"""Single-owner password login for the hosted CourtTrack dashboard.

No third-party identity service is needed. Public court data remains public at
its source; this gate protects the user's own case list and mutation endpoints.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import Request

CLOUD_MODE = os.getenv('CLOUD_MODE', '0') == '1'
PASSWORD = os.getenv('COURTTRACK_PASSWORD', '')
SESSION_SECRET = os.getenv('COURTTRACK_SESSION_SECRET', '')
COOKIE = 'courttrack_session'
EXPIRES_SECONDS = 12 * 60 * 60
_attempts = defaultdict(deque)
_lock = threading.Lock()


def validate_configuration() -> None:
    if CLOUD_MODE and (len(PASSWORD) < 12 or len(SESSION_SECRET) < 32):
        raise RuntimeError('Cloud mode requires COURTTRACK_PASSWORD (12+ chars) and COURTTRACK_SESSION_SECRET (32+ chars).')


def _urlsafe(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))


def new_session() -> str:
    validate_configuration()
    raw = json.dumps({'exp': int(time.time() + EXPIRES_SECONDS), 'n': secrets.token_hex(12)},
                     separators=(',', ':')).encode('utf8')
    encoded = _urlsafe(raw)
    signature = _urlsafe(hmac.new(SESSION_SECRET.encode(), encoded.encode(), hashlib.sha256).digest())
    return encoded + '.' + signature


def valid_session(value: str | None) -> bool:
    if not CLOUD_MODE:
        return True
    if not value or not SESSION_SECRET:
        return False
    try:
        encoded, signature = value.split('.', 1)
        expected = _urlsafe(hmac.new(SESSION_SECRET.encode(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return False
        data = json.loads(_unb64(encoded))
        return isinstance(data['exp'], int) and time.time() < data['exp'] <= time.time() + EXPIRES_SECONDS
    except (ValueError, KeyError, TypeError, OverflowError, binascii.Error):
        return False


def check_password(guess: str) -> bool:
    return bool(PASSWORD) and hmac.compare_digest(
        hashlib.sha256(guess.encode()).digest(), hashlib.sha256(PASSWORD.encode()).digest())


def allow_attempt(identity: str) -> bool:
    """Block further login attempts after eight tries in 15 minutes."""
    now = time.monotonic()
    with _lock:
        q = _attempts[identity]
        while q and q[0] < now - 900:
            q.popleft()
        if len(q) >= 8:
            return False
        q.append(now)
        return True


def trusted_mutation(request: Request) -> bool:
    """Prevent browser cross-site POST/DELETE to authenticated endpoints."""
    site = request.headers.get('sec-fetch-site', '')
    if site and site not in ('same-origin', 'none'):
        return False
    origin = request.headers.get('origin')
    if origin:
        parsed = urlsplit(origin)
        return parsed.scheme == ('https' if CLOUD_MODE else request.url.scheme) and parsed.netloc == request.headers.get('host')
    return True
