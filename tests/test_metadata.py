"""Tests for rich-metadata enrichment of Real-FLAC downloads.

Covers: the four source extractors (Spotify spclient / Qobuz / Deezer / Tidal),
the SpotifyIsrcResolver.resolve_metadata cache + best-effort/thread-safety, the
backend provider-metadata store, the seam's tiered self-consistent merge
(provider <-> spotify <-> embed), and the metadata writers' non-destructive
album guard + discnumber. No test hits the live network.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from lossless import metadata as M

# --------------------------------------------------------------------------- #
# Source payloads (shapes verified live; see lossless/metadata.py docstrings)
# --------------------------------------------------------------------------- #
SPOTIFY_JSON = {
    "name": "Never Gonna Give You Up",
    "number": 1,
    "disc_number": 1,
    "artist": [{"name": "Rick Astley"}],
    "album": {
        "name": "Whenever You Need Somebody",
        "artist": [{"name": "Rick Astley"}],
        "date": {"year": 1987, "month": 11, "day": 12},
        "cover_group": {
            "image": [
                {"size": "DEFAULT", "file_id": "ab67616d00001e02"},
                {"size": "SMALL", "file_id": "ab67616d00004851"},
                {"size": "LARGE", "file_id": "ab67616d0000b273"},
            ]
        },
    },
    "external_id": [{"type": "isrc", "id": "GBARL9300135"}],
}

QOBUZ_DICT = {
    "id": 42,
    "title": "Alone",
    "version": "Extended Mix",
    "performer": {"name": "Jan Blomqvist"},
    "track_number": 3,
    "media_number": 1,
    "release_date_original": "2023-03-24",
    "album": {
        "title": "Disconnected",
        "artist": {"name": "Jan Blomqvist"},
        "image": {"large": "https://qobuz/large.jpg", "small": "https://qobuz/small.jpg"},
    },
}

DEEZER_JSON = {
    "title": "Alone",
    "artist": {"name": "Jan Blomqvist"},
    "contributors": [{"name": "Jan Blomqvist"}, {"name": "Malou"}],
    "track_position": 5,
    "disk_number": 2,
    "release_date": "2023-03-24",
    "album": {"title": "Love Songs", "cover_xl": "https://dz/xl.jpg"},
}

TIDAL_INFO = {
    "version": "2.10",
    "data": {
        "title": "Alone",
        "artists": [{"name": "Jan Blomqvist"}, {"name": "MaLou"}],
        "trackNumber": 1,
        "volumeNumber": 1,
        "streamStartDate": "2023-03-24T00:00:00.000+0000",
        "album": {"title": "Alone", "cover": "65c9137e-a685-4088-8147-ccf10b9104fe"},
    },
}


class TestSpotifyExtractor:
    def test_full_extraction(self):
        m = M.extract_spotify_metadata(SPOTIFY_JSON)
        assert m["title"] == "Never Gonna Give You Up"
        assert m["artists"] == "Rick Astley"
        assert m["album"] == "Whenever You Need Somebody"
        assert m["albumArtist"] == "Rick Astley"
        assert m["trackNumber"] == 1
        assert m["discNumber"] == 1
        assert m["releaseDate"] == "1987-11-12"
        # LARGE wins the cover selection (highest size rank).
        assert m["cover"] == "https://i.scdn.co/image/ab67616d0000b273"

    def test_partial_degrades(self):
        assert M.extract_spotify_metadata({"name": "X"}) == {"title": "X"}
        assert M.extract_spotify_metadata({}) is None
        assert M.extract_spotify_metadata("nope") is None

    def test_date_year_only(self):
        m = M.extract_spotify_metadata(
            {"name": "x", "album": {"name": "a", "date": {"year": 1999}}}
        )
        assert m["releaseDate"] == "1999"


class TestQobuzExtractor:
    def test_full(self):
        m = M.extract_qobuz_metadata(QOBUZ_DICT)
        assert m["title"] == "Alone (Extended Mix)"  # version folded in
        assert m["artists"] == "Jan Blomqvist"
        assert m["album"] == "Disconnected"
        assert m["albumArtist"] == "Jan Blomqvist"
        assert m["trackNumber"] == 3
        assert m["discNumber"] == 1
        assert m["releaseDate"] == "2023-03-24"
        assert m["cover"] == "https://qobuz/large.jpg"

    def test_no_double_version(self):
        d = {**QOBUZ_DICT, "title": "Alone (Extended Mix)", "version": "Extended Mix"}
        assert M.extract_qobuz_metadata(d)["title"] == "Alone (Extended Mix)"


class TestDeezerExtractor:
    def test_full(self):
        m = M.extract_deezer_metadata(DEEZER_JSON)
        assert m["title"] == "Alone"
        assert m["artists"] == "Jan Blomqvist, Malou"  # contributors joined
        assert m["album"] == "Love Songs"
        assert m["trackNumber"] == 5
        assert m["discNumber"] == 2
        assert m["cover"] == "https://dz/xl.jpg"


class TestTidalExtractor:
    def test_unwraps_envelope_and_builds_cover(self):
        m = M.extract_tidal_metadata(TIDAL_INFO)
        assert m["title"] == "Alone"
        assert m["artists"] == "Jan Blomqvist, MaLou"
        assert m["album"] == "Alone"
        assert m["trackNumber"] == 1
        assert m["discNumber"] == 1
        assert m["releaseDate"] == "2023-03-24"  # date part of streamStartDate
        # uuid dashes -> path slashes, 1280x1280
        assert m["cover"] == (
            "https://resources.tidal.com/images/65c9137e/a685/4088/8147/ccf10b9104fe/1280x1280.jpg"
        )

    def test_nbsp_cleaned(self):
        d = {"data": {"title": "x", "artists": [{"name": "A\xa0B"}], "album": {"title": "al"}}}
        assert M.extract_tidal_metadata(d)["artists"] == "A B"


# --------------------------------------------------------------------------- #
# SpotifyIsrcResolver.resolve_metadata — cache, best-effort, thread-safety
# --------------------------------------------------------------------------- #
class TestResolveMetadata:
    def _resolver(self, json_data, status=200):
        from lossless.spotify_isrc import SpotifyIsrcResolver

        r = SpotifyIsrcResolver(session=MagicMock())
        r._get_token = MagicMock(return_value="tok")
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = json_data
        resp.text = "{}"
        r._session.get.return_value = resp
        return r

    def test_resolves_and_caches(self):
        r = self._resolver(SPOTIFY_JSON)
        m = r.resolve_metadata("4PTG3Z6ehGkBFwjybzWkR8")
        assert m["album"] == "Whenever You Need Somebody"
        m2 = r.resolve_metadata("4PTG3Z6ehGkBFwjybzWkR8")
        assert m2 is m or m2 == m
        # cached: only one network round-trip for repeated calls
        assert r._session.get.call_count == 1

    def test_shares_fetch_with_isrc_path(self):
        r = self._resolver(SPOTIFY_JSON)
        assert r.resolve("4PTG3Z6ehGkBFwjybzWkR8") == "GBARL9300135"
        # metadata reuses the cached JSON -> still a single fetch
        assert r.resolve_metadata("4PTG3Z6ehGkBFwjybzWkR8")["trackNumber"] == 1
        assert r._session.get.call_count == 1

    def test_best_effort_on_http_error(self):
        r = self._resolver(SPOTIFY_JSON, status=429)
        assert r.resolve_metadata("4PTG3Z6ehGkBFwjybzWkR8") is None  # never raises

    def test_best_effort_on_bad_id(self):
        r = self._resolver(SPOTIFY_JSON)
        assert r.resolve_metadata("") is None
        assert r.resolve_metadata("not a real id !!") is None

    def test_thread_safe_concurrent_resolves(self):
        r = self._resolver(SPOTIFY_JSON)
        out = []
        ids = ["4PTG3Z6ehGkBFwjybzWkR8", "4cOdK2wGLETKBW3PvgPWqT"]

        def worker(tid):
            for _ in range(20):
                out.append(r.resolve_metadata(tid))

        threads = [threading.Thread(target=worker, args=(i,)) for i in ids for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert all(o and o["album"] == "Whenever You Need Somebody" for o in out)


# --------------------------------------------------------------------------- #
# Backend provider-metadata store (thread-safe)
# --------------------------------------------------------------------------- #
class TestProviderStore:
    def _backend(self):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(download_source="lossless")._backend

    def test_record_and_read(self):
        b = self._backend()
        b._record_provider_meta("abc", "tidal", {"album": "Alone"})
        rec = b.provider_metadata_for("abc")
        assert rec == {"source": "tidal", "meta": {"album": "Alone"}}
        assert b.provider_metadata_for("missing") is None

    def test_youtube_marks_degraded(self):
        b = self._backend()
        b._record_provider_meta("abc", "youtube", None)
        assert b.provider_metadata_for("abc") == {"source": "youtube", "meta": {}}

    def test_concurrent_records(self):
        b = self._backend()

        def worker(n):
            for i in range(50):
                b._record_provider_meta(f"id{n}", "qobuz", {"album": f"a{i}"})

        ts = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        for n in range(8):
            assert b.provider_metadata_for(f"id{n}")["source"] == "qobuz"


# --------------------------------------------------------------------------- #
# Seam: tiered, self-consistent enrichment
# --------------------------------------------------------------------------- #
class TestEnrichSongMeta:
    def _scraper(self, source="provider"):
        from Spotify_Downloader import MusicScraper

        s = MusicScraper(download_source="lossless", flac_metadata_source=source)
        return s

    def _meta(self):
        return {
            "title": "Alone",
            "artists": "Jan Blomqvist,\xa0MaLou",  # nbsp from the embed
            "album": "",
            "releaseDate": "",
            "cover": "https://embed/thumb.jpg",
            "file": "/x.flac",
            "trackNumber": 7,  # PLAYLIST position
        }

    def test_provider_first_wins_as_a_set(self):
        s = self._scraper("provider")
        s._backend.provider_metadata_for = MagicMock(
            return_value={"source": "tidal", "meta": M.extract_tidal_metadata(TIDAL_INFO)}
        )
        s._backend._isrc = MagicMock()
        s._backend._isrc.resolve_metadata.return_value = M.extract_spotify_metadata(SPOTIFY_JSON)
        meta = self._meta()
        s._enrich_song_meta(meta, "tid")
        # The whole album set comes from Tidal (the provider that served it).
        assert meta["album"] == "Alone"
        assert meta["trackNumber"] == 1  # album number, NOT playlist pos 7
        assert meta["discNumber"] == 1
        assert meta["cover"].startswith("https://resources.tidal.com/")
        assert meta["artists"] == "Jan Blomqvist, MaLou"  # nbsp gone

    def test_spotify_first_when_configured(self):
        s = self._scraper("spotify")
        s._backend.provider_metadata_for = MagicMock(
            return_value={"source": "tidal", "meta": M.extract_tidal_metadata(TIDAL_INFO)}
        )
        s._backend._isrc = MagicMock()
        s._backend._isrc.resolve_metadata.return_value = M.extract_spotify_metadata(SPOTIFY_JSON)
        meta = self._meta()
        s._enrich_song_meta(meta, "tid")
        assert meta["album"] == "Whenever You Need Somebody"  # Spotify set wins
        assert meta["cover"] == "https://i.scdn.co/image/ab67616d0000b273"

    def test_no_cross_source_mixing(self):
        # Provider set lacks an album; must NOT borrow Spotify's album while
        # keeping the provider's number. Falls wholly to the source WITH an album.
        s = self._scraper("provider")
        s._backend.provider_metadata_for = MagicMock(
            return_value={"source": "qobuz", "meta": {"artists": "Q", "trackNumber": 9}}
        )
        s._backend._isrc = MagicMock()
        s._backend._isrc.resolve_metadata.return_value = M.extract_spotify_metadata(SPOTIFY_JSON)
        meta = self._meta()
        s._enrich_song_meta(meta, "tid")
        # Spotify is the only set with an album -> its number, not the provider's 9.
        assert meta["album"] == "Whenever You Need Somebody"
        assert meta["trackNumber"] == 1

    def test_falls_back_to_spotify_when_no_provider(self):
        s = self._scraper("provider")
        s._backend.provider_metadata_for = MagicMock(return_value={"source": "youtube", "meta": {}})
        s._backend._isrc = MagicMock()
        s._backend._isrc.resolve_metadata.return_value = M.extract_spotify_metadata(SPOTIFY_JSON)
        meta = self._meta()
        s._enrich_song_meta(meta, "tid")
        assert meta["album"] == "Whenever You Need Somebody"

    def test_no_rich_source_keeps_embed_but_cleans_nbsp(self):
        s = self._scraper("provider")
        s._backend.provider_metadata_for = MagicMock(return_value={"source": "youtube", "meta": {}})
        s._backend._isrc = MagicMock()
        s._backend._isrc.resolve_metadata.return_value = None
        meta = self._meta()
        s._enrich_song_meta(meta, "tid")
        assert meta["album"] == ""  # nothing fabricated
        assert meta["trackNumber"] == 7  # playlist pos preserved
        assert meta["artists"] == "Jan Blomqvist, MaLou"  # nbsp still cleaned

    def test_youtube_backend_only_cleans_nbsp(self):
        # A non-lossless backend exposes no provider store -> embed stands, cleaned.
        from Spotify_Downloader import MusicScraper

        s = MusicScraper(download_source="youtube")
        meta = self._meta()
        s._enrich_song_meta(meta, "tid")
        assert meta["artists"] == "Jan Blomqvist, MaLou"
        assert meta["album"] == ""

    def test_winner_with_no_track_number_clears_playlist_position(self):
        # A winning release WITH an album but WITHOUT a track number must NOT keep
        # the embed's playlist position (that would mix sources). It clears to 0.
        s = self._scraper("provider")
        s._backend.provider_metadata_for = MagicMock(
            return_value={
                "source": "qobuz",
                "meta": {"album": "Disco", "cover": "https://q/c.jpg"},
            }
        )
        s._backend._isrc = MagicMock()
        s._backend._isrc.resolve_metadata.return_value = None
        meta = self._meta()  # trackNumber == 7 (playlist position)
        s._enrich_song_meta(meta, "tid")
        assert meta["album"] == "Disco"
        assert meta["trackNumber"] == 0  # playlist pos 7 cleared, not mixed in
        assert meta["discNumber"] == 0
        assert meta["cover"] == "https://q/c.jpg"  # winner's cover wins
        assert meta["releaseDate"] == ""  # winner had none -> not the embed's

    def test_provider_first_complete_album_skips_spotify_lookup(self):
        # Lazy: with a complete provider album, the Spotify spclient path is never
        # touched (no needless token/metadata request).
        s = self._scraper("provider")
        s._backend.provider_metadata_for = MagicMock(
            return_value={"source": "tidal", "meta": M.extract_tidal_metadata(TIDAL_INFO)}
        )
        s._backend._isrc = MagicMock()
        s._enrich_song_meta(self._meta(), "tid")
        s._backend._isrc.resolve_metadata.assert_not_called()


# --------------------------------------------------------------------------- #
# Metadata writers: non-destructive album guard + discnumber
# --------------------------------------------------------------------------- #
class TestFlacWriter:
    def _flac(self, tmp_path):
        # Build a minimal real FLAC via ffmpeg so mutagen can tag it.
        import shutil
        import subprocess

        ff = shutil.which("ffmpeg")
        if not ff:
            pytest.skip("ffmpeg not available")
        path = str(tmp_path / "t.flac")
        subprocess.run(
            [ff, "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-y", path],
            capture_output=True,
            check=True,
        )
        return path

    def test_writes_album_disc_and_never_erases(self, tmp_path):
        from mutagen.flac import FLAC

        from Spotify_Downloader import _write_metadata_flac

        path = self._flac(tmp_path)
        _write_metadata_flac(
            path,
            {
                "title": "Alone",
                "artists": "Jan Blomqvist, MaLou",
                "album": "Alone",
                "albumArtist": "Jan Blomqvist",
                "releaseDate": "2023-03-24",
                "trackNumber": 1,
                "discNumber": 1,
            },
            None,
        )
        a = FLAC(path)
        assert a["album"] == ["Alone"]
        assert a["discnumber"] == ["1"]
        assert a["tracknumber"] == ["1"]
        assert a["albumartist"] == ["Jan Blomqvist"]
        # Re-tag with an EMPTY album (rich source unavailable) must NOT erase it.
        _write_metadata_flac(path, {"title": "Alone", "artists": "X", "album": ""}, None)
        assert FLAC(path)["album"] == ["Alone"]

    def test_strips_container_junk(self, tmp_path):
        from mutagen.flac import FLAC

        from Spotify_Downloader import _write_metadata_flac

        path = self._flac(tmp_path)
        a = FLAC(path)
        a["major_brand"] = "M4A "
        a["encoder"] = "Lavf"
        a.save()
        _write_metadata_flac(path, {"title": "t", "artists": "a", "album": "al"}, None)
        out = FLAC(path)
        assert "major_brand" not in out
        assert "encoder" not in out


class TestMp3M4aParity:
    def test_mp3_album_guard_and_disc(self, tmp_path):
        pytest.importorskip("mutagen.easyid3")
        import shutil
        import subprocess

        from mutagen.easyid3 import EasyID3

        from Spotify_Downloader import _write_metadata_mp3

        ff = shutil.which("ffmpeg")
        if not ff:
            pytest.skip("ffmpeg not available")
        path = str(tmp_path / "t.mp3")
        subprocess.run(
            [ff, "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-y", path],
            capture_output=True,
            check=True,
        )
        _write_metadata_mp3(
            path,
            {"title": "t", "artists": "a", "album": "Al", "discNumber": 2, "trackNumber": 3},
            None,
        )
        a = EasyID3(path)
        assert a["album"] == ["Al"]
        assert a["discnumber"] == ["2"]
        _write_metadata_mp3(path, {"title": "t", "artists": "a", "album": ""}, None)
        assert EasyID3(path)["album"] == ["Al"]  # not erased


class TestOpusWriter:
    def _opus(self, tmp_path):
        import shutil
        import subprocess

        ff = shutil.which("ffmpeg")
        if not ff:
            pytest.skip("ffmpeg not available")
        enc = subprocess.run(
            [ff, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        )
        if "libopus" not in enc.stdout:
            pytest.skip("ffmpeg libopus encoder not available")
        path = str(tmp_path / "t.opus")
        subprocess.run(
            [
                ff,
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-c:a",
                "libopus",
                "-y",
                path,
            ],
            capture_output=True,
            check=True,
        )
        return path

    def test_writes_tags_and_cover_never_erases(self, tmp_path):
        from mutagen.oggopus import OggOpus

        from Spotify_Downloader import _write_metadata_opus

        path = self._opus(tmp_path)
        cover = b"\xff\xd8fakejpeg"
        _write_metadata_opus(
            path,
            {
                "title": "Alone",
                "artists": "Jan Blomqvist",
                "album": "Alone",
                "trackNumber": 1,
            },
            cover,
        )
        a = OggOpus(path)
        assert a["title"] == ["Alone"]
        assert a["album"] == ["Alone"]
        assert a["tracknumber"] == ["1"]
        assert a["metadata_block_picture"]
        _write_metadata_opus(path, {"title": "Alone", "artists": "X", "album": ""}, None)
        assert OggOpus(path)["album"] == ["Alone"]

    def test_writer_registered_for_opus(self):
        from Spotify_Downloader import _METADATA_WRITERS, _write_metadata_opus

        assert _METADATA_WRITERS[".opus"] is _write_metadata_opus
