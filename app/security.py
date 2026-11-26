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
