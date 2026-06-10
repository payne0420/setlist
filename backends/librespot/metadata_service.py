"""Best-effort rich-metadata lookup for the NON-librespot (e.g. YouTube) download path.

The librespot streaming path gets the real Spotify ``Metadata.Track`` protobuf for free
while it captures audio. The YouTube path has no such protobuf, and the no-auth Spotify
embed it builds ``song_meta`` from carries **no album and no track/disc number** (and the
anonymous Web API is heavily rate-limited — 429). The one reliable source of that metadata
is the authenticated librespot session's *metadata-only* Mercury fetch
(:func:`backends.librespot._librespot.track_metadata`), which streams nothing, needs no
audio key, and isn't subject to the anonymous Web-API limits.

:class:`LibrespotMetadataService` wraps that into a cached, thread-safe, **best-effort**
resolver: every failure mode (no cached credentials, alpha lib unavailable, connect error,
per-track fetch error) degrades to ``None`` so the caller simply keeps its embed-derived
``song_meta``. It either reuses a session the caller already owns (the librespot backend's,
to avoid a second login) or lazily connects its own metadata-only session from the cached
credentials and tears only that one down on :meth:`close`.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Callable

from . import _librespot as adapter
from . import track_metadata
from .session import LibrespotSession


class LibrespotMetadataService:
    """Resolve a Spotify track id to the rich tag dict, via metadata-only Mercury.

    Thread-safe: the YouTube path enriches from parallel download workers, and a single
    librespot session multiplexes Mercury requests over one connection, so lookups are
    serialized under a lock. Results are cached per track id (a run re-tags each track
    once, but the cache also makes a duplicated id free).
    """

    def __init__(
        self,
        credentials_path: str = "",
        *,
        session_provider: Callable[[], object | None] | None = None,
        connect: bool = True,
    ):
        # session_provider lets the caller hand us an already-connected raw librespot
        # session (the librespot backend's) so we don't open a second one. When it
        # returns None we lazily connect our own metadata-only session and own it.
        self._credentials_path = credentials_path
        self._session_provider = session_provider
        self._connect = connect
        self._own_session: LibrespotSession | None = None
        self._disabled = False
        self._cache: dict[str, dict | None] = {}
        self._lock = threading.Lock()

    def get(self, base62: str) -> dict | None:
        """Rich metadata for *base62* (album, clean artists, track/disc, cover…), or None.

        Never raises: any unavailability or fetch error returns ``None`` so the caller
        keeps whatever metadata it already has.
        """
        if not base62 or self._disabled:
            return None
        # The lock guards only the cache + session acquisition (and serializes the
        # one-time lazy connect). The Mercury fetch itself is an HTTP request via
        # librespot's ApiClient (requests.Session — thread-safe for concurrent calls),
        # so it runs OUTSIDE the lock: the YouTube path's parallel workers fetch
        # metadata concurrently instead of serializing behind one network round-trip.
        with self._lock:
            if base62 in self._cache:
                return self._cache[base62]
            raw = self._raw_session()
        if raw is None:
            return None  # disabled/unavailable — don't cache, the session may yet appear
        try:
            proto = adapter.track_metadata(raw, base62)
            meta = track_metadata.extract_from_proto(proto)
        except Exception:  # noqa: BLE001 - upstream raises broad types; stay best-effort
            # A single-track failure must not disable the service or fail a download.
            return None
        if meta is not None:
            # Cache only a real result; a None (empty proto / transient miss) stays
            # retryable rather than being pinned for the rest of the run.
            with self._lock:
                self._cache[base62] = meta
        return meta

    def close(self) -> None:
        """Tear down ONLY a session we opened ourselves (never the caller's).

        Must be called after all in-flight :meth:`get` calls have finished — the
        scraper guarantees this: its download ``ThreadPoolExecutor`` joins (``with``
        block, ``shutdown(wait=True)``) before ``ScraperThread.run``'s finally closes
        the service and the backend. So a reused backend session is never torn down
        from under an in-flight fetch."""
        with self._lock:
            if self._own_session is not None:
                with contextlib.suppress(Exception):
                    self._own_session.close()
                self._own_session = None

    # -- internals ---------------------------------------------------------

    def _raw_session(self):
        """Return a usable raw librespot session, or None (disabling on hard failure).

        Caller holds ``self._lock``.
        """
        # 1) Reuse a caller-supplied live session (the librespot backend's) if present.
        if self._session_provider is not None:
            with contextlib.suppress(Exception):
                raw = self._session_provider()
                if raw is not None:
                    return raw
        # 2) Else our own lazily-connected, metadata-only session.
        if self._own_session is not None:
            return self._own_session.raw
        if self._disabled or not self._connect:
            return None
        try:
            sess = LibrespotSession(self._credentials_path or None)
            if not sess.has_credentials():
                self._disabled = True  # not logged in -> no rich metadata available
                return None
            sess.connect_stored()
        except Exception:  # noqa: BLE001 - lib unavailable / auth failure -> stay best-effort
            self._disabled = True
            return None
        self._own_session = sess
        return sess.raw
