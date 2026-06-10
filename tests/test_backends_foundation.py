"""Foundation tests: pluggable AudioBackend factory + shared selector module.

Locks the contract the two follow-on backends (lossless, librespot) plug into:
the make_backend registry, the YouTubeBackend delegation, and the extracted
track_selectors heuristics reproducing the old _select_youtube_match decisions.
"""

import json
from types import SimpleNamespace

import Spotify_Downloader as sd
import track_selectors
from backends import AudioBackend, make_backend
from backends.chain import FallbackChainBackend
from backends.youtube import YouTubeBackend
from Spotify_Downloader import MusicScraper


def _track(title="Song", artists="Artist", duration_ms=200000, tid="x"):
    return SimpleNamespace(title=title, artists=artists, duration_ms=duration_ms, id=tid)


class TestMakeBackend:
    def test_default_backend_is_youtube(self):
        s = MusicScraper()
        assert isinstance(s._backend, YouTubeBackend)
        assert s._backend.max_concurrency == 4

    def test_make_backend_youtube(self):
        b = make_backend("youtube", scraper=MusicScraper())
        assert isinstance(b, YouTubeBackend)

    def test_unknown_source_falls_back_to_youtube(self):
        b = make_backend("does-not-exist", scraper=MusicScraper())
        assert isinstance(b, YouTubeBackend)

    def test_youtube_backend_satisfies_protocol(self):
        assert isinstance(MusicScraper()._backend, AudioBackend)

    def test_lossless_with_fallback_wraps_chain(self):
        b = make_backend("lossless", scraper=MusicScraper(), fallback_order=["youtube"])
        assert isinstance(b, FallbackChainBackend)
        assert b.max_concurrency == 4

    def test_youtube_empty_chain_stays_leaf(self):
        b = make_backend("youtube", scraper=MusicScraper(), fallback_order=[])
        assert isinstance(b, YouTubeBackend)


class TestYouTubeBackendFetch:
    def test_normal_fetch_delegates_with_expected_query(self):
        s = MusicScraper()
        seen = {}

        def fake(query, dest, **kw):
            seen.update(query=query, dest=dest, kw=kw)
            return dest, False

        s.download_track_audio = fake
        path, ext, used = s._backend.fetch(
            track=_track(),
            destination="/tmp/song.mp3",
            extended=False,
            audio_format="mp3",
            audio_quality="192",
            cancel=s.is_cancelled,
        )
        assert (path, ext, used) == ("/tmp/song.mp3", "mp3", False)
        assert seen["query"] == "ytsearch1:Song Artist audio"
        assert seen["kw"]["expected_duration_s"] == 200.0
        assert "fallback_query" not in seen["kw"]

    def test_extended_fetch_strips_radio_edit_and_sets_fallback(self):
        s = MusicScraper(extended_mix=True)
        seen = {}

        def fake(query, dest, **kw):
            seen.update(query=query, kw=kw)
            return dest, True

        s.download_track_audio = fake
        path, ext, used = s._backend.fetch(
            track=_track(title="Song (Radio Edit)", duration_ms=180000),
            destination="/tmp/song.mp3",
            extended=True,
            audio_format="mp3",
            audio_quality="192",
            cancel=s.is_cancelled,
        )
        assert used is True
        # bare "extended", radio-edit stripped, widened to ytsearch10
        assert seen["query"] == "ytsearch10:Song Artist extended"
        assert seen["kw"]["fallback_query"] == "ytsearch1:Song Artist audio"

    def test_actual_ext_comes_from_returned_path(self):
        s = MusicScraper()
        s.download_track_audio = lambda _q, _d, **_kw: ("/tmp/song.flac", False)
        path, ext, used = s._backend.fetch(
            track=_track(),
            destination="/tmp/song.mp3",
            extended=False,
            audio_format="flac",
            audio_quality="192",
            cancel=s.is_cancelled,
        )
        assert path == "/tmp/song.flac"
        assert ext == "flac"  # derived from the real file, not the destination

    def test_actual_ext_opus_from_returned_path(self):
        s = MusicScraper()
        s.download_track_audio = lambda _q, _d, **_kw: ("/tmp/song.opus", False)
        path, ext, used = s._backend.fetch(
            track=_track(),
            destination="/tmp/song.mp3",
            extended=False,
            audio_format="original",
            audio_quality="192",
            cancel=s.is_cancelled,
        )
        assert path == "/tmp/song.opus"
        assert ext == "opus"

    def test_fetch_forwards_audio_format_and_quality_normal(self):
        s = MusicScraper(audio_format="flac")
        seen = {}

        def fake(query, dest, **kw):
            seen.update(kw=kw)
            return dest, False

        s.download_track_audio = fake
        s._backend.fetch(
            track=_track(),
            destination="/tmp/song.mp3",
            extended=False,
            audio_format="mp3",
            audio_quality="320",
            cancel=s.is_cancelled,
        )
        assert seen["kw"]["audio_format"] == "mp3"
        assert seen["kw"]["audio_quality"] == "320"

    def test_fetch_forwards_audio_format_and_quality_extended(self):
        s = MusicScraper(extended_mix=True, audio_format="flac")
        seen = {}

        def fake(query, dest, **kw):
            seen.update(kw=kw)
            return dest, True

        s.download_track_audio = fake
        s._backend.fetch(
            track=_track(title="Song (Radio Edit)"),
            destination="/tmp/song.mp3",
            extended=True,
            audio_format="mp3",
            audio_quality="320",
            cancel=s.is_cancelled,
        )
        assert seen["kw"]["audio_format"] == "mp3"
        assert seen["kw"]["audio_quality"] == "320"


