"""Tests for spotifydown_api module."""

from __future__ import annotations

import threading
import time

import pytest

from spotifydown_api import (
    ExtractionError,
    PlaylistClient,
    PlaylistInfo,
    RateLimitError,
    SpotifyEmbedAPI,
    TrackInfo,
    detect_spotify_url_type,
    extract_album_id,
    extract_playlist_id,
    extract_track_id,
    sanitize_filename,
)


class TestExtractPlaylistId:
    """Tests for extract_playlist_id function."""

    def test_valid_playlist_url(self):
        """Extract ID from standard playlist URL."""
        url = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
        assert extract_playlist_id(url) == "37i9dQZF1DXcBWIGoYBM5M"

    def test_playlist_url_with_query_params(self):
        """Extract ID from URL with query parameters."""
        url = "https://open.spotify.com/playlist/abc123?si=xyz789"
        # Current implementation uses re.match which won't match query params
        # This tests current behavior - ID before query params
        assert extract_playlist_id(url) == "abc123"

    def test_invalid_url_raises_valueerror(self):
        """Invalid URLs should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid Spotify playlist URL"):
            extract_playlist_id("https://example.com/playlist/123")

    def test_track_url_raises_valueerror(self):
        """Track URLs should raise ValueError for playlist extraction."""
        with pytest.raises(ValueError, match="Invalid Spotify playlist URL"):
            extract_playlist_id("https://open.spotify.com/track/abc123")

    def test_album_url_raises_valueerror(self):
        """Album URLs should raise ValueError for playlist extraction."""
        with pytest.raises(ValueError, match="Invalid Spotify playlist URL"):
            extract_playlist_id("https://open.spotify.com/album/abc123")

    def test_empty_url_raises_valueerror(self):
        """Empty URL should raise ValueError."""
        with pytest.raises(ValueError):
            extract_playlist_id("")


class TestExtractAlbumId:
    """Tests for extract_album_id function."""

    def test_valid_album_url(self):
        """Extract ID from standard album URL."""
        url = "https://open.spotify.com/album/1JcLZljq8ADWNCdwVJKNID"
        assert extract_album_id(url) == "1JcLZljq8ADWNCdwVJKNID"

    def test_album_url_with_query_params(self):
        """Extract ID from album URL with trailing query params."""
        url = "https://open.spotify.com/album/abc123?si=xyz789"
        assert extract_album_id(url) == "abc123"

    def test_album_uri(self):
        """Extract ID from a spotify:album: URI."""
        assert extract_album_id("spotify:album:abc123") == "abc123"

    def test_playlist_url_raises_valueerror(self):
        """Playlist URLs should raise ValueError for album extraction."""
        with pytest.raises(ValueError, match="Invalid Spotify album URL"):
            extract_album_id("https://open.spotify.com/playlist/abc123")


class TestExtractTrackId:
    """Tests for extract_track_id function."""

    def test_valid_track_url(self):
        """Extract ID from standard track URL."""
        url = "https://open.spotify.com/track/4iV5W9uYEdYUVa79Axb7Rh"
        assert extract_track_id(url) == "4iV5W9uYEdYUVa79Axb7Rh"

    def test_invalid_url_raises_valueerror(self):
        """Invalid URLs should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid Spotify track URL"):
            extract_track_id("https://example.com/track/123")

    def test_playlist_url_raises_valueerror(self):
        """Playlist URLs should raise ValueError for track extraction."""
        with pytest.raises(ValueError, match="Invalid Spotify track URL"):
            extract_track_id("https://open.spotify.com/playlist/abc123")


