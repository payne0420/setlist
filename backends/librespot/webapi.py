"""Spotify Web-API access via the user's OWN registered app (Client-Credentials flow).

The extended-version search hits the public ``/v1/search`` REST endpoint. Issued with
librespot's built-in keymaster ``client_id`` (``session.tokens()``), Spotify rate-limits
that to uselessness — a single search 429s, and a repeat request *inside* the cooldown
ESCALATES the penalty to a ~24h ban (observed Retry-After jumping 30-60s -> 86400s).

A token minted from the user's *own* registered app (client_id + secret, no user login)
is a SEPARATE rate-limit bucket with proper per-app limits, so ``/v1/search`` works —
verified to return 200 even while the keymaster path is mid-ban (the ban is keyed to the
client_id, not the IP). This is OPTIONAL: with no app credentials configured the backend
stays on the keymaster path (fail-fast, no extended search when rate-limited).
"""

from __future__ import annotations

import time

import requests

_TOKEN_URL = "https://accounts.spotify.com/api/token"
# Refresh this many seconds before the server-stated expiry so an in-flight search
# never races a token that expires between acquisition and use.
_EXPIRY_SKEW_S = 60


class ClientCredentialsToken:
    """Lazily fetch and cache a Client-Credentials bearer token (~1h lifetime).

    ``get()`` returns a valid token, fetching once and reusing it until ~1min before
    expiry. Thread-safety is not needed here: a librespot run is serialized (the
    backend declares ``max_concurrency == 1``), so a single un-locked cache is fine.
    """

    def __init__(self, client_id: str, client_secret: str, *, _clock=time.monotonic):
        self._client_id = client_id
        self._client_secret = client_secret
        self._clock = _clock
        self._token: str | None = None
        self._expiry: float = 0.0

    def get(self) -> str:
        """Return a valid bearer token, fetching/refreshing as needed.

        Raises (``requests`` error / KeyError) on an auth or network failure — the
        caller treats that exactly like any other extended-search failure (stream the
        original track), so a bad/expired secret never crashes a download.
        """
        now = self._clock()
        if self._token and now < self._expiry:
            return self._token
        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        ttl = body.get("expires_in", 3600)
        self._expiry = now + max(0, ttl - _EXPIRY_SKEW_S)
        return self._token
