"""Tests for FallbackChainBackend and make_backend chain wrapping."""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from backends import make_backend, validate_fallback_order
from backends.chain import FallbackChainBackend
from backends.librespot.errors import (
    LibrespotAuthError,
    LibrespotCancelled,
    LibrespotNotPremium,
    LibrespotUnavailable,
    NoExtendedCutError,
    OggCaptureError,
)
from backends.youtube import YouTubeBackend
from lossless.errors import NotFoundOnServiceError
from Spotify_Downloader import MusicScraper


def _track(**kw):
    base = {"title": "Song", "artists": "Artist", "duration_ms": 200000, "id": "tid"}
    base.update(kw)
    return SimpleNamespace(**base)


def _no_cancel():
    return False


class FakeLeaf:
    max_concurrency = 4
    calls: list[dict] = []
    closed: list[str] = []
    exc: Exception | None = None
    result = ("/tmp/out.mp3", "mp3", False)
    last_track_metadata = None
    raw_session = None
    _isrc = None

    def __init__(self, name: str):
        self.name = name
        self._entered = threading.Event()
        self._release = threading.Event()

    def fetch(self, **kw):
        FakeLeaf.calls.append({"name": self.name, **kw})
        if self.exc:
            raise self.exc
        if hasattr(self, "_gate_hold"):
            self._entered.set()
            self._release.wait(timeout=2)
        return self.result

    def close(self):
        FakeLeaf.closed.append(self.name)

    def provider_metadata_for(self, track_id):
        if self.name == "lossless":
            return {"source": "qobuz", "meta": {"album": "A"}}
        return None


def _chain(primary, steps, scraper=None):
    scraper = scraper or SimpleNamespace(error_signal=SimpleNamespace(emit=lambda *_a: None))
    pairs = [(primary, steps[0])] + [(s.name, s) for s in steps[1:]]
    return FallbackChainBackend(primary, pairs, scraper=scraper)


class TestMakeBackendChain:
    def test_empty_chain_returns_leaf_youtube(self):
        b = make_backend("youtube", scraper=MusicScraper(), fallback_order=())
        assert isinstance(b, YouTubeBackend)

    def test_nonempty_chain_wraps(self):
        b = make_backend("lossless", scraper=MusicScraper(), fallback_order=["youtube"])
        assert isinstance(b, FallbackChainBackend)

    def test_librespot_primary_with_youtube_chain(self):
        b = make_backend("librespot", scraper=MusicScraper(), fallback_order=["youtube"])
        assert isinstance(b, FallbackChainBackend)


