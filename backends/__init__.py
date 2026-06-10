"""Pluggable audio-download backends.

The single audio-fetch seam in MusicScraper routes through a backend selected by
the ``download_source`` setting. YouTube is the default. Follow-on backends — a
lossless Qobuz/Tidal/Amazon backend and a librespot native-OGG backend —
register themselves by adding one branch to :func:`make_backend` (and one value
to :data:`KNOWN_DOWNLOAD_SOURCES`).
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

# download_source values understood by make_backend. Follow-on goals append
# their own ("lossless", "librespot"); load_config uses this as its whitelist so
# new sources are accepted automatically once registered.
KNOWN_DOWNLOAD_SOURCES = ("youtube", "lossless")
DEFAULT_DOWNLOAD_SOURCE = "youtube"


@runtime_checkable
class AudioBackend(Protocol):
    """Resolve + download one track. Implementations live under ``backends/``."""

    # Safe number of concurrent per-track fetches this backend supports. The
    # playlist worker pool is clamped to this (YouTube = 4; a single Spotify
    # session backend would declare 1).
    max_concurrency: int

    def fetch(
        self,
        *,
        track,
        destination: str,
        extended: bool,
        audio_format: str,
        audio_quality: str,
        cancel: Callable[[], bool],
    ) -> tuple[str, str, bool]:
        """Download one track to (near) *destination*.

        Returns ``(final_path, actual_ext, used_extended)``:
          * ``final_path``    — the audio file actually written on disk.
          * ``actual_ext``    — its real extension ('mp3'|'m4a'|'flac'|'ogg'|...),
            so callers never assume ``SUPPORTED_FORMATS[fmt]['ext']``.
          * ``used_extended`` — True iff an extended cut was produced (drives
            filename/tag marking via _resolve_extended_output / _meta_title).
        """
        ...


def make_backend(download_source: str, *, scraper) -> AudioBackend:
    """Return the AudioBackend for *download_source*. Unknown values fall back to
    YouTube so a stale/garbage config can never leave the app without a backend.
    Imports are lazy so optional/heavy backend deps load only when selected.
    """
    from .youtube import YouTubeBackend

    if download_source == "youtube":
        return YouTubeBackend(scraper)
    if download_source == "lossless":
        from .real_flac import RealFlacBackend

        return RealFlacBackend(scraper)
    # Follow-on goals register here, e.g.:
    #   if download_source == "librespot":
    #       from .librespot.backend import LibrespotBackend
    #       return LibrespotBackend(scraper)
    return YouTubeBackend(scraper)
