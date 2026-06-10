"""Shared helpers for the lossless package: cache dir, JSON cache, AES-GCM.

Deliberately free of any dependency on ``Spotify_Downloader`` so the whole
``lossless/`` package imports cleanly in isolation (the test suite exercises it
without booting PyQt).
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from typing import Any

from .constants import DEFAULT_ACCEPT, DEFAULT_DOWNLOADER_USER_AGENT

# Loopback-only plain-http instance URLs: http://localhost / 127.0.0.1 / [::1],
# optionally with a port. Anything else over http is rejected.
_LOOPBACK_HTTP_RE = re.compile(
    r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d{1,5})?$", re.IGNORECASE
)


def normalize_tidal_api_url(value: str) -> str:
    """Trim + validate a user-supplied Tidal API instance URL.

    Mirrors SpotiFLAC's ``normalizeCustomTidalAPIValue`` (trailing slash
    stripped, ``https://`` or empty). DIVERGENCE (intentional): plain
    ``http://`` is additionally accepted for **loopback hosts only**
    (localhost / 127.0.0.1 / [::1]) so a self-hosted instance on this machine
    works without a TLS proxy; any remote host still requires ``https://`` so
    credentials-bearing traffic never goes plaintext over a network.
    """
    v = (value or "").strip().rstrip("/")
    if v.startswith("https://"):
        return v
    m = _LOOPBACK_HTTP_RE.match(v)
    if m:
        port = m.group(2)  # ":<port>" or None
        if port is None or 1 <= int(port[1:]) <= 65535:
            return v
    return ""


def cache_dir() -> str | None:
    """Per-user cache directory for lossless resolver state (tokens, ISRC, creds).

    Mirrors the app's ``_config_dir`` placement but under a ``cache`` subfolder,
    so transient resolver state never mixes with ``config.json``. Returns ``None``
    (degrading to an in-memory-only cache) if the directory can't be created —
    the cache is an optimization and must never block a download (matches
    SpotiFLAC treating cache errors as a miss).
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
    path = os.path.join(base, "Setlist", "cache")
    try:
        os.makedirs(path, exist_ok=True)
        return path
    except OSError:
        return None


class JsonStore:
    """A tiny thread-safe JSON-file key/value store.

    Replaces SpotiFLAC's bbolt cache with a plain JSON file (the ISRC cache has
    no TTL; callers that need a TTL — e.g. the anon token — check expiry on the
    stored value themselves). Best-effort: read/write failures never raise, they
    just behave as a cache miss / skipped write so a download is never blocked on
    cache IO.
    """

    def __init__(self, filename: str) -> None:
        # Resolve the path lazily and defensively: a cache-dir failure must never
        # raise out of construction (the resolver stays usable, in-memory only).
        self._filename = filename
        self._path: str | None = None
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None

    def _ensure_path(self) -> str | None:
        if self._path is None:
            d = cache_dir()
            self._path = os.path.join(d, self._filename) if d else ""
        return self._path or None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            self._data = {}
            path = self._ensure_path()
            if path:
                try:
                    with open(path, encoding="utf-8") as fh:
                        loaded = json.load(fh)
                    if isinstance(loaded, dict):
                        self._data = loaded
                except (OSError, ValueError):
                    pass
        return self._data

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._load().get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            data = self._load()
            data[key] = value
            path = self._ensure_path()
            if not path:
                return  # in-memory only; cache is an optimization, not a dependency
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh)
                os.replace(tmp, path)
            except OSError:
                pass  # cache is an optimization, never a hard dependency


def aesgcm_decrypt(seed_parts, nonce: bytes, ciphertext: bytes, tag: bytes, aad: bytes) -> bytes:
    """Decrypt a SpotiFLAC-style embedded secret.

    key = SHA-256(concat(seed_parts)); AES-256-GCM open of (ciphertext||tag) with
    the given 12-byte nonce and AAD. Mirrors Go's ``gcm.Open(nil, nonce,
    ciphertext||tag, aad)`` exactly (cryptography's AESGCM also expects the tag
    appended to the ciphertext).
    """
    import hashlib

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = hashlib.sha256(b"".join(seed_parts)).digest()
    return AESGCM(key).decrypt(nonce, ciphertext + tag, aad)


def default_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """The shared downloader headers (UA + Accept), plus any per-call extras.

    Mirrors ``NewRequestWithDefaultHeaders`` (http_headers.go): User-Agent =
    Chrome 146, Accept = ``application/json, text/plain, */*``.
    """
    headers = {"User-Agent": DEFAULT_DOWNLOADER_USER_AGENT, "Accept": DEFAULT_ACCEPT}
    if extra:
        headers.update(extra)
    return headers