class TestAdvanceAbort:
    def setup_method(self):
        FakeLeaf.calls = []
        FakeLeaf.closed = []

    def test_lossless_exhaustion_advances_to_youtube(self):
        lossless = FakeLeaf("lossless")
        lossless.exc = NotFoundOnServiceError("no lossless source found")
        yt = FakeLeaf("youtube")
        yt.result = ("/yt.mp3", "mp3", False)
        chain = _chain("lossless", [lossless, yt])
        path, ext, used = chain.fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=False,
            audio_format="flac",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert path == "/yt.mp3"
        assert len(FakeLeaf.calls) == 2
        assert FakeLeaf.calls[1]["audio_format"] == "mp3"
        assert FakeLeaf.calls[1]["audio_quality"] == "320"

    def test_librespot_not_premium_advances(self):
        lib = FakeLeaf("librespot")
        lib.max_concurrency = 1
        lib.exc = LibrespotNotPremium("premium")
        yt = FakeLeaf("youtube")
        chain = _chain("librespot", [lib, yt])
        chain.fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=False,
            audio_format="ogg",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert FakeLeaf.calls[1]["audio_format"] == "mp3"
        assert FakeLeaf.calls[1]["audio_quality"] == "320"

    def test_librespot_unavailable_advances(self):
        lib = FakeLeaf("librespot")
        lib.exc = LibrespotUnavailable("missing")
        yt = FakeLeaf("youtube")
        _chain("librespot", [lib, yt]).fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=False,
            audio_format="ogg",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert len(FakeLeaf.calls) == 2

    def test_ogg_capture_advances(self):
        lib = FakeLeaf("librespot")
        lib.exc = OggCaptureError("no ogg")
        yt = FakeLeaf("youtube")
        _chain("librespot", [lib, yt]).fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=False,
            audio_format="ogg",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert len(FakeLeaf.calls) == 2

    def test_no_extended_cut_advances_with_extended_true(self):
        lib = FakeLeaf("librespot")
        lib.exc = NoExtendedCutError("no extended")
        yt = FakeLeaf("youtube")
        _chain("librespot", [lib, yt]).fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=True,
            audio_format="ogg",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert FakeLeaf.calls[1]["extended"] is True

    def test_auth_error_aborts(self):
        lib = FakeLeaf("librespot")
        lib.exc = LibrespotAuthError("not logged in")
        yt = FakeLeaf("youtube")
        with pytest.raises(LibrespotAuthError):
            _chain("librespot", [lib, yt]).fetch(
                track=_track(),
                destination="/tmp/s.mp3",
                extended=False,
                audio_format="ogg",
                audio_quality="192",
                cancel=_no_cancel,
            )
        assert len(FakeLeaf.calls) == 1

    def test_librespot_cancelled_aborts(self):
        lib = FakeLeaf("librespot")
        lib.exc = LibrespotCancelled("cancelled")
        yt = FakeLeaf("youtube")
        with pytest.raises(LibrespotCancelled):
            _chain("librespot", [lib, yt]).fetch(
                track=_track(),
                destination="/tmp/s.mp3",
                extended=False,
                audio_format="ogg",
                audio_quality="192",
                cancel=_no_cancel,
            )
        assert len(FakeLeaf.calls) == 1

    def test_cancel_aborts_mid_chain(self):
        lossless = FakeLeaf("lossless")
        lossless.exc = NotFoundOnServiceError("no lossless source found")
        yt = FakeLeaf("youtube")
        state = {"cancel": False}

        def cancel():
            return state["cancel"]

        def lossless_fetch(**kw):
            state["cancel"] = True
            raise NotFoundOnServiceError("no lossless source found")

        lossless.fetch = lossless_fetch
        chain = _chain("lossless", [lossless, yt])
        with pytest.raises(NotFoundOnServiceError):
            chain.fetch(
                track=_track(),
                destination="/tmp/s.mp3",
                extended=False,
                audio_format="flac",
                audio_quality="192",
                cancel=cancel,
            )
        assert len(FakeLeaf.calls) == 0  # youtube never reached

    def test_exhaustion_reraises_last(self):
        lossless = FakeLeaf("lossless")
        lossless.exc = NotFoundOnServiceError("no lossless source found")
        yt = FakeLeaf("youtube")
        yt.exc = RuntimeError("yt failed")
        with pytest.raises(RuntimeError):
            _chain("lossless", [lossless, yt]).fetch(
                track=_track(),
                destination="/tmp/s.mp3",
                extended=False,
                audio_format="flac",
                audio_quality="192",
                cancel=_no_cancel,
            )

    def test_exhaustion_empty_chain_reraises(self):
        lossless = FakeLeaf("lossless")
        lossless.exc = NotFoundOnServiceError("no lossless source found")
        with pytest.raises(NotFoundOnServiceError):
            _chain("lossless", [lossless]).fetch(
                track=_track(),
                destination="/tmp/s.mp3",
                extended=False,
                audio_format="flac",
                audio_quality="192",
                cancel=_no_cancel,
            )


class TestHasFallbackHint:
    def setup_method(self):
        FakeLeaf.calls = []
        FakeLeaf.closed = []

    def test_primary_gets_has_fallback_when_more_steps(self):
        a = FakeLeaf("lossless")
        a.exc = NotFoundOnServiceError("no lossless source found")
        b = FakeLeaf("youtube")
        _chain("lossless", [a, b]).fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=False,
            audio_format="flac",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert FakeLeaf.calls[0]["has_fallback"] is True
        assert FakeLeaf.calls[1]["has_fallback"] is False

    def test_single_step_has_fallback_false(self):
        a = FakeLeaf("youtube")
        _chain("youtube", [a]).fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert FakeLeaf.calls[0]["has_fallback"] is False