class TestSelectorParity:
    """track_selectors must reproduce the old _select_youtube_match decisions."""

    def test_normal_trusts_top_when_within_tolerance(self):
        cands = [
            {"id": "a", "title": "Song", "duration_s": 201},
            {"id": "b", "title": "Song closer", "duration_s": 200},
        ]
        assert track_selectors.select_normal(cands, 200) == "a"

    def test_normal_repicks_closest_when_top_off(self):
        cands = [
            {"id": "a", "title": "Song (Official Video)", "duration_s": 300},
            {"id": "b", "title": "Song", "duration_s": 198},
        ]
        assert track_selectors.select_normal(cands, 200) == "b"

    def test_normal_none_when_no_candidates(self):
        assert track_selectors.select_normal([], 200) is None

    def test_extended_picks_longest_keyworded_in_window(self):
        cands = [
            {"id": "a", "title": "Song (Extended Mix)", "duration_s": 360},
            {"id": "b", "title": "Song extended", "duration_s": 300},
            {"id": "c", "title": "Song", "duration_s": 240},  # no keyword
        ]
        assert track_selectors.select_extended(cands, 200, 1200) == "a"

    def test_extended_already_extended_fallback_picks_nearest(self):
        cands = [{"id": "a", "title": "Song (Extended Mix)", "duration_s": 205}]
        # 205 is not > expected+7=207, so the longer-window is empty; the
        # already-extended window [expected/2.5, upper] catches it.
        assert track_selectors.select_extended(cands, 200, 1200) == "a"

    def test_extended_none_without_keyword(self):
        cands = [{"id": "a", "title": "Song", "duration_s": 360}]
        assert track_selectors.select_extended(cands, 200, 1200) is None

    def test_extended_no_expected_takes_first_sane_keyworded(self):
        cands = [
            {"id": "a", "title": "Song (Extended Mix)", "duration_s": 99999},  # too long
            {"id": "b", "title": "Song extended", "duration_s": 300},
        ]
        assert track_selectors.select_extended(cands, None, 1200) == "b"

    def test_title_matches_same_song_through_qualifiers(self):
        # Accent-folded, qualifier-stripped token overlap accepts the same song.
        assert track_selectors.title_matches("More - Zerb Remix", "More (Zerb Extended Remix)")
        assert track_selectors.title_matches(
            "Canopée des Cîmes", "Canopee des Cimes (Extended Mix)"
        )
        # An empty source can't be judged, so it accepts (preserves old behavior).
        assert track_selectors.title_matches("", "Anything (Extended Mix)")

    def test_title_matches_rejects_different_song(self):
        # The real bug: a "Teardrop"/"Maybe Not" search drifting to another track.
        assert not track_selectors.title_matches(
            "Teardrop", "Maybe Not (Rodriguez Jr. Extended Remix)"
        )
        assert not track_selectors.title_matches(
            "Maybe Not", "Our Broken Mind Embassy (Extended Mix)"
        )

    def test_strict_title_rejects_wrong_song_extended_cut(self):
        # Without the guard the longer keyworded cut wins; with strict_title it is
        # discarded as a different song, so the selector falls back (None).
        cands = [{"id": "x", "title": "Our Broken Mind Embassy (Extended Mix)", "duration_s": 470}]
        assert track_selectors.select_extended(cands, 200, 1200) == "x"
        assert (
            track_selectors.select_extended(
                cands, 200, 1200, source_title="Maybe Not", strict_title=True
            )
            is None
        )

    def test_strict_title_keeps_matching_extended_cut(self):
        cands = [{"id": "x", "title": "Maybe Not (Extended Mix)", "duration_s": 470}]
        assert (
            track_selectors.select_extended(
                cands, 200, 1200, source_title="Maybe Not", strict_title=True
            )
            == "x"
        )

    def test_strip_radio_edit_leaves_unrelated_words(self):
        assert track_selectors.strip_radio_edit("Radio Ga Ga") == "Radio Ga Ga"
        assert track_selectors.strip_radio_edit("Song (Radio Edit)") == "Song"


