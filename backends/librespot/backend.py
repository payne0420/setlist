"""LibrespotBackend — Spotify's native ~320k OGG/Vorbis via librespot.

Implements the foundation's ``AudioBackend`` protocol. Opt-in, Premium-only, and
serialized (``max_concurrency = 1`` — a single Spotify session can't safely run
parallel native streams; the foundation's worker clamp enforces this). Designed so
the alpha librespot library breaking NEVER bricks Setlist: import/availability/
premium failures degrade to the YouTube backend with a clear status, and the
default download source stays YouTube.
"""

from __future__ import annotations

import contextlib
import os
import random
import threading
import time

from ..youtube import YouTubeBackend
from . import audio, search
from .errors import (
    LibrespotCancelled,
    LibrespotNotPremium,
    LibrespotUnavailable,
    OggCaptureError,
)
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

    def __init__(self, scraper, *, allow_youtube_fallback: bool = True, sleep=time.sleep):
        self._scraper = scraper
        self._allow_fallback = allow_youtube_fallback
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
        self._youtube: YouTubeBackend | None = None
        # Reliable per-fetch signal of whether THIS track was served by the YouTube
        # fallback (read by the seam to flag History). Set per fetch(); safe because a
        # librespot run is serialized (max_concurrency == 1).
        self.used_youtube_fallback = False
        # Rich Spotify metadata (album, clean artists, track/disc number, cover)
        # captured from the LoadedStream.track protobuf on the last native fetch — the
        # album the spotifydown embed lacks. The seam reads this after fetch() to
        # enrich the written tags. Same single-slot pattern as used_youtube_fallback
        # (safe: max_concurrency == 1). None when this track fell back to YouTube
        # (no protobuf) or the load was an episode.
        self.last_track_metadata: dict | None = None

    # -- AudioBackend protocol --------------------------------------------

    def fetch(self, *, track, destination, extended, audio_format, audio_quality, cancel):
        self.used_youtube_fallback = False
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
        except LibrespotUnavailable as exc:
            return self._youtube_fallback(
                track,
                destination,
                extended,
                audio_format,
                audio_quality,
                cancel,
                reason=f"Spotify backend unavailable ({exc}); falling back to YouTube",
            )
        # LibrespotAuthError (not logged in) propagates: the user chose librespot and
        # must log in; a silent YouTube swap would be misleading.

        if session.is_known_free():
            # Only fall back when we POSITIVELY know the account is non-Premium. An
            # inconclusive check (e.g. a 429 on the profile probe) must NOT downgrade a
            # Premium user — we attempt the native stream and let a genuinely free
            # account fail per-track into the YouTube fallback below.
            return self._youtube_fallback(
                track,
                destination,
                extended,
                audio_format,
                audio_quality,
                cancel,
                reason="Spotify account is not Premium (320k OGG needs Premium); "
                "falling back to YouTube",
                error_if_no_fallback=LibrespotNotPremium(
                    "Spotify Premium is required for native 320k OGG"
                ),
            )

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
            elif (
                not search_failed
                and self._allow_fallback
                and getattr(self._scraper, "librespot_extended_yt_fallback", False)
            ):
                # No extended cut on Spotify and the user opted to look on YouTube
                # (which often hosts the 12"/extended mix). Delegate to the YouTube
                # backend's own extended search rather than the original Spotify track.
                return self._youtube_fallback(
                    track,
                    destination,
                    True,  # keep extended mode on for YouTube's extended search
                    audio_format,
                    audio_quality,
                    cancel,
                    reason=f"No extended mix on Spotify for '{track.title}'; "
                    "searching YouTube instead",
                )
            # else: no extended cut found -> stream the original Spotify track
            # natively (used_extended stays False, so the file isn't mislabeled).

        try:
            final_path = audio.fetch_track_ogg(
                session.raw,
                base62,
                dest=dest_ogg,
                cancel=cancel,
                on_status=self._emit,
                on_metadata=self._capture_metadata,
            )
        except OggCaptureError as exc:
            return self._youtube_fallback(
                track,
                destination,
                extended,
                audio_format,
                audio_quality,
                cancel,
                reason=f"No native Spotify OGG for '{track.title}' ({exc}); "
                "falling back to YouTube",
                error_if_no_fallback=exc,
            )
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
        lo, hi = self.INTER_TRACK_JITTER_S
        delay = random.uniform(lo, hi)
        deadline = delay
        step = 0.2
        while deadline > 0:
            if cancel():
                return
            self._sleep(min(step, deadline))
            deadline -= step

    def _youtube_fallback(
        self,
        track,
        destination,
        extended,
        audio_format,
        audio_quality,
        cancel,
        *,
        reason: str,
        error_if_no_fallback: Exception | None = None,
    ):
        """Delegate ONE track to YouTube so an alpha-lib/premium/availability failure
        never bricks the run. If fallback is disabled, raise the underlying error."""
        if not self._allow_fallback:
            raise error_if_no_fallback or OggCaptureError(reason)
        self.used_youtube_fallback = True  # reliable signal for the seam / History
        # This source's format is pinned to its native "ogg"; honoring that here
        # would force a pointless extra lossy generation (YouTube's stream ->
        # Vorbis) that also breaks on ffmpeg builds without libvorbis. Mirror
        # the Real FLAC fallback instead: deliver MP3 320k, labelled as such.
        if audio_format not in ("mp3", "m4a", "opus"):
            audio_format, audio_quality = "mp3", "320"
            reason += " (MP3 320k)"
        self._emit(reason)
        if self._youtube is None:
            self._youtube = YouTubeBackend(self._scraper)
        return self._youtube.fetch(
            track=track,
            destination=destination,
            extended=extended,
            audio_format=audio_format,
            audio_quality=audio_quality,
            cancel=cancel,
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