class TestProvenanceAndForwarding:
    def setup_method(self):
        FakeLeaf.calls = []
        FakeLeaf.closed = []

    def test_served_by_thread_local(self):
        lossless = FakeLeaf("lossless")
        lossless.exc = NotFoundOnServiceError("no lossless source found")
        yt = FakeLeaf("youtube")
        chain = _chain("lossless", [lossless, yt])
        chain.fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=False,
            audio_format="flac",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert chain.served_by == "youtube"

    def test_last_track_metadata_snapshot(self):
        lib = FakeLeaf("librespot")
        lib.last_track_metadata = {"album": "X"}
        lib.result = ("/tmp/x.ogg", "ogg", False)
        chain = _chain("librespot", [lib])
        chain.fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=False,
            audio_format="ogg",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert chain.last_track_metadata == {"album": "X"}

    def test_max_concurrency_from_primary(self):
        lossless = FakeLeaf("lossless")
        lossless.max_concurrency = 4
        lib = FakeLeaf("librespot")
        lib.max_concurrency = 1
        chain = _chain("lossless", [lossless, lib])
        assert chain.max_concurrency == 4

    def test_provider_metadata_for_forwarding(self):
        lossless = FakeLeaf("lossless")
        chain = _chain("lossless", [lossless])
        assert chain.provider_metadata_for("tid") == {"source": "qobuz", "meta": {"album": "A"}}

    def test_raw_session_forwarding(self):
        lib = FakeLeaf("librespot")
        lib.raw_session = object()
        chain = _chain("librespot", [lib])
        assert chain.raw_session is lib.raw_session

    def test_isrc_forwarding(self):
        lossless = FakeLeaf("lossless")
        isrc = object()
        lossless._isrc = isrc
        chain = _chain("lossless", [lossless])
        assert chain._isrc is isrc

    def test_close_forwarding(self):
        lossless = FakeLeaf("lossless")
        lib = FakeLeaf("librespot")
        yt = FakeLeaf("youtube")
        chain = _chain("lossless", [lossless, lib, yt])
        chain.close()
        assert FakeLeaf.closed == ["lossless", "librespot", "youtube"]

    def test_librespot_gate_serializes(self):
        lib = FakeLeaf("librespot")
        lib.max_concurrency = 1
        lib._gate_hold = True
        lib.result = ("/tmp/x.ogg", "ogg", False)
        chain = _chain("librespot", [lib])

        def run():
            chain.fetch(
                track=_track(id="a"),
                destination="/tmp/a.mp3",
                extended=False,
                audio_format="ogg",
                audio_quality="192",
                cancel=_no_cancel,
            )

        t1 = threading.Thread(target=run)
        t1.start()
        lib._entered.wait(timeout=2)
        t2 = threading.Thread(target=run)
        t2.start()
        threading.Event().wait(0.05)
        assert len([c for c in FakeLeaf.calls if c["name"] == "librespot"]) == 1
        lib._release.set()
        t1.join(timeout=2)
        lib._release.set()
        t2.join(timeout=2)


class TestWarningEmission:
    def setup_method(self):
        FakeLeaf.calls = []
        FakeLeaf.closed = []

    def test_final_step_advance_emits_no_warning(self):
        emitted = []
        scraper = SimpleNamespace(error_signal=SimpleNamespace(emit=emitted.append))
        lossless = FakeLeaf("lossless")
        lossless.exc = NotFoundOnServiceError("no lossless source found")
        with pytest.raises(NotFoundOnServiceError):
            _chain("lossless", [lossless], scraper=scraper).fetch(
                track=_track(),
                destination="/tmp/s.mp3",
                extended=False,
                audio_format="flac",
                audio_quality="192",
                cancel=_no_cancel,
            )
        assert emitted == []

    def test_lossless_to_youtube_message(self):
        emitted = []
        scraper = SimpleNamespace(error_signal=SimpleNamespace(emit=emitted.append))
        lossless = FakeLeaf("lossless")
        lossless.exc = NotFoundOnServiceError("no lossless source found")
        yt = FakeLeaf("youtube")
        _chain("lossless", [lossless, yt], scraper=scraper).fetch(
            track=_track(),
            destination="/tmp/s.mp3",
            extended=False,
            audio_format="flac",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert any("MP3 320k" in m for m in emitted)

    def test_no_extended_cut_message(self):
        emitted = []
        scraper = SimpleNamespace(error_signal=SimpleNamespace(emit=emitted.append))
        lib = FakeLeaf("librespot")
        lib.exc = NoExtendedCutError("no extended")
        yt = FakeLeaf("youtube")
        _chain("librespot", [lib, yt], scraper=scraper).fetch(
            track=_track(title="My Song"),
            destination="/tmp/s.mp3",
            extended=True,
            audio_format="ogg",
            audio_quality="192",
            cancel=_no_cancel,
        )
        assert any("No extended mix on Spotify" in m for m in emitted)


class TestValidateFallbackOrder:
    def test_drops_unknown_and_dupes(self):
        assert validate_fallback_order("lossless", ["youtube", "bogus", "youtube"]) == ("youtube",)

    def test_non_list_uses_default(self):
        assert validate_fallback_order("lossless", "nope") == ("youtube",)