class TestConfigDownloadSource:
    def test_default_download_source(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sd, "_config_path", lambda: str(tmp_path / "config.json"))
        assert sd.load_config()["download_source"] == "youtube"

    def test_unknown_download_source_resets(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"download_source": "bogus"}))
        monkeypatch.setattr(sd, "_config_path", lambda: str(cfg))
        assert sd.load_config()["download_source"] == "youtube"


class TestSourceFormats:
    """Per-source format policy: every source only offers what it can honestly
    deliver (no fake-lossless from YouTube; lossless/librespot pin their native
    container)."""

    def _load(self, tmp_path, monkeypatch, data):
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps(data))
        monkeypatch.setattr(sd, "_config_path", lambda: str(cfg))
        return sd.load_config()

    def test_youtube_offers_only_lossy_formats(self):
        """YouTube offers only lossy transcode targets plus the no-transcode passthrough."""
        for fmt in sd.SOURCE_FORMATS["youtube"]:
            info = sd.SUPPORTED_FORMATS[fmt]
            if fmt == "original":
                assert info.get("passthrough") and not info["lossy"]
            else:
                assert info["lossy"]

    def test_config_flac_plus_youtube_resets_to_mp3(self, tmp_path, monkeypatch):
        loaded = self._load(tmp_path, monkeypatch, {"format": "flac"})
        assert loaded["format"] == "mp3"

    def test_config_lossless_source_pins_flac(self, tmp_path, monkeypatch):
        loaded = self._load(tmp_path, monkeypatch, {"download_source": "lossless", "format": "mp3"})
        assert loaded["format"] == "flac"

    def test_config_librespot_source_pins_ogg(self, tmp_path, monkeypatch):
        loaded = self._load(
            tmp_path, monkeypatch, {"download_source": "librespot", "format": "mp3"}
        )
        assert loaded["format"] == "ogg"

    def test_scraper_coerces_format_to_source(self):
        # flac + YouTube would transcode a lossy stream into a lossless
        # container; the scraper falls back to the source's default instead.
        assert sd.MusicScraper(audio_format="flac").audio_format == "mp3"
        assert (
            sd.MusicScraper(download_source="lossless", audio_format="mp3").audio_format == "flac"
        )
        assert (
            sd.MusicScraper(download_source="librespot", audio_format="mp3").audio_format == "ogg"
        )

    def test_scraper_keeps_format_the_source_offers(self):
        assert sd.MusicScraper(audio_format="m4a").audio_format == "m4a"
