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
KNOWN_DOWNLOAD_SOURCES = ("youtube", "lossless", "librespot")
DEFAULT_DOWNLOAD_SOURCE = "youtube"

ALLOWED_FALLBACKS = {
    "youtube": (),
    "librespot": ("youtube",),
    "lossless": ("librespot", "youtube"),
}
DEFAULT_FALLBACK_ORDER = {
    "youtube": (),
    "librespot": ("youtube",),
    "lossless": ("youtube",),
}


def validate_fallback_order(source: str, order) -> tuple[str, ...]:
    """Drop unknown, duplicate, and disallowed entries; non-list → default."""
    allowed = ALLOWED_FALLBACKS.get(source, ())
    default = DEFAULT_FALLBACK_ORDER.get(source, ())
    if not isinstance(order, (list, tuple)):
        return default
    seen: set[str] = set()
    out: list[str] = []
    for item in order:
        if not isinstance(item, str):
            continue
        if item not in allowed or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return tuple(out)


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
        has_fallback: bool = False,
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


def _make_leaf(download_source: str, *, scraper):
    """Return a single leaf backend (no chain wrapper)."""
    from .youtube import YouTubeBackend

    if download_source == "librespot":
        try:
            from .librespot.backend import LibrespotBackend
        except ImportError as exc:  # pragma: no cover - defensive
            print(f"[*] librespot backend unavailable ({exc}); falling back to YouTube")
            return YouTubeBackend(scraper)
        return LibrespotBackend(scraper)
    if download_source == "youtube":
        return YouTubeBackend(scraper)
    if download_source == "lossless":
        from .real_flac import RealFlacBackend

        return RealFlacBackend(scraper)
    return YouTubeBackend(scraper)


def make_backend(
    download_source: str, *, scraper, fallback_order: tuple[str, ...] | list[str] = ()
) -> AudioBackend:
    """Return the AudioBackend for *download_source*. Unknown values fall back to
    YouTube so a stale/garbage config can never leave the app without a backend.
    Imports are lazy so optional/heavy backend deps load only when selected.
    Wraps in :class:`~backends.chain.FallbackChainBackend` only when the chain
    is non-empty.
    """
    order = validate_fallback_order(download_source, fallback_order)
    if not order:
        return _make_leaf(download_source, scraper=scraper)

    from .chain import FallbackChainBackend

    steps: list[tuple[str, object]] = [
        (download_source, _make_leaf(download_source, scraper=scraper))
    ]
    for source_name in order:
        if source_name == "librespot":
            try:
                from .librespot.backend import LibrespotBackend
            except ImportError as exc:
                print(f"[*] librespot fallback step unavailable ({exc}); skipping")
                continue
            steps.append((source_name, LibrespotBackend(scraper)))
        elif source_name == "youtube":
            from .youtube import YouTubeBackend

            steps.append((source_name, YouTubeBackend(scraper)))
    if len(steps) == 1:
        return steps[0][1]
    return FallbackChainBackend(download_source, steps, scraper=scraper)
