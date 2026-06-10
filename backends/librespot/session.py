"""librespot session lifecycle: login, credential cache, Premium gating.

Wraps the :mod:`._librespot` adapter so the rest of the backend (and the PyQt UI)
never imports the alpha library directly. A :class:`LibrespotSession` owns one
connected librespot Session plus the path to its cached ``credentials.json`` (a
reusable token, never the password).
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Callable

import requests

from . import _librespot as adapter
from .errors import LibrespotAuthError, LibrespotUnavailable

_PROFILE_URL = "https://api.spotify.com/v1/me"


def _retry_after_seconds(resp, default: float = 2.0) -> float:
    """Honor a 429 Retry-After header, clamped to a few seconds."""
    try:
        return min(float(resp.headers.get("Retry-After", default)), 10.0)
    except (TypeError, ValueError):
        return default


def _config_base() -> str:
    """Per-user config base dir, matching Setlist's own convention (Spotify_Downloader._config_dir)."""
    if sys.platform == "win32":
        return os.environ.get("APPDATA", os.path.expanduser("~"))
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    return os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))


def default_credentials_path() -> str:
    """Default location for the cached librespot credentials file.

    ``<config base>/Setlist/librespot/credentials.json``. Kept under Setlist's
    config dir so it survives reinstalls and is never world-readable (chmod 0600
    after write, see :meth:`LibrespotSession._secure_credentials`).
    """
    return os.path.join(_config_base(), "Setlist", "librespot", "credentials.json")


class LibrespotSession:
    """One connected librespot Session + its credential file. Premium-gated.

    Construction is cheap and never touches the alpha library; call
    :meth:`connect_stored` (reuse cached token) or :meth:`connect_oauth` (browser
    login, BLOCKING — run off the UI thread) to actually connect.
    """

    def __init__(self, credentials_path: str | None = None):
        self._credentials_path = credentials_path or default_credentials_path()
        self._session = None
        self._product: str | None = None

    @property
    def credentials_path(self) -> str:
        return self._credentials_path

    @property
    def raw(self):
        """The underlying librespot Session (for audio/search). None until connected."""
        return self._session

    def has_credentials(self) -> bool:
        return adapter.has_stored_credentials(self._credentials_path)

    def is_connected(self) -> bool:
        return self._session is not None

    # -- connect -----------------------------------------------------------

    def connect_stored(self) -> LibrespotSession:
        """Resume from the cached credentials file. Raises if unavailable/not logged in."""
        self._require_available()
        if not self.has_credentials():
            raise LibrespotAuthError("Not logged in to Spotify (no cached credentials)")
        try:
            self._session = adapter.login_stored(self._credentials_path)
        except LibrespotUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - upstream raises broad types
            raise LibrespotAuthError(f"Spotify login failed: {exc}") from exc
        # Tighten perms on every connect: a credentials file from an older version or
        # a lax umask may still be world-readable.
        self._secure_credentials()
        self._after_connect()
        return self

    def connect_oauth(self, on_auth_url: Callable[[str], None]) -> LibrespotSession:
        """Interactive OAuth login (BLOCKING). ``on_auth_url`` opens the browser.

        On success the alpha lib writes the credentials file; we then chmod it 0600.
        """
        self._require_available()
        os.makedirs(os.path.dirname(self._credentials_path), exist_ok=True)
        try:
            self._session = adapter.login_oauth(self._credentials_path, on_auth_url)
        except LibrespotUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - upstream raises broad types
            raise LibrespotAuthError(f"Spotify login failed: {exc}") from exc
        self._secure_credentials()
        self._after_connect()
        return self

    # -- account info ------------------------------------------------------

    @property
    def product(self) -> str | None:
        """Account product type, lowercased ("premium" | "free" | None)."""
        return self._product

    def is_premium(self) -> bool:
        return self._product == "premium"

    def is_known_free(self) -> bool:
        """True only when we POSITIVELY detected a non-Premium account.

        ``None`` (detection inconclusive) is NOT "free": the backend then still
        attempts the native stream so a flaky/rate-limited check never downgrades a
        Premium user to YouTube. A genuinely free account's stream fails and falls
        back per-track."""
        return self._product is not None and self._product != "premium"

    def username(self) -> str | None:
        if self._session is None:
            return None
        return adapter.account_username(self._session)

    def close(self) -> None:
        if self._session is not None:
            with contextlib.suppress(Exception):  # best-effort teardown
                self._session.close()
            self._session = None

    # -- internals ---------------------------------------------------------

    def _require_available(self) -> None:
        if not adapter.is_available():
            raise LibrespotUnavailable(f"librespot library unavailable: {adapter.import_error()}")

    def _after_connect(self) -> None:
        self._product = self._detect_product()

    def _detect_product(self, *, _attr_polls: int = 8, _sleep=time.sleep) -> str | None:
        """Premium/free detection. Returns "premium" | "free"/other | None (unknown).

        PRIMARY: librespot's own ``get_user_attribute("type")`` — it arrives over the
        session's Mercury connection (effectively at connect time), is NOT subject to
        Spotify Web-API rate limits, and each connect re-sends the full attribute set
        for the authenticated account, so the always-present ``type`` key reflects THIS
        account (no stale cross-login leak). This is the right signal for a librespot
        session; we poll it briefly in case a slow connection delays the event.

        SECONDARY (fallback only): the Web-API ``/v1/me`` ``product`` field, which needs
        the ``user-read-private`` scope and is rate-limited — we retry a 429 a couple of
        times. If everything is inconclusive we return None (NOT "free"); the backend
        then attempts the native stream anyway so a 429 can't downgrade a Premium user.
        """
        for i in range(max(1, _attr_polls)):
            try:
                attr = adapter.product_type(self._session)
            except Exception:  # noqa: BLE001 - missing attr / not-yet-authed
                attr = None
            if attr:
                return str(attr).lower()
            if i + 1 < _attr_polls:
                _sleep(0.25)

        for attempt in range(3):
            try:
                token = adapter.web_bearer_token(self._session, "user-read-private")
                resp = requests.get(
                    _PROFILE_URL,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=15,
                )
            except Exception:  # noqa: BLE001 - profile lookup is best-effort
                return None
            if resp.status_code == 200:
                return str(resp.json().get("product") or "").lower() or None
            if resp.status_code == 429 and attempt < 2:
                _sleep(_retry_after_seconds(resp))
                continue
            return None
        return None

    def _secure_credentials(self) -> None:
        """chmod the credentials file to 0600 (librespot writes it world-readable)."""
        with contextlib.suppress(OSError):
            os.chmod(self._credentials_path, 0o600)
