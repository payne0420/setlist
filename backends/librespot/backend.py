"""LibrespotBackend — Spotify's native ~320k OGG/Vorbis via librespot.

Implements the foundation's ``AudioBackend`` protocol. Opt-in, Premium-only, and
serialized (``max_concurrency = 1`` — a single Spotify session can't safely run
parallel native streams; the foundation's worker clamp enforces this). Designed so
the alpha librespot library breaking NEVER bricks Setlist: import/availability/
premium failures raise classified exceptions for the fallback chain to handle,
and the default download source stays YouTube.
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

from . import audio, search
from .errors import (
    LibrespotCancelled,
    LibrespotNotPremium,
    LibrespotUnavailable,
    NoExtendedCutError,
    OggCaptureError,
)
from .pacing import KeyThrottlePacer
from .session import LibrespotSession
from .webapi import ClientCredentialsToken


class LibrespotBackend:
    """Resolve a track to its Spotify id and capture the native OGG stream."""

    # A single Spotify session cannot safely run parallel native streams, and
    # concurrency raises ban risk. The playlist worker pool is clamped to this.
    max_concurrency = 1

    # Small randomized inter-track pause to avoid hammering Spotify (which triggers
    # "Failed fetching audio key" / bans). Applied between tracks, not before the first.
    INTER_TRACK_JITTER_S = (0.4, 1.3)

    def __init__(self, scraper, *, sleep=time.sleep):
        self._scraper = scraper
        self._sleep = sleep
        # Optional: the user's OWN Spotify app (Client-Credentials) for the extended
        # search. Its own rate-limit bucket makes /v1/search usable; without creds we
        # stay on librespot's hard-throttled keymaster token. See :mod:`.webapi`.
        cid = (getattr(scraper, "spotify_client_id", "") or "").strip()
        csec = (getattr(scraper, "spotify_client_secret", "") or "").strip()
        self._webapi_token = ClientCredentialsToken(cid, csec) if cid and csec else None
        self._session: LibrespotSession | None = None
        self._session_lock = threading.Lock()
        self._served_any = False
        self._pacer = KeyThrottlePacer(base_jitter_s=self.INTER_TRACK_JITTER_S)
        self._fetch_throttled = False
        self._pacing_announced = False
        # Rich Spotify metadata (album, clean artists, track/disc number, cover)
        # captured from the LoadedStream.track protobuf on the last native fetch — the
        # album the spotifydown embed lacks. The seam reads this after fetch() to
        # enrich the written tags. Same single-slot pattern (chain snapshots per-thread).
        self.last_track_metadata: dict | None = None

    # -- AudioBackend protocol --------------------------------------------

    def fetch(
        self,
        *,
        track,
        destination,
        extended,
        audio_format,
        audio_quality,
        cancel,
        has_fallback: bool = False,
    ):
        self.last_track_metadata = None
        # The seam hands us a ".mp3" destination; our real output is native ".ogg".
        dest_ogg = os.path.splitext(destination)[0] + ".ogg"
        if os.path.exists(dest_ogg):
            return dest_ogg, "ogg", extended

        self._throttle(cancel)
        if cancel():
            # Stop promptly on a cancel during the inter-track pause — don't open a
            # network session / run profile detection just to be torn down.
            raise LibrespotCancelled("cancelled before fetch")

        try:
            session = self._ensure_session()
        except LibrespotUnavailable:
            raise

        if session.is_known_free():
            # Only fail when we POSITIVELY know the account is non-Premium. An
            # inconclusive check (e.g. a 429 on the profile probe) must NOT downgrade a
            # Premium user — we attempt the native stream and let a genuinely free
            # account fail per-track.
            raise LibrespotNotPremium("Spotify Premium is required for native 320k OGG")

        base62 = track.id
        used_extended = False
        if extended:
            expected_s = (track.duration_ms / 1000) if track.duration_ms else None
            try:
                # Mint the user's app token (cached) when configured; None -> the
                # search falls back to the keymaster token. A token-fetch failure
                # (bad/expired secret) lands in the except below like any search error.
                web_token = self._webapi_token.get() if self._webapi_token else None
                chosen = search.find_extended_id(
                    session.raw,
                    title=track.title,
                    artists=track.artists,
                    expected_s=expected_s,
                    max_track_duration_s=self._scraper.max_track_duration_s,
                    web_token=web_token,
                )
                search_failed = False
            except Exception as exc:  # noqa: BLE001 - search HTTP/token failure
                # A failed search is NOT "no extended cut" — don't divert to YouTube on
                # a transient error; stream the original native track instead.
                self._emit(
                    f"Extended search failed for '{track.title}' ({exc}); "
                    "streaming the original Spotify track"
                )
                chosen = None
                search_failed = True
            if chosen:
                base62 = chosen
                used_extended = True
            elif not search_failed and has_fallback:
                raise NoExtendedCutError(f"No extended mix on Spotify for '{track.title}'")
            # else: no extended cut found -> stream the original Spotify track
            # natively (used_extended stays False, so the file isn't mislabeled).

        try:
            self._fetch_throttled = False
            final_path = audio.fetch_track_ogg(
                session.raw,
                base62,
                dest=dest_ogg,
                cancel=cancel,
                on_status=self._emit,
                on_metadata=self._capture_metadata,
                on_throttle=self._note_throttle,
            )
            if not self._fetch_throttled:
                self._pacer.note_success()
        except OggCaptureError:
            raise
        return final_path, "ogg", used_extended

    # -- internals ---------------------------------------------------------

    def close(self) -> None:
        """Tear down the librespot session at end-of-run / cancel.

        Called by ScraperThread.run()'s finally so a finished or cancelled download
        doesn't leave an authenticated background connection open. Idempotent.
        """
        with self._session_lock:
            if self._session is not None:
                self._session.close()
                self._session = None

    @property
    def raw_session(self):
        """The live raw librespot session, or None if not connected yet.

        Lets the scraper's metadata service reuse this session (for YouTube-fallback
        tracks within a librespot run) instead of opening a second login. Read after a
        fetch, when the session exists and isn't mid-stream (max_concurrency == 1)."""
        sess = self._session
        if sess is not None and sess.is_connected():
            return sess.raw
        return None

    def _ensure_session(self) -> LibrespotSession:
        """Lazily build + connect the session (reused across the run). Thread-safe."""
        with self._session_lock:
            if self._session is None or not self._session.is_connected():
                creds = getattr(self._scraper, "spotify_credentials_path", "") or None
                sess = LibrespotSession(creds)
                sess.connect_stored()  # reuse cached token; never opens a browser here
                self._session = sess
            return self._session

    def _throttle(self, cancel) -> None:
        """Cancel-aware jitter pause between tracks (skips the first)."""
        if not self._served_any:
            self._served_any = True
            return
        delay = self._pacer.next_delay()
        deadline = delay
        step = 0.2
        while deadline > 0:
            if cancel():
                return
            self._sleep(min(step, deadline))
            deadline -= step

    def _note_throttle(self) -> None:
        self._fetch_throttled = True
        floor = self._pacer.note_throttle()
        if not self._pacing_announced:
            self._pacing_announced = True
            self._emit(
                f"Spotify is rate-limiting audio keys; pacing downloads (~{floor:.0f}s between tracks)"
            )

    def _capture_metadata(self, meta: dict | None) -> None:
        """Stash the protobuf metadata from the just-captured stream for the seam."""
        self.last_track_metadata = meta

    def _emit(self, message: str) -> None:
        """Log a backend status line — to the console (logs) and the UI status bar."""
        print(f"[*] librespot: {message}")
        sig = getattr(self._scraper, "error_signal", None)
        if sig is not None:
            # status is non-essential; never let a UI-signal hiccup fail a download
            with contextlib.suppress(Exception):
                sig.emit(message)