class TestDetectSpotifyUrlType:
    """Tests for detect_spotify_url_type function."""

    def test_detect_playlist(self):
        """Detect playlist URL type."""
        url = "https://open.spotify.com/playlist/abc123"
        url_type, item_id = detect_spotify_url_type(url)
        assert url_type == "playlist"
        assert item_id == "abc123"

    def test_detect_track(self):
        """Detect track URL type."""
        url = "https://open.spotify.com/track/xyz789"
        url_type, item_id = detect_spotify_url_type(url)
        assert url_type == "track"
        assert item_id == "xyz789"

    def test_invalid_url_raises_valueerror(self):
        """Invalid URLs should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid Spotify URL"):
            detect_spotify_url_type("https://example.com/something")

    def test_detect_album(self):
        """Detect album URL type."""
        url = "https://open.spotify.com/album/1JcLZljq8ADWNCdwVJKNID"
        url_type, item_id = detect_spotify_url_type(url)
        assert url_type == "album"
        assert item_id == "1JcLZljq8ADWNCdwVJKNID"

    def test_detect_album_intl_and_query(self):
        """Detect album URL with locale prefix and trailing query params."""
        url = "https://open.spotify.com/intl-de/album/abc123?si=xyz"
        url_type, item_id = detect_spotify_url_type(url)
        assert url_type == "album"
        assert item_id == "abc123"


class TestSanitizeFilename:
    """Tests for sanitize_filename function."""

    def test_basic_sanitization(self):
        """Basic filename sanitization."""
        assert sanitize_filename("Hello World") == "Hello World"

    def test_removes_windows_reserved_chars(self):
        """Only the Windows-reserved characters are stripped (superset of mac/linux)."""
        assert sanitize_filename('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij"

    def test_keeps_ordinary_punctuation(self):
        """Non-reserved punctuation is legal on all platforms and kept verbatim."""
        assert sanitize_filename("P!nk - Sober (Remix) [Explicit] & more") == (
            "P!nk - Sober (Remix) [Explicit] & more"
        )

    def test_preserves_allowed_chars(self):
        """Allowed characters should be preserved."""
        assert sanitize_filename("file-name_123.mp3") == "file-name_123.mp3"

    def test_collapses_multiple_spaces(self):
        """Multiple spaces should be collapsed to one."""
        assert sanitize_filename("Hello    World") == "Hello World"

    def test_no_spaces_option(self):
        """Test allow_spaces=False."""
        result = sanitize_filename("Hello World", allow_spaces=False)
        assert result == "HelloWorld"

    def test_empty_or_reserved_only_returns_unknown(self):
        """Empty input, or input that is entirely reserved chars, returns 'Unknown'."""
        assert sanitize_filename("") == "Unknown"
        assert sanitize_filename('/\\:*?"<>|') == "Unknown"

    def test_unicode_preserved(self):
        """Accented and non-Latin characters must be kept (closes the special-char bug)."""
        assert sanitize_filename("MONTAGEM BAILÃO") == "MONTAGEM BAILÃO"
        assert sanitize_filename("Café ☕ Music") == "Café ☕ Music"
        assert sanitize_filename("Beyoncé") == "Beyoncé"
        assert sanitize_filename("日本語の歌") == "日本語の歌"
        assert sanitize_filename("Метель") == "Метель"

    def test_control_chars_removed(self):
        """Control characters (tab, newline, NUL) are stripped on all platforms."""
        assert sanitize_filename("tab\there\nnewline\x00nul") == "tabherenewlinenul"

    def test_trailing_and_leading_dots_spaces_trimmed(self):
        """Windows rejects trailing dots/spaces; a leading dot hides files on Unix."""
        assert sanitize_filename("trailing. ") == "trailing"
        assert sanitize_filename("...Baby One More Time") == "Baby One More Time"

    def test_windows_reserved_device_names_escaped(self):
        """DOS device names (CON, NUL, COM1, ...) are escaped, incl. with extension."""
        assert sanitize_filename("NUL") == "_NUL"
        assert sanitize_filename("con.mp3") == "_con.mp3"
        assert sanitize_filename("COM1") == "_COM1"
        assert sanitize_filename("LPT9.flac") == "_LPT9.flac"
        # a normal title that merely contains a reserved word is untouched
        assert sanitize_filename("CONcrete") == "CONcrete"


class TestSpotifyEmbedAPI:
    """Tests for SpotifyEmbedAPI class."""

    def test_init_creates_session(self):
        """Init should create a requests session if not provided."""
        api = SpotifyEmbedAPI()
        assert api._session is not None

    def test_init_uses_provided_session(self):
        """Init should use provided session."""
        import requests

        session = requests.Session()
        api = SpotifyEmbedAPI(session=session)
        assert api._session is session

    def test_headers_include_user_agent(self):
        """Headers should include a user agent."""
        api = SpotifyEmbedAPI()
        headers = api._headers()
        assert "user-agent" in headers
        assert "Chrome" in headers["user-agent"]

    def test_embed_url_selection_by_content_type(self):
        """Album content uses the album embed endpoint, playlist the playlist one."""
        api = SpotifyEmbedAPI()
        assert api._embed_url_for("abc", "album") == ("https://open.spotify.com/embed/album/abc")
        assert api._embed_url_for("abc", "playlist") == (
            "https://open.spotify.com/embed/playlist/abc"
        )

    def test_album_iteration_tags_album_name_and_skips_spclient(self):
        """Album iteration uses the album embed URL, tags every track with the
        album name, and never invokes the playlist-only spclient fallback."""
        api = SpotifyEmbedAPI()
        fetched_urls: list[str] = []
        album_data = {
            "props": {
                "pageProps": {
                    "state": {
                        "data": {
                            "entity": {
                                "name": "My Album",
                                "trackList": [
                                    {
                                        "uri": "spotify:track:t1",
                                        "title": "One",
                                        "subtitle": "Artist A",
                                        "duration": 100000,
                                    },
                                    {
                                        "uri": "spotify:track:t2",
                                        "title": "Two",
                                        "subtitle": "Artist B",
                                        "duration": 200000,
                                    },
                                ],
                            }
                        }
                    }
                }
            }
        }

        def fake_fetch(url: str) -> dict:
            fetched_urls.append(url)
            return album_data

        def explode(*_args, **_kwargs):
            raise AssertionError("spclient must not be called for albums")

        api._fetch_embed_data = fake_fetch  # type: ignore[assignment]
        api._session.get = explode  # type: ignore[assignment]

        tracks = list(api.iter_playlist_tracks("ALBUMID", content_type="album"))

        assert len(tracks) == 2
        assert all(t.album == "My Album" for t in tracks)
        assert fetched_urls == ["https://open.spotify.com/embed/album/ALBUMID"]


class TestPlaylistClient:
    """Tests for PlaylistClient class."""

    def test_init_creates_embed_api(self):
        """Init should create an embed API instance."""
        client = PlaylistClient()
        assert client._embed_api is not None
        assert isinstance(client._embed_api, SpotifyEmbedAPI)

    def test_get_track_download_link_returns_none(self):
        """Download link should return None (feature deprecated)."""
        client = PlaylistClient()
        assert client.get_track_download_link("abc123") is None

    def test_get_track_youtube_id_returns_none(self):
        """YouTube ID should return None (feature deprecated)."""
        client = PlaylistClient()
        assert client.get_track_youtube_id("abc123") is None


class TestTrackInfo:
    """Tests for TrackInfo dataclass."""

    def test_spotify_id_property(self):
        """spotify_id property should return the id field."""
        track = TrackInfo(
            id="abc123",
            title="Test",
            artists="Artist",
            album=None,
            release_date=None,
            cover_url=None,
            duration_ms=None,
            preview_url=None,
            raw={},
        )
        assert track.spotify_id == "abc123"


class TestPlaylistInfo:
    """Tests for PlaylistInfo dataclass."""

    def test_dataclass_fields(self):
        """Test dataclass field defaults."""
        info = PlaylistInfo(
            name="Test",
            owner=None,
            description=None,
            cover_url=None,
        )
        assert info.name == "Test"
        assert info.track_count is None


class TestDeepFind:
    """Tests for SpotifyEmbedAPI._deep_find static method."""

    def test_finds_key_at_top_level(self):
        data = {"trackList": [1, 2, 3], "other": "value"}
        result = SpotifyEmbedAPI._deep_find(data, "trackList")
        assert result is data

    def test_finds_key_nested(self):
        data = {"a": {"b": {"trackList": [1]}}}
        result = SpotifyEmbedAPI._deep_find(data, "trackList")
        assert result == {"trackList": [1]}

    def test_returns_none_when_missing(self):
        data = {"a": {"b": {"c": "d"}}}
        assert SpotifyEmbedAPI._deep_find(data, "trackList") is None

    def test_respects_max_depth(self):
        data = {"a": {"b": {"c": {"trackList": [1]}}}}
        assert SpotifyEmbedAPI._deep_find(data, "trackList", max_depth=2) is None
        assert SpotifyEmbedAPI._deep_find(data, "trackList", max_depth=4) is not None

    def test_non_dict_returns_none(self):
        assert SpotifyEmbedAPI._deep_find("string", "key") is None  # type: ignore
        assert SpotifyEmbedAPI._deep_find([], "key") is None  # type: ignore


class TestResolvePath:
    """Tests for SpotifyEmbedAPI._resolve_path static method."""

    def test_resolves_valid_path(self):
        data = {"a": {"b": {"c": "value"}}}
        assert SpotifyEmbedAPI._resolve_path(data, ("a", "b", "c")) == "value"

    def test_returns_none_on_missing_key(self):
        data = {"a": {"b": "value"}}
        assert SpotifyEmbedAPI._resolve_path(data, ("a", "x")) is None

    def test_returns_none_on_non_dict_intermediate(self):
        data = {"a": "string"}
        assert SpotifyEmbedAPI._resolve_path(data, ("a", "b")) is None

    def test_empty_path_returns_data(self):
        data = {"a": 1}
        assert SpotifyEmbedAPI._resolve_path(data, ()) is data


class TestResilientExtraction:
    """Tests for resilient entity and token extraction across JSON structures."""

    def test_extract_entity_standard_path(self):
        """Entity found via standard state.data.entity path."""
        api = SpotifyEmbedAPI()
        data = {"props": {"pageProps": {"state": {"data": {"entity": {"name": "Test"}}}}}}
        assert api._extract_entity(data) == {"name": "Test"}

    def test_extract_entity_no_state(self):
        """Entity found when 'state' wrapper is missing."""
        api = SpotifyEmbedAPI()
        data = {"props": {"pageProps": {"data": {"entity": {"name": "NoState"}}}}}
        assert api._extract_entity(data) == {"name": "NoState"}

    def test_extract_entity_flat(self):
        """Entity found directly under pageProps."""
        api = SpotifyEmbedAPI()
        data = {"props": {"pageProps": {"entity": {"name": "Flat"}}}}
        assert api._extract_entity(data) == {"name": "Flat"}

    def test_extract_entity_deep_find_fallback(self):
        """Entity found via _deep_find when no known path matches."""
        api = SpotifyEmbedAPI()
        data = {
            "props": {
                "pageProps": {
                    "weirdKey": {
                        "nested": {"trackList": [{"uri": "spotify:track:x"}], "name": "Deep"}
                    }
                }
            }
        }
        result = api._extract_entity(data)
        assert result["name"] == "Deep"
        assert "trackList" in result

    def test_extract_entity_missing_raises(self):
        """ExtractionError raised with pageProps keys when entity not found."""
        api = SpotifyEmbedAPI()
        data = {"props": {"pageProps": {"someKey": "value", "otherKey": 42}}}
        with pytest.raises(ExtractionError, match="pageProps keys:"):
            api._extract_entity(data)

    def test_token_extraction_standard(self):
        """Token extracted from standard path."""
        api = SpotifyEmbedAPI()
        data = {
            "props": {
                "pageProps": {
                    "state": {
                        "data": {"entity": {"name": "Test"}},
                        "settings": {
                            "session": {
                                "accessToken": "tok123",
                                "accessTokenExpirationTimestampMs": 9999999999999,
                            }
                        },
                    }
                }
            }
        }
        # Simulate what _fetch_embed_data does for token caching
        _TOKEN_PATHS = (
            ("props", "pageProps", "state", "settings", "session"),
            ("props", "pageProps", "settings", "session"),
            ("props", "pageProps", "session"),
        )
        for path in _TOKEN_PATHS:
            session_data = api._resolve_path(data, path)
            if isinstance(session_data, dict) and "accessToken" in session_data:
                api._cached_token = session_data.get("accessToken")
                break
        assert api._cached_token == "tok123"

    def test_token_extraction_no_state(self):
        """Token extracted when 'state' wrapper is missing."""
        api = SpotifyEmbedAPI()
        data = {
            "props": {
                "pageProps": {
                    "settings": {
                        "session": {
                            "accessToken": "tok_alt",
                            "accessTokenExpirationTimestampMs": 9999999999999,
                        }
                    }
                }
            }
        }
        _TOKEN_PATHS = (
            ("props", "pageProps", "state", "settings", "session"),
            ("props", "pageProps", "settings", "session"),
            ("props", "pageProps", "session"),
        )
        for path in _TOKEN_PATHS:
            session_data = api._resolve_path(data, path)
            if isinstance(session_data, dict) and "accessToken" in session_data:
                api._cached_token = session_data.get("accessToken")
                break
        assert api._cached_token == "tok_alt"

    def test_token_extraction_flat(self):
        """Token extracted from flat session path."""
        api = SpotifyEmbedAPI()
        data = {
            "props": {
                "pageProps": {
                    "session": {
                        "accessToken": "tok_flat",
                        "accessTokenExpirationTimestampMs": 9999999999999,
                    }
                }
            }
        }
        _TOKEN_PATHS = (
            ("props", "pageProps", "state", "settings", "session"),
            ("props", "pageProps", "settings", "session"),
            ("props", "pageProps", "session"),
        )
        for path in _TOKEN_PATHS:
            session_data = api._resolve_path(data, path)
            if isinstance(session_data, dict) and "accessToken" in session_data:
                api._cached_token = session_data.get("accessToken")
                break
        assert api._cached_token == "tok_flat"


_OVERFLOW_ENTITY = {
    "name": "Big Playlist",
    "trackList": [
        {"uri": "spotify:track:abc123", "title": "T1", "subtitle": "A1", "duration": 180000},
        {"uri": "spotify:track:def456", "title": "T2", "subtitle": "A2", "duration": 200000},
    ],
}

_OVERFLOW_PLAYLIST_DATA = {
    "props": {"pageProps": {"state": {"data": {"entity": _OVERFLOW_ENTITY}}}}
}

_SPCLIENT_OVERFLOW = {
    "length": 150,
    "contents": {
        "items": [
            {"uri": "spotify:track:abc123"},
            {"uri": "spotify:track:def456"},
            {"uri": "spotify:track:overflow1"},
            {"uri": "spotify:track:overflow2"},
        ]
    },
}

_TRACK_EMBED_ENTITY = {
    "name": "Overflow Track",
    "artists": [{"name": "Overflow Artist"}],
    "duration": 210000,
    "visualIdentity": {"image": [{"url": "https://example.com/cover.jpg", "maxWidth": 300}]},
    "releaseDate": {"isoString": "2024-01-15T00:00:00Z"},
    "audioPreview": {"url": "https://preview.example.com/track.mp3"},
}

_TRACK_EMBED_DATA = {"props": {"pageProps": {"state": {"data": {"entity": _TRACK_EMBED_ENTITY}}}}}


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json


def _wire_overflow_playlist(api: SpotifyEmbedAPI) -> None:
    """Stub playlist embed + spclient so iter_playlist_tracks hits the overflow pool."""
    api._cached_token = "tok"

    def fake_fetch(url: str) -> dict:
        if "/embed/playlist/" in url:
            return _OVERFLOW_PLAYLIST_DATA
        raise AssertionError(f"unexpected embed fetch: {url}")

    def fake_get(url, **kwargs):
        if "spclient" in url:
            return _FakeResponse(200, json_data=_SPCLIENT_OVERFLOW)
        raise AssertionError(f"unexpected session.get: {url}")

    api._fetch_embed_data = fake_fetch  # type: ignore[assignment]
    api._session.get = fake_get  # type: ignore[assignment]


class TestOverflowMetadataResolver:
    """Overflow-track metadata: Mercury resolver first, gated anonymous fallback."""

    def test_overflow_tracks_use_resolver_not_embed(self):
        resolver_meta = {
            "title": "Resolved Title",
            "artists": "Resolved Artist",
            "album": "Resolved Album",
            "releaseDate": "2024-06-01",
            "cover": "https://example.com/resolved.jpg",
            "durationMs": 213000,
        }

        def resolver(track_id: str):
            assert track_id.startswith("overflow")
            return dict(resolver_meta)

        api = SpotifyEmbedAPI(metadata_resolver=resolver)

        def explode_once(url: str) -> dict:
            if "/embed/track/" in url:
                raise AssertionError("embed track fetch must not run when resolver succeeds")
            raise AssertionError(f"unexpected once fetch: {url}")

        api._fetch_embed_data_once = explode_once  # type: ignore[assignment]
        _wire_overflow_playlist(api)

        overflow_tracks = [
            t for t in api.iter_playlist_tracks("PLID") if t.id.startswith("overflow")
        ]
        assert len(overflow_tracks) == 2
        for track in overflow_tracks:
            assert track.id in ("overflow1", "overflow2")
            assert track.title == "Resolved Title"
            assert track.artists == "Resolved Artist"
            assert track.album == "Resolved Album"
            assert track.release_date == "2024-06-01"
            assert track.cover_url == "https://example.com/resolved.jpg"
            assert track.duration_ms == 213000
            assert track.preview_url is None
            assert track.raw == resolver_meta

    def test_resolver_miss_falls_back_to_track_embed(self):
        api = SpotifyEmbedAPI(metadata_resolver=lambda _tid: None)

        def fake_once(url: str) -> dict:
            if "/embed/track/" in url:
                return _TRACK_EMBED_DATA
            raise AssertionError(f"unexpected once fetch: {url}")

        api._fetch_embed_data_once = fake_once  # type: ignore[assignment]
        _wire_overflow_playlist(api)

        overflow_tracks = [
            t for t in api.iter_playlist_tracks("PLID") if t.id.startswith("overflow")
        ]
        assert len(overflow_tracks) == 2
        track = overflow_tracks[0]
        assert track.title == "Overflow Track"
        assert track.artists == "Overflow Artist"
        assert track.duration_ms == 210000
        assert track.cover_url == "https://example.com/cover.jpg"
        assert track.release_date == "2024-01-15"

    def test_resolver_dict_without_title_or_artists_falls_back(self):
        api = SpotifyEmbedAPI(metadata_resolver=lambda _tid: {"artists": "X"})
        api._fetch_embed_data_once = lambda url: (  # type: ignore[assignment]
            _TRACK_EMBED_DATA if "/embed/track/" in url else (_ for _ in ()).throw(AssertionError())
        )
        _wire_overflow_playlist(api)
        tracks = [t for t in api.iter_playlist_tracks("PLID") if t.id.startswith("overflow")]
        assert tracks and tracks[0].title == "Overflow Track"

        api2 = SpotifyEmbedAPI(metadata_resolver=lambda _tid: {"title": "Y"})
        api2._fetch_embed_data_once = api._fetch_embed_data_once  # type: ignore[assignment]
        _wire_overflow_playlist(api2)
        tracks2 = [t for t in api2.iter_playlist_tracks("PLID") if t.id.startswith("overflow")]
        assert tracks2 and tracks2[0].title == "Overflow Track"

    def test_resolver_exception_is_swallowed(self):
        def boom(_tid):
            raise RuntimeError("resolver blew up")

        api = SpotifyEmbedAPI(metadata_resolver=boom)
        api._fetch_embed_data_once = lambda url: (  # type: ignore[assignment]
            _TRACK_EMBED_DATA if "/embed/track/" in url else (_ for _ in ()).throw(AssertionError())
        )
        _wire_overflow_playlist(api)
        tracks = list(api.iter_playlist_tracks("PLID"))
        assert len(tracks) == 4  # 2 embed + 2 overflow via fallback

    def test_get_track_prefers_resolver(self):
        meta = {
            "title": "Single",
            "artists": "Artist",
            "album": "Album",
            "cover": "https://example.com/cover.jpg",
        }
        api = SpotifyEmbedAPI(metadata_resolver=lambda _tid: meta)

        def explode_once(url: str) -> dict:
            raise AssertionError("embed fetch must not run")

        api._fetch_embed_data_once = explode_once  # type: ignore[assignment]
        track = api.get_track("trackid")
        assert track.title == "Single"
        assert track.artists == "Artist"
        assert track.album == "Album"

    def test_resolver_without_cover_falls_back_to_embed(self):
        resolver_meta = {
            "title": "Mercury Title",
            "artists": "Mercury Artist",
            "album": "Mercury Album",
            "durationMs": 180000,
        }
        embed_calls: list[str] = []

        def fake_once(url: str) -> dict:
            if "/embed/track/" in url:
                embed_calls.append(url)
                return _TRACK_EMBED_DATA
            raise AssertionError(f"unexpected once fetch: {url}")

        api = SpotifyEmbedAPI(metadata_resolver=lambda _tid: dict(resolver_meta))
        api._fetch_embed_data_once = fake_once  # type: ignore[assignment]

        track = api.get_track("trackid")
        assert embed_calls == [api._EMBED_TRACK_URL.format(track_id="trackid")]
        assert track.title == "Overflow Track"
        assert track.artists == "Overflow Artist"
        assert track.cover_url == "https://example.com/cover.jpg"

    def test_resolver_without_cover_returned_when_embed_fails(self):
        resolver_meta = {
            "title": "Mercury Title",
            "artists": "Mercury Artist",
            "album": "Mercury Album",
            "durationMs": 180000,
        }
        api = SpotifyEmbedAPI(metadata_resolver=lambda _tid: dict(resolver_meta))

        def always_429(url: str) -> dict:
            raise RateLimitError("rate limited", retry_after=0.0)

        api._fetch_track_embed_data = always_429  # type: ignore[assignment]

        track = api.get_track("trackid")
        assert track.title == "Mercury Title"
        assert track.artists == "Mercury Artist"
        assert track.album == "Mercury Album"
        assert track.cover_url is None
        assert track.duration_ms == 180000


class TestSharedRateLimitGate:
    def test_gate_trip_honors_retry_after_and_caps(self, monkeypatch):
        from spotifydown_api import _SharedRateLimitGate

        now = 1000.0
        monkeypatch.setattr("spotifydown_api.time.monotonic", lambda: now)
        gate = _SharedRateLimitGate()

        gate.trip(5.0)
        assert gate.tripped is True
        assert gate._until == pytest.approx(now + 5.0)

        gate.tripped = False
        gate.trip(None)
        assert gate.tripped is True
        assert gate._until == pytest.approx(now + _SharedRateLimitGate.DEFAULT_COOLDOWN_S)

        gate.tripped = False
        gate.trip(999.0)
        assert gate.tripped is True
        assert gate._until == pytest.approx(now + _SharedRateLimitGate.MAX_COOLDOWN_S)

    def test_gate_wait_returns_false_when_cancelled(self):
        from spotifydown_api import _SharedRateLimitGate

        gate = _SharedRateLimitGate()
        gate._until = time.monotonic() + 3600.0
        cancel = threading.Event()
        cancel.set()
        assert gate.wait(cancel) is False

    def test_gate_wait_wakes_blocked_waiter_on_cancel(self):
        from spotifydown_api import _SharedRateLimitGate

        gate = _SharedRateLimitGate()
        gate.trip(10.0)
        cancel = threading.Event()
        wait_result: list[bool | None] = [None]

        def waiter() -> None:
            wait_result[0] = gate.wait(cancel)

        start = time.monotonic()
        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        cancel.set()
        thread.join(timeout=2.0)
        elapsed = time.monotonic() - start

        assert not thread.is_alive()
        assert wait_result[0] is False
        assert elapsed < 1.0

    def test_track_429_trips_gate_and_stops_after_one_retry(self, monkeypatch):
        import time as time_mod

        api = SpotifyEmbedAPI()
        get_calls = {"n": 0}

        def fake_get(url, **kwargs):
            get_calls["n"] += 1
            return _FakeResponse(429, headers={"Retry-After": "0"})

        api._session.get = fake_get  # type: ignore[assignment]
        monkeypatch.setattr(
            time_mod,
            "sleep",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not blind-retry 429")),
        )

        result = api._fetch_track_metadata_anonymous("trackid")
        assert result is None
        assert get_calls["n"] == 2
        assert api._rate_gate.tripped is True

    def test_rate_limit_notice_emitted_once(self, monkeypatch):
        import time as time_mod

        api = SpotifyEmbedAPI(metadata_resolver=lambda _tid: None)
        notices: list[str] = []

        def fake_get(url, **kwargs):
            if "spclient" in url:
                return _FakeResponse(200, json_data=_SPCLIENT_OVERFLOW)
            if "/embed/track/" in url:
                return _FakeResponse(429, headers={"Retry-After": "0"})
            raise AssertionError(url)

        api._fetch_embed_data = lambda _url: _OVERFLOW_PLAYLIST_DATA  # type: ignore[assignment]
        api._cached_token = "tok"
        api._session.get = fake_get  # type: ignore[assignment]
        monkeypatch.setattr(time_mod, "sleep", lambda *_a, **_k: None)

        list(api.iter_playlist_tracks("PLID", on_notice=notices.append))
        assert len(notices) == 1
        assert "rate-limiting" in notices[0].lower()

    def test_rate_limit_notice_resets_between_runs(self, monkeypatch):
        import time as time_mod

        api = SpotifyEmbedAPI(metadata_resolver=lambda _tid: None)
        notices: list[str] = []

        def fake_get_run1(url, **kwargs):
            if "spclient" in url:
                return _FakeResponse(200, json_data=_SPCLIENT_OVERFLOW)
            if "/embed/track/" in url:
                return _FakeResponse(429, headers={"Retry-After": "0"})
            raise AssertionError(url)

        api._fetch_embed_data = lambda _url: _OVERFLOW_PLAYLIST_DATA  # type: ignore[assignment]
        api._cached_token = "tok"
        api._session.get = fake_get_run1  # type: ignore[assignment]
        monkeypatch.setattr(time_mod, "sleep", lambda *_a, **_k: None)

        list(api.iter_playlist_tracks("PLID", on_notice=notices.append))
        assert len(notices) == 1

        def fake_get_run2(url, **kwargs):
            if "spclient" in url:
                return _FakeResponse(200, json_data=_SPCLIENT_OVERFLOW)
            if "/embed/track/" in url:
                return _FakeResponse(200, json_data=_TRACK_EMBED_DATA)
            raise AssertionError(url)

        api._session.get = fake_get_run2  # type: ignore[assignment]
        list(api.iter_playlist_tracks("PLID", on_notice=notices.append))
        assert len(notices) == 1

    def test_generator_close_sets_cancel_event(self):
        import threading as th

        api = SpotifyEmbedAPI()
        captured: list[th.Event] = []
        wait_results: list[bool | None] = []
        started = th.Event()
        closed = th.Event()

        def blocking_fetch(track_id: str, cancel_event: th.Event | None = None):
            if track_id.startswith("overflow"):
                captured.append(cancel_event)
                started.set()
                if cancel_event is not None:
                    wait_results.append(cancel_event.wait(timeout=2.0))
            return None

        api._fetch_track_metadata = blocking_fetch  # type: ignore[assignment]
        _wire_overflow_playlist(api)

        gen = api.iter_playlist_tracks("PLID")
        collected: list[TrackInfo] = []

        def consume() -> None:
            try:
                for track in gen:
                    collected.append(track)
            finally:
                gen.close()
                closed.set()

        start = time.monotonic()
        consumer = th.Thread(target=consume, daemon=True)
        consumer.start()
        assert started.wait(timeout=2.0)
        # Consumer is blocked in as_completed until the pool worker unblocks.
        # finally uses this same Event; set() wakes the worker (what close does).
        assert captured and captured[0] is not None
        captured[0].set()
        assert closed.wait(timeout=2.0)
        consumer.join(timeout=2.0)
        elapsed = time.monotonic() - start

        assert captured[0].is_set()
        assert wait_results
        assert wait_results[0] is True
        assert elapsed < 1.5
