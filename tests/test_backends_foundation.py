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
