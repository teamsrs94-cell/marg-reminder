import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
        new = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(new.hex(), dk)
    except Exception:
        return False


def new_csrf() -> str:
    return secrets.token_urlsafe(24)


class LoginLimiter:
    """5 failed logins per 10 minutes per key (email+ip)."""

    def __init__(self, limit=5, window=600):
        self.limit, self.window = limit, window
        self.fails: dict[str, list[float]] = defaultdict(list)

    def _prune(self, key):
        cutoff = time.time() - self.window
        self.fails[key] = [t for t in self.fails[key] if t > cutoff]

    def blocked(self, key) -> bool:
        self._prune(key)
        return len(self.fails[key]) >= self.limit

    def fail(self, key):
        self._prune(key)
        self.fails[key].append(time.time())

    def reset(self, key):
        self.fails.pop(key, None)


# ---- password-reset links: signed, expire in 1 hour, and die as soon as the password changes
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

RESET_MAX_AGE = 3600


def _fp(password_hash: str) -> str:
    return hashlib.sha256(password_hash.encode()).hexdigest()[:16]


def make_reset_token(secret_key: str, user_id: int, password_hash: str) -> str:
    return URLSafeTimedSerializer(secret_key, salt="pw-reset").dumps({"u": user_id, "h": _fp(password_hash)})


def read_reset_token(secret_key: str, token: str):
    """Returns (user_id, fingerprint) or None if the link is invalid or older than 1 hour."""
    try:
        d = URLSafeTimedSerializer(secret_key, salt="pw-reset").loads(token, max_age=RESET_MAX_AGE)
        return int(d["u"]), str(d["h"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


def reset_token_matches(fingerprint: str, password_hash: str) -> bool:
    return hmac.compare_digest(fingerprint, _fp(password_hash))
