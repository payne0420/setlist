"""User-configurable fallback chain: ordered leaf backends with classified advancement."""

from __future__ import annotations

import contextlib
import threading
from typing import Callable

from lossless.errors import LosslessError

from .librespot.errors import (
    LibrespotAuthError,
    LibrespotCancelled,
    LibrespotNotPremium,
    LibrespotUnavailable,
    NoExtendedCutError,
    OggCaptureError,
)

_ADVANCE = (
    LosslessError,
    LibrespotUnavailable,
    LibrespotNotPremium,
    OggCaptureError,
    NoExtendedCutError,
)


class FallbackChainBackend:
    """Wrap ordered leaf backends; advance on unavailable-class exceptions."""

    def __init__(self, primary_source: str, steps: list[tuple[str, object]], *, scraper):
        self._primary_source = primary_source
        self._steps = steps
        self._scraper = scraper
        self._tls = threading.local()
        self._gates = [
            threading.BoundedSemaphore(getattr(leaf, "max_concurrency", 4)) for _, leaf in steps
        ]

    @property
    def max_concurrency(self) -> int:
        return self._steps[0][1].max_concurrency

    @property
    def served_by(self) -> str | None:
        return getattr(self._tls, "served_by", None)

    @property
    def last_track_metadata(self) -> dict | None:
        return getattr(self._tls, "last_track_metadata", None)

    @property
    def _isrc(self):
        for _, leaf in self._steps:
            isrc = getattr(leaf, "_isrc", None)
            if isrc is not None:
                return isrc
        return None

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
        del has_fallback  # only meaningful on leaf backends
        last_exc: Exception | None = None
        n = len(self._steps)
        for i, (source_name, leaf) in enumerate(self._steps):
            if cancel():
                if last_exc is not None:
                    raise last_exc
                raise LibrespotCancelled("cancelled before fetch")
            step_fmt, step_q = audio_format, audio_quality
            if i > 0:
                if source_name == "youtube":
                    step_fmt = "original"
                elif source_name == "librespot":
                    step_fmt, step_q = "ogg", "320"
            step_has_fallback = i < n - 1
            gate = self._gates[i]
            gate.acquire()
            try:
                result = leaf.fetch(
                    track=track,
                    destination=destination,
                    extended=extended,
                    audio_format=step_fmt,
                    audio_quality=step_q,
                    cancel=cancel,
                    has_fallback=step_has_fallback,
                )
                self._tls.served_by = source_name
                self._tls.last_track_metadata = getattr(leaf, "last_track_metadata", None)
                return result
            except LibrespotCancelled:
                raise
            except LibrespotAuthError:
                raise
            except _ADVANCE as exc:
                if cancel():
                    raise exc
                last_exc = exc
                if i < n - 1:
                    self._emit_advance(source_name, exc, track)
                continue
            except Exception:
                raise
            finally:
                gate.release()
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("fallback chain exhausted without exception")

    def provider_metadata_for(self, track_id):
        for _, leaf in self._steps:
            fn = getattr(leaf, "provider_metadata_for", None)
            if callable(fn):
                meta = fn(track_id)
                if meta is not None:
                    return meta
        return None

    @property
    def raw_session(self):
        for _, leaf in self._steps:
            raw = getattr(leaf, "raw_session", None)
            if raw is not None:
                return raw
        return None

    def close(self) -> None:
        for _, leaf in self._steps:
            close = getattr(leaf, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    close()

    def _emit_advance(self, source_name: str, exc: Exception, track) -> None:
        title = getattr(track, "title", "") or ""
        if isinstance(exc, NoExtendedCutError):
            msg = f"No extended mix on Spotify for '{title}'; searching YouTube instead"
        elif isinstance(exc, LibrespotUnavailable):
            msg = f"Spotify backend unavailable ({exc}); falling back to YouTube"
        elif isinstance(exc, LibrespotNotPremium):
            msg = "Spotify account is not Premium (320k OGG needs Premium); falling back to YouTube"
        elif isinstance(exc, OggCaptureError):
            msg = f"No native Spotify OGG for '{title}' ({exc}); falling back to YouTube"
        elif isinstance(exc, LosslessError):
            if source_name == "lossless" and self._primary_source == "lossless":
                next_source = self._next_source_after(source_name)
                if next_source == "librespot":
                    msg = f"Lossless source unavailable for '{title}' — trying Spotify (320k OGG)"
                else:
                    msg = (
                        "Lossless source unavailable — fell back to YouTube "
                        "(lossy source quality, not lossless)"
                    )
            else:
                msg = (
                    "Lossless source unavailable — fell back to YouTube "
                    "(lossy source quality, not lossless)"
                )
        else:
            msg = f"{exc}; falling back to YouTube"
        self._emit(msg)

    def _next_source_after(self, source_name: str) -> str | None:
        seen = False
        for name, _ in self._steps:
            if seen:
                return name
            if name == source_name:
                seen = True
        return None

    def _emit(self, message: str) -> None:
        print(f"[*] fallback: {message}")
        sig = getattr(self._scraper, "error_signal", None)
        if sig is not None:
            with contextlib.suppress(Exception):
                sig.emit(message)
