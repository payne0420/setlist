"""Tests for the Real FLAC (lossless) backend.

Mirrors the style of TestDownloadTrackAudioOpts: HTTP + subprocess are mocked,
and the load-bearing crypto/signature constants are pinned against the worked
vectors extracted from SpotiFLAC/backend/*.go (goal §14).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from lossless import bridge, qobuz, validate
from lossless import constants as C
from lossless import spotify_isrc as si
from lossless._util import aesgcm_decrypt


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, status=200, json_data=None, text="", content=b"", headers=None):
        self.status_code = status
        self._json = json_data
        self.text = text if text else (json.dumps(json_data) if json_data is not None else "")
        self.content = content or self.text.encode()
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class RouteSession:
    """A fake requests.Session that dispatches .get/.post/.head by URL substring."""

    def __init__(self, routes):
        self._routes = routes
        self.calls = []

    def _match(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for needle, resp in self._routes.items():
            if needle in url:
                return resp(url, kw) if callable(resp) else resp
        return FakeResp(404, text="no route")

    def get(self, url, **kw):
        return self._match("GET", url, **kw)

    def post(self, url, **kw):
        return self._match("POST", url, **kw)

    def head(self, url, **kw):
        return self._match("HEAD", url, **kw)


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("lossless._util.cache_dir", lambda: str(tmp_path))
    return tmp_path


# --------------------------------------------------------------------------- #
# (b) Qobuz MD5 signature — known vectors
# --------------------------------------------------------------------------- #
class TestQobuzSignature:
    def test_signature_payload_and_vector(self):
        params = {"query": ["USUM71703861"], "limit": ["1"]}
        payload = qobuz.signature_payload(
            "track/search", params, "1700000000", C.QOBUZ_DEFAULT_APP_SECRET
        )
        assert payload == (
            "tracksearchlimit1queryUSUM71703861" "1700000000" + C.QOBUZ_DEFAULT_APP_SECRET
        )
        assert qobuz.request_signature(
            "track/search", params, "1700000000", C.QOBUZ_DEFAULT_APP_SECRET
        ) == ("80f86ad720695063422aaf47c563934c")

    def test_trackget_vector(self):
        sig = qobuz.request_signature(
            "track/get", {"track_id": ["123456"]}, "1700000000", C.QOBUZ_DEFAULT_APP_SECRET
        )
        assert sig == "b28ecba4d3d542c33ba2c1549b42a3e2"

    def test_signature_excludes_injected_keys(self):
        # app_id/request_ts/request_sig must not enter the payload.
        base = {"query": ["x"]}
        with_injected = {**base, "app_id": ["a"], "request_ts": ["1"], "request_sig": ["z"]}
        assert qobuz.signature_payload("track/search", base, "1700000000", "s") == (
            qobuz.signature_payload("track/search", with_injected, "1700000000", "s")
        )

    def test_normalize_search_value_single_pass(self):
        assert qobuz.normalize_search_value("AC/DC - Back feat. X") == "ac dc back x"
        assert qobuz.normalize_search_value("A & B") == "a and b"

    def test_score_candidate(self):
        track = {
            "title": "Song",
            "performer": {"name": "Artist"},
            "album": {"title": "Alb"},
            "hires": True,
        }
        assert qobuz.score_candidate(track, "Song", "Artist", "Alb") == 1000 + 300 + 150 + 40

    def test_gdstudio_signature_vector(self):
        import hashlib

        base = "music.gdstudio.xyz|20260510|172000000|341032040"
        assert hashlib.md5(base.encode()).hexdigest()[-8:].upper() == "810AEDA3"
        assert qobuz.QobuzClient._gdstudio_padded_version() == "20260510"


# --------------------------------------------------------------------------- #
# (c) AES-GCM debug-key decryption — stable plaintext
# --------------------------------------------------------------------------- #
class TestDebugKeys:
    def test_musicdl_key(self):
        pt = aesgcm_decrypt(
            C.QOBUZ_MUSICDL_DEBUG_KEY_SEED_PARTS,
            C.QOBUZ_MUSICDL_DEBUG_KEY_NONCE,
            C.QOBUZ_MUSICDL_DEBUG_KEY_CIPHERTEXT,
            C.QOBUZ_MUSICDL_DEBUG_KEY_TAG,
            C.QOBUZ_MUSICDL_DEBUG_KEY_AAD,
        ).decode()
        assert pt == "ryzmicisgoatedandnothingcomesevenclose"

    def test_amazon_key(self):
        pt = aesgcm_decrypt(
            C.AMAZON_DEBUG_KEY_SEED_PARTS,
            C.AMAZON_DEBUG_KEY_NONCE,
            C.AMAZON_DEBUG_KEY_CIPHERTEXT,
            C.AMAZON_DEBUG_KEY_TAG,
            C.AMAZON_DEBUG_KEY_AAD,
        ).decode()
        assert pt == "spotbyeqzziokofiafkarxyz"


# --------------------------------------------------------------------------- #
# (a) ISRC flow — token + spclient requests
# --------------------------------------------------------------------------- #
class TestIsrc:
    def test_base62_gid_roundtrip(self):
        gid = si.spotify_entity_id_to_gid("4cOdK2wGLETKBW3PvgPWqT")
        assert len(gid) == 32 and all(c in "0123456789abcdef" for c in gid)

    def test_totp_six_digit(self):
        code, ver = si.generate_spotify_totp(1700000000)
        assert len(code) == 6 and code.isdigit() and ver == 61

    def test_isrc_regex_uppercases(self):
        assert si.first_isrc_match('{"id":"usum71703861"}') == "USUM71703861"

    def test_token_request_params(self, isolated_cache):
        captured = {}

        def token_route(url, kw):
            captured.update(kw.get("params", {}))
            return FakeResp(
                200, {"accessToken": "tok", "accessTokenExpirationTimestampMs": 9_999_999_999_999}
            )

        sess = RouteSession({"open.spotify.com/api/token": token_route})
        r = si.SpotifyIsrcResolver(session=sess)
        assert r._get_token() == "tok"
        assert captured["totp"] == captured["totpServer"]  # same code
        assert captured["totpVer"] == "61"
        assert captured["reason"] == "init" and captured["productType"] == "web-player"

    def test_spclient_isrc_extraction(self, isolated_cache):
        meta = {"external_id": [{"type": "isrc", "id": "usum71703861"}]}
        sess = RouteSession(
            {
                "api/token": FakeResp(
                    200, {"accessToken": "t", "accessTokenExpirationTimestampMs": 9_999_999_999_999}
                ),
                "spclient.wg.spotify.com": FakeResp(200, meta),
            }
        )
        r = si.SpotifyIsrcResolver(session=sess)
        assert r.resolve("4cOdK2wGLETKBW3PvgPWqT") == "USUM71703861"
        # spclient request carried a Bearer token + market=from_token
        spcall = next(c for c in sess.calls if "spclient" in c[1])
        assert spcall[2]["headers"]["Authorization"] == "Bearer t"
        assert "market=from_token" in spcall[1]

    def test_soundplate_fallback(self, isolated_cache):
        sess = RouteSession(
            {
                "api/token": FakeResp(
                    200, {"accessToken": "t", "accessTokenExpirationTimestampMs": 9_999_999_999_999}
                ),
                "spclient.wg.spotify.com": FakeResp(404, text="nope"),
                "spotify.php": FakeResp(200, {"isrc": "GBAYE0601498", "spotify_url": ""}),
            }
        )
        r = si.SpotifyIsrcResolver(session=sess)
        assert r.resolve("4cOdK2wGLETKBW3PvgPWqT") == "GBAYE0601498"

    def test_resolve_never_raises_and_caches(self, isolated_cache):
        sess = MagicMock()
        sess.get.side_effect = RuntimeError("network down")
        r = si.SpotifyIsrcResolver(session=sess)
        assert r.resolve("4cOdK2wGLETKBW3PvgPWqT") is None
        # second call with a cached value short-circuits (no HTTP)
        r._isrc_cache.set("4cOdK2wGLETKBW3PvgPWqT", "USUM71703861")
        sess.get.reset_mock()
        assert r.resolve("4cOdK2wGLETKBW3PvgPWqT") == "USUM71703861"
        sess.get.assert_not_called()


# --------------------------------------------------------------------------- #
# Qobuz resolution + frontends (mocked HTTP)
# --------------------------------------------------------------------------- #
class TestQobuzResolution:
    def test_search_by_isrc_picks_highest_score(self):
        items = [
            {"id": 1, "title": "Other", "performer": {"name": "Z"}, "album": {"title": ""}},
            {
                "id": 2,
                "title": "Song",
                "performer": {"name": "Artist"},
                "album": {"title": "Alb"},
                "hires": True,
            },
        ]
        client = qobuz.QobuzClient(session=MagicMock())
        client._signed_get = MagicMock(return_value={"tracks": {"total": 2, "items": items}})
        best = client.search_by_isrc("USX", "Song", "Artist", "Alb")
        assert best["id"] == 2

    def test_wjhe_uses_location_header(self):
        sess = RouteSession(
            {
                "music.wjhe.top": FakeResp(302, headers={"Location": "https://cdn.wjhe/x.flac"}),
            }
        )
        client = qobuz.QobuzClient(session=sess)
        assert client._wjhe(42, "27") == "https://cdn.wjhe/x.flac"

    def test_extract_streaming_url_jsonp_and_escaped(self):
        client = qobuz.QobuzClient(session=MagicMock())
        body = 'cb({"data":{"url":"https:\\/\\/cdn.x\\/a.flac"}})'
        assert client._extract_streaming_url(body) == "https://cdn.x/a.flac"


# --------------------------------------------------------------------------- #
# (d) selectors for extended candidate mapping (Qobuz)
# --------------------------------------------------------------------------- #
class TestExtendedCandidateMapping:
    def test_qobuz_version_folded_into_title_for_keyword(self):
        # The Qobuz `version` field ("Extended Mix") must be visible to the
        # keyword filter, so the candidate title folds title + version.
        import track_selectors

        items = [
            {"id": 10, "title": "Song", "version": "Extended Mix", "duration": 360},
            {"id": 11, "title": "Song", "version": "", "duration": 200},
        ]
        cands = [
            {
                "id": it["id"],
                "title": f"{it['title']} {it.get('version') or ''}".strip(),
                "duration_s": it["duration"],
            }
            for it in items
        ]
        assert track_selectors.select_extended(cands, 200, 1200) == 10


# --------------------------------------------------------------------------- #
# Duration validation (worked examples)
# --------------------------------------------------------------------------- #
class TestValidate:
    def _patch_dur(self, monkeypatch, seconds):
        monkeypatch.setattr(validate, "get_audio_duration", lambda *_a, **_k: float(seconds))

    def test_preview_rejected(self, monkeypatch):
        self._patch_dur(monkeypatch, 20)
        ok, _ = validate.is_acceptable_duration("/x.flac", 200)
        assert ok is False

    def test_wrong_recording_rejected(self, monkeypatch):
        self._patch_dur(monkeypatch, 100)  # |100-200|=100 > 50
        ok, _ = validate.is_acceptable_duration("/x.flac", 200)
        assert ok is False

    def test_close_enough_accepted(self, monkeypatch):
        self._patch_dur(monkeypatch, 160)  # |160-200|=40 <= 50
        ok, _ = validate.is_acceptable_duration("/x.flac", 200)
        assert ok is True

    def test_round_half_away(self, monkeypatch):
        # expected=90 -> allowed = max(15, round(22.5)) = 23 (NOT 22, banker's).
        self._patch_dur(monkeypatch, 90 + 23)  # exactly at the boundary -> accepted (strict >)
        assert validate.is_acceptable_duration("/x.flac", 90)[0] is True
        self._patch_dur(monkeypatch, 90 + 24)
        assert validate.is_acceptable_duration("/x.flac", 90)[0] is False

    def test_extended_bypasses_guard(self, monkeypatch):
        self._patch_dur(monkeypatch, 600)  # way longer than expected
        ok, _ = validate.is_acceptable_duration("/x.flac", 200, extended=True)
        assert ok is True

    def test_undeterminable_accepted(self, monkeypatch):
        monkeypatch.setattr(validate, "get_audio_duration", lambda *_a, **_k: None)
        assert validate.is_acceptable_duration("/x.flac", 200)[0] is True


# --------------------------------------------------------------------------- #
# Service ordering
# --------------------------------------------------------------------------- #
class TestServiceOrder:
    def _backend(self, **cfg):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(download_source="lossless", **cfg)._backend

    def test_default_qobuz_amazon(self):
        assert self._backend()._service_order() == ["qobuz", "amazon"]

    def test_tidal_prepended_when_configured(self):
        b = self._backend(tidal_api_url="https://my.tidal/")
        assert b._service_order() == ["tidal", "qobuz", "amazon"]

    def test_tidal_dropped_without_instance(self):
        b = self._backend(lossless_service_order="tidal,qobuz,amazon")
        assert "tidal" not in b._service_order()

    def test_extended_forces_qobuz_first(self):
        # Qobuz is the only catalog-search service, so extended mode must try it
        # before Tidal/Amazon (which resolve the exact ISRC = radio edit).
        b = self._backend(tidal_api_url="https://my.tidal/")
        assert b._service_order(extended=False)[0] == "tidal"
        assert b._service_order(extended=True)[0] == "qobuz"


# --------------------------------------------------------------------------- #
# (e) Backend fetch: native .flac, correct actual_ext, NO ffmpeg re-encode
# --------------------------------------------------------------------------- #
class TestRealFlacBackendFetch:
    def _backend(self, **cfg):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(download_source="lossless", **cfg)._backend

    def _track(self, **kw):
        base = {
            "id": "4cOdK2wGLETKBW3PvgPWqT",
            "title": "Song",
            "artists": "Artist",
            "album": "Alb",
            "duration_ms": 200000,
            "isrc": None,
        }
        base.update(kw)
        return SimpleNamespace(**base)

    def test_qobuz_writes_native_flac_no_reencode(self, tmp_path):
        backend = self._backend()
        backend._isrc = MagicMock(resolve=lambda _id: "USUM71703861")
        backend._qobuz = MagicMock()
        backend._qobuz.search_by_isrc.return_value = {"id": 42}
        backend._qobuz.get_download_url.return_value = "https://cdn/x.flac"

        def fake_download(url, path, cancel):
            with open(path, "wb") as f:
                f.write(b"fLaC" + b"\x00" * 200)

        backend._download = fake_download
        dest = str(tmp_path / "out.mp3")
        with (
            patch("backends.real_flac.is_acceptable_duration", return_value=(True, "")),
            patch("subprocess.run") as mock_run,
        ):
            path, ext, used = backend.fetch(
                track=self._track(),
                destination=dest,
                extended=False,
                audio_format="flac",
                audio_quality="192",
                cancel=lambda: False,
            )
        assert ext == "flac"
        assert path.endswith(".flac")
        with open(path, "rb") as f:
            assert f.read(4) == b"fLaC"  # genuine FLAC magic
        assert used is False
        # The Qobuz path streams bytes — ffmpeg must NEVER be invoked to make FLAC.
        for call in mock_run.call_args_list:
            argv = call.args[0] if call.args else []
            assert not any("ffmpeg" in str(a) for a in argv)

    def test_qobuz_normal_records_provider_metadata(self, tmp_path):
        backend = self._backend()
        backend._isrc = MagicMock(resolve=lambda _id: "USUM71703861")
        backend._qobuz = MagicMock()
        backend._qobuz.search_by_isrc.return_value = {
            "id": 42,
            "title": "Song",
            "performer": {"name": "Artist"},
            "track_number": 4,
            "media_number": 2,
            "album": {"title": "The Album"},
        }
        backend._qobuz.get_download_url.return_value = "https://cdn/x.flac"

        def fake_dl(url, path, cancel):
            with open(path, "wb") as f:
                f.write(b"fLaC" + b"\x00" * 200)

        backend._download = fake_dl
        track = self._track()
        with patch("backends.real_flac.is_acceptable_duration", return_value=(True, "")):
            backend.fetch(
                track=track,
                destination=str(tmp_path / "o.mp3"),
                extended=False,
                audio_format="flac",
                audio_quality="192",
                cancel=lambda: False,
            )
        rec = backend.provider_metadata_for(track.id)
        assert rec["source"] == "qobuz"
        assert rec["meta"]["album"] == "The Album"
        assert rec["meta"]["trackNumber"] == 4
        assert rec["meta"]["discNumber"] == 2

    def test_qobuz_extended_records_provider_metadata(self, tmp_path):
        # Extended wins must ALSO record provider metadata (the full chosen item).
        backend = self._backend()
        backend._isrc = MagicMock(resolve=lambda _id: "USUM71703861")
        backend._qobuz = MagicMock()
        backend._qobuz.search.return_value = [
            {
                "id": 99,
                "title": "Song",
                "version": "Extended Mix",
                "duration": 400,
                "performer": {"name": "Artist"},
                "track_number": 1,
                "album": {"title": "Ext Single"},
            },
        ]
        backend._qobuz.get_download_url.return_value = "https://cdn/x.flac"

        def fake_dl(url, path, cancel):
            with open(path, "wb") as f:
                f.write(b"fLaC" + b"\x00" * 200)

        backend._download = fake_dl
        track = self._track()
        with (
            patch("backends.real_flac.is_acceptable_duration", return_value=(True, "")),
            patch("track_selectors.select_extended", return_value=99),
        ):
            _p, _e, used = backend.fetch(
                track=track,
                destination=str(tmp_path / "o.mp3"),
                extended=True,
                audio_format="flac",
                audio_quality="192",
                cancel=lambda: False,
            )
        assert used is True
        rec = backend.provider_metadata_for(track.id)
        assert rec["source"] == "qobuz"
        assert rec["meta"]["album"] == "Ext Single"

    def test_non_flac_bytes_rejected(self, tmp_path):
        backend = self._backend()
        backend._isrc = MagicMock(resolve=lambda _id: "USUM71703861")
        backend._qobuz = MagicMock()
        backend._qobuz.search_by_isrc.return_value = {"id": 42}
        backend._qobuz.get_download_url.return_value = "https://cdn/x.mp3"
        backend._youtube = MagicMock()
        backend._youtube.fetch.return_value = ("/yt.flac", "flac", False)

        def fake_download(url, path, cancel):
            with open(path, "wb") as f:
                f.write(b"ID3\x00fake-mp3")  # NOT FLAC

        backend._download = fake_download
        with patch("backends.real_flac.is_acceptable_duration", return_value=(True, "")):
            path, ext, used = backend.fetch(
                track=self._track(),
                destination=str(tmp_path / "o.mp3"),
                extended=False,
                audio_format="flac",
                audio_quality="192",
                cancel=lambda: False,
            )
        # All lossless services rejected the non-FLAC -> YouTube fallback used.
        backend._youtube.fetch.assert_called_once()
        assert (path, ext, used) == ("/yt.flac", "flac", False)

    def test_extended_uses_catalog_search(self, tmp_path):
        backend = self._backend()
        backend._isrc = MagicMock(resolve=lambda _id: "USUM71703861")
        backend._qobuz = MagicMock()
        # catalog search returns an extended cut (longer) + the radio edit
        backend._qobuz.search.return_value = [
            {"id": 10, "title": "Song", "version": "Extended Mix", "duration": 360},
            {"id": 11, "title": "Song", "version": "", "duration": 200},
        ]
        backend._qobuz.get_download_url.return_value = "https://cdn/x.flac"
        captured = {}

        def fake_download(url, path, cancel):
            captured["id"] = backend._qobuz.get_download_url.call_args.args[0]
            with open(path, "wb") as f:
                f.write(b"fLaC" + b"\x00" * 100)

        backend._download = fake_download
        with patch("backends.real_flac.is_acceptable_duration", return_value=(True, "")):
            path, ext, used = backend.fetch(
                track=self._track(),
                destination=str(tmp_path / "o.mp3"),
                extended=True,
                audio_format="flac",
                audio_quality="192",
                cancel=lambda: False,
            )
        assert used is True  # extended leg won
        assert captured["id"] == 10  # the longer, keyworded candidate
        backend._qobuz.search.assert_called()  # catalog search, not ISRC

    def test_all_services_fail_falls_back_to_youtube(self, tmp_path):
        backend = self._backend()
        backend._isrc = MagicMock(resolve=lambda _id: None)
        backend._qobuz = MagicMock()
        backend._qobuz.search_by_isrc.side_effect = qobuz.NotFoundOnServiceError("nope")
        backend._youtube = MagicMock()
        backend._youtube.fetch.return_value = ("/yt/out.flac", "flac", False)
        path, ext, used = backend.fetch(
            track=self._track(),
            destination=str(tmp_path / "o.mp3"),
            extended=False,
            audio_format="flac",
            audio_quality="192",
            cancel=lambda: False,
        )
        backend._youtube.fetch.assert_called_once()
        assert path == "/yt/out.flac"

    def test_fallback_toggle_off_skips_youtube(self, tmp_path):
        # Pure-lossless: when the toggle is off, a track with no lossless source
        # is failed/skipped rather than downloaded from YouTube.
        backend = self._backend(lossless_youtube_fallback=False)
        assert backend._youtube_fallback is False
        backend._isrc = MagicMock(resolve=lambda _id: None)
        backend._qobuz = MagicMock()
        backend._qobuz.search_by_isrc.side_effect = qobuz.NotFoundOnServiceError("nope")
        backend._youtube = MagicMock()
        with pytest.raises(qobuz.NotFoundOnServiceError):
            backend.fetch(
                track=self._track(),
                destination=str(tmp_path / "o.mp3"),
                extended=False,
                audio_format="flac",
                audio_quality="192",
                cancel=lambda: False,
            )
        backend._youtube.fetch.assert_not_called()

    def test_fallback_toggle_default_on(self):
        assert self._backend()._youtube_fallback is True

    def test_cancel_short_circuits(self, tmp_path):
        from lossless.errors import LosslessError

        backend = self._backend()
        backend._isrc = MagicMock(resolve=lambda _id: "USUM71703861")
        backend._qobuz = MagicMock()
        backend._youtube = MagicMock(fetch=MagicMock(return_value=("/yt.flac", "flac", False)))
        # Cancel before any service runs -> fetch raises, and we do NOT kick off a
        # Qobuz resolution OR an unwanted YouTube download for a stopped track.
        with pytest.raises(LosslessError):
            backend.fetch(
                track=self._track(isrc="USUM71703861"),
                destination=str(tmp_path / "o.mp3"),
                extended=False,
                audio_format="flac",
                audio_quality="192",
                cancel=lambda: True,
            )
        backend._qobuz.search_by_isrc.assert_not_called()
        backend._youtube.fetch.assert_not_called()


# --------------------------------------------------------------------------- #
# Bridge helpers
# --------------------------------------------------------------------------- #
class TestBridge:
    def test_amazon_url_canonicalization(self):
        assert bridge.normalize_amazon_music_url("https://x/tracks/B01ABCDEFG") == (
            "https://music.amazon.com/tracks/B01ABCDEFG?musicTerritory=US"
        )
        assert bridge.normalize_amazon_music_url("https://nope") == ""

    def test_tidal_id_extraction(self):
        assert (
            bridge.extract_tidal_track_id("https://listen.tidal.com/track/12345678?u=1") == 12345678
        )
        assert bridge.extract_tidal_track_id("https://x/album/9") is None


class TestTidalDash:
    """The DASH parser must surface the codec so the lossless guard can't
    false-pass a lossy stream relying on the AdaptationSet-level template."""

    def _mpd(self, codecs):
        return (
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>'
            f'<AdaptationSet mimeType="audio/mp4" codecs="{codecs}">'
            '<SegmentTemplate initialization="init.mp4" media="seg_$Number$.mp4">'
            '<SegmentTimeline><S d="100" r="2"/></SegmentTimeline></SegmentTemplate>'
            f'<Representation id="A" bandwidth="1411000" codecs="{codecs}"/>'
            "</AdaptationSet></Period></MPD>"
        )

    def _parse(self, codecs):
        from lossless.tidal import TidalClient

        client = TidalClient.__new__(TidalClient)
        return client._parse_dash(self._mpd(codecs))

    def test_flac_dash_segments_and_mime(self):
        init_url, media_urls, mime = self._parse("flac")
        assert init_url == "init.mp4"
        assert media_urls == ["seg_1.mp4", "seg_2.mp4", "seg_3.mp4"]  # Σ(r+1)=3
        assert "flac" in mime.lower()  # passes the lossless guard

    def test_lossy_dash_codec_surfaced(self):
        # A rep relying on the AdaptationSet template + lossy codecs must NOT
        # produce an empty mime (which would false-pass the lossless guard).
        _init, _media, mime = self._parse("mp4a.40.2")
        assert "mp4a" in mime and "flac" not in mime.lower()


# --------------------------------------------------------------------------- #
# ffmpeg remux/decrypt must drop MP4 container junk tags (major_brand,
# compatible_brands=mp41dash, encoder=Lavf...) so they never leak into the FLAC.
# -map_metadata -1 -bitexact, with -c copy keeping the audio bit-identical.
# --------------------------------------------------------------------------- #
class TestRemuxStripsContainerTags:
    @staticmethod
    def _assert_strips(argv):
        assert "-c" in argv and "copy" in argv  # still a pure remux (no re-encode)
        assert "-map_metadata" in argv
        assert argv[argv.index("-map_metadata") + 1] == "-1"
        assert "-bitexact" in argv

    def test_tidal_dash_remux_argv(self):
        from lossless.tidal import TidalClient

        mpd = (
            '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011"><Period>'
            '<AdaptationSet mimeType="audio/mp4" codecs="flac">'
            '<SegmentTemplate initialization="init.mp4" media="seg_$Number$.mp4">'
            '<SegmentTimeline><S d="100" r="2"/></SegmentTimeline></SegmentTemplate>'
            '<Representation id="A" bandwidth="1411000" codecs="flac"/>'
            "</AdaptationSet></Period></MPD>"
        )
        client = TidalClient(MagicMock(), "http://127.0.0.1:8000", ffmpeg="/usr/bin/ffmpeg")
        client._download = MagicMock()  # don't actually fetch segments
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr=b"")) as mock_run:
            client._from_dash(mpd, "/tmp/out.flac", "LOSSLESS", lambda: False, 0)
        self._assert_strips(mock_run.call_args.args[0])

    def test_amazon_decrypt_argv(self):
        from lossless.amazon import AmazonClient

        c = AmazonClient(MagicMock(), ffmpeg="/usr/bin/ffmpeg", ffprobe="/usr/bin/ffprobe")
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr=b"")) as mock_run:
            c._decrypt_copy("/tmp/enc.mp4", "deadbeef", "/tmp/out.flac")
        argv = mock_run.call_args.args[0]
        self._assert_strips(argv)
        assert "-decryption_key" in argv  # decrypt path preserved


# --------------------------------------------------------------------------- #
# Tidal instance URL normalization (shared helper used by load_config, the
# settings panel, and the backend)
# --------------------------------------------------------------------------- #
class TestTidalUrlNormalization:
    """https:// for any host; plain http:// for loopback only (a self-hosted
    instance on this machine must not need a TLS proxy, but credentials-bearing
    traffic must never go plaintext to a remote host)."""

    def _norm(self, value):
        from lossless._util import normalize_tidal_api_url

        return normalize_tidal_api_url(value)

    def test_https_accepted_and_slash_stripped(self):
        assert self._norm(" https://my.tidal/ ") == "https://my.tidal"

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://[::1]:8000",
            "http://localhost",
            "HTTP://LOCALHOST:8000",
        ],
    )
    def test_loopback_http_accepted(self, url):
        assert self._norm(url + "/") == url

    @pytest.mark.parametrize(
        "url",
        [
            "http://evil.example:8000",  # remote http: plaintext credentials
            "http://192.168.1.50:8000",  # LAN is still not loopback
            "http://localhost.evil.example",  # prefix-spoofed host
            "http://localhost:8000/api",  # path suffix: instances serve at root
            "ftp://localhost",
            "localhost:8000",
            "",
            # SSRF / host-spoofing shapes that a naive scheme check would let
            # through — every one must resolve to "" (no plaintext to a non-
            # loopback host).
            "http://127.0.0.1@evil.example",  # userinfo smuggling
            "http://evil.example#@localhost",  # fragment-after-host trick
            "http://evil.example/127.0.0.1",  # loopback only in the path
            "http://0x7f.0.0.1",  # hex-encoded 127.x
            "http://127.1",  # short-form loopback (not the exact literal)
            "http://2130706433",  # decimal-encoded 127.0.0.1
            "http://[::1]%25eth0",  # IPv6 zone id
            "http://localhost:8000/../x",  # path traversal suffix
            "http://localhost:99999",  # out-of-range port
            "http://localhost:0",  # port 0 is invalid
            "http://localhost:8000\nHost: evil.example",  # header-injection shape
        ],
    )
    def test_everything_else_rejected(self, url):
        assert self._norm(url) == ""

    def test_backend_uses_shared_helper(self):
        from backends.real_flac import RealFlacBackend

        assert RealFlacBackend._normalize_tidal_url("http://127.0.0.1:8000/") == (
            "http://127.0.0.1:8000"
        )
        assert RealFlacBackend._normalize_tidal_url("http://evil.example") == ""


# --------------------------------------------------------------------------- #
# Tidal v2 response: manifest under data.manifest OR at the top level
# --------------------------------------------------------------------------- #
class TestTidalManifestNesting:
    """Some instances return {"data": {"manifest": ...}}, others put the
    manifest at the top level — _fetch_one must accept both."""

    def _client_with_body(self, body):
        import base64

        from lossless.tidal import TidalClient

        bts = json.dumps(
            {"mimeType": "audio/flac", "codecs": "flac", "urls": ["https://cdn/x.flac"]}
        )
        manifest = base64.b64encode(bts.encode()).decode()
        session = MagicMock()
        session.get.return_value = FakeResp(json_data=body(manifest))
        client = TidalClient(session, "http://127.0.0.1:8000", ffmpeg=None)
        client._download = MagicMock()
        return client

    def _fetch(self, body):
        client = self._client_with_body(body)
        out = client._fetch_one(123, "/tmp/x.flac", "LOSSLESS", lambda: False, 0)
        client._download.assert_called_once()
        assert client._download.call_args.args[:2] == ("https://cdn/x.flac", "/tmp/x.flac")
        return out

    def test_nested_data_manifest(self):
        assert self._fetch(lambda m: {"data": {"manifest": m}}) == "/tmp/x.flac"

    def test_top_level_manifest(self):
        assert self._fetch(lambda m: {"manifest": m}) == "/tmp/x.flac"

    def test_non_dict_data_falls_back_to_top_level(self):
        assert self._fetch(lambda m: {"data": "queued", "manifest": m}) == "/tmp/x.flac"

    def test_legacy_original_track_url_array_still_works(self):
        # The legacy [{"OriginalTrackUrl": ...}] array branch must keep working
        # alongside the new top-level-manifest support.
        from lossless.tidal import TidalClient

        session = MagicMock()
        session.get.return_value = FakeResp(
            json_data=[{"OriginalTrackUrl": ""}, {"OriginalTrackUrl": "https://cdn/y.flac"}]
        )
        client = TidalClient(session, "http://127.0.0.1:8000", ffmpeg=None)
        client._download = MagicMock()
        out = client._fetch_one(7, "/tmp/y.flac", "LOSSLESS", lambda: False, 0)
        assert out == "/tmp/y.flac"
        client._download.assert_called_once()
        assert client._download.call_args.args[:2] == ("https://cdn/y.flac", "/tmp/y.flac")

    def test_loopback_http_bypasses_env_proxy(self):
        # The manifest call to a loopback instance must disable proxies so it
        # can't be routed off-box via HTTP_PROXY; a remote https instance keeps
        # the session/env default (proxies=None).
        from lossless.tidal import TidalClient

        loop = TidalClient(MagicMock(), "http://127.0.0.1:8000", ffmpeg=None)
        assert loop._manifest_proxies == {"http": None, "https": None}
        remote = TidalClient(MagicMock(), "https://my.tidal", ffmpeg=None)
        assert remote._manifest_proxies is None
        # Confirm the value is actually threaded into the request.
        client = self._client_with_body(lambda m: {"data": {"manifest": m}})
        client._fetch_one(1, "/tmp/z.flac", "LOSSLESS", lambda: False, 0)
        assert client._session.get.call_args.kwargs.get("proxies") == {"http": None, "https": None}


# --------------------------------------------------------------------------- #
# Instance-native Tidal id resolution (the fix for Songlink-bridge drop-outs):
# resolve the track id from the instance's own /search/ — ISRC first, then
# title+artist — instead of the rate-limited public Deezer->Songlink bridge.
# --------------------------------------------------------------------------- #
class TestTidalFindTrackId:
    def _client(self, search_by_param):
        """search_by_param: {"i": items_list, "s": items_list} -> dispatched by
        the query param present on each session.get call."""
        from lossless.tidal import TidalClient

        session = MagicMock()

        def fake_get(url, params=None, **kw):
            params = params or {}
            key = "i" if "i" in params else "s" if "s" in params else None
            return FakeResp(json_data={"data": {"items": search_by_param.get(key, [])}})

        session.get.side_effect = fake_get
        return TidalClient(session, "http://127.0.0.1:8000", ffmpeg=None)

    @staticmethod
    def _track(
        tid, title="Alone", artist="Jan Blomqvist", quality="LOSSLESS", stream=True, allow=True
    ):
        return {
            "id": tid,
            "title": title,
            "audioQuality": quality,
            "streamReady": stream,
            "allowStreaming": allow,
            "artists": [{"name": artist}],
        }

    def test_isrc_search_wins_and_is_trusted(self):
        # ISRC pins the recording: first streamable lossless item is returned,
        # no title/artist match required.
        c = self._client({"i": [self._track(456640584, title="whatever", artist="x")]})
        assert (
            c.find_track_id(isrc="NLF712301168", title="Alone", artists="Jan Blomqvist")
            == 456640584
        )
        # only the ISRC endpoint was needed
        assert c._session.get.call_count == 1

    def test_prefers_lossless_over_lossy(self):
        items = [self._track(1, quality="HIGH"), self._track(2, quality="LOSSLESS")]
        c = self._client({"i": items})
        assert c.find_track_id(isrc="X") == 2

    def test_isrc_ranks_by_popularity_to_pick_canonical_release(self):
        # One ISRC -> many releases (single, compilation, mini-mix). The most
        # popular (the canonical single) must win, not the first listed.
        mini = {**self._track(456640584, title="Alone"), "popularity": 16, "trackNumber": 7}
        comp = {**self._track(289026555, title="Alone"), "popularity": 14, "trackNumber": 5}
        single = {**self._track(279446425, title="Alone"), "popularity": 41, "trackNumber": 1}
        c = self._client({"i": [mini, comp, single]})  # single is listed LAST
        assert c.find_track_id(isrc="NLF712301168") == 279446425

    def test_isrc_tie_breaks_to_lower_track_number(self):
        a = {**self._track(10, title="Alone"), "popularity": 5, "trackNumber": 9}
        b = {**self._track(11, title="Alone"), "popularity": 5, "trackNumber": 1}
        c = self._client({"i": [a, b]})
        assert c.find_track_id(isrc="X") == 11

    def test_skips_non_streamable(self):
        items = [
            self._track(1, allow=False),
            self._track(2, stream=False),
            self._track(3, quality="LOSSLESS"),
        ]
        c = self._client({"i": items})
        assert c.find_track_id(isrc="X") == 3

    def test_falls_back_to_text_search_when_isrc_empty(self):
        c = self._client({"i": [], "s": [self._track(279446425)]})
        assert c.find_track_id(isrc="NOPE", title="Alone", artists="Jan Blomqvist") == 279446425

    def test_no_isrc_uses_text_search(self):
        c = self._client({"s": [self._track(279446425)]})
        assert c.find_track_id(title="Alone", artists="Jan Blomqvist") == 279446425

    def test_text_search_rejects_wrong_title_and_artist(self):
        # A relevance hit that is the wrong song (or wrong artist) must NOT match.
        items = [self._track(99, title="Alone Pt II", artist="Someone Else")]
        c = self._client({"s": items})
        assert c.find_track_id(title="Alone", artists="Jan Blomqvist") is None

    def test_text_search_matches_primary_artist_with_features(self):
        # Spotify "Jan Blomqvist, Malou" must still match a Tidal entry credited
        # to the primary artist.
        items = [self._track(7, title="Alone", artist="Jan Blomqvist")]
        c = self._client({"s": items})
        assert c.find_track_id(title="Alone", artists="Jan Blomqvist, Malou") == 7

    def test_text_search_query_strips_editorial_cruft(self):
        # Editorial/promo titles ("[ANR092] **Highest New Entry ...**") must be
        # cleaned in the QUERY so Tidal returns the real track; the original
        # title is still used for the match check.
        from lossless.tidal import TidalClient

        seen = {}
        session = MagicMock()

        def fake_get(url, params=None, **kw):
            params = params or {}
            if "s" in params:
                seen["q"] = params["s"]
                return FakeResp(
                    json_data={"data": {"items": [self._track(56013566, title="More")]}}
                )
            return FakeResp(json_data={"data": {"items": []}})

        session.get.side_effect = fake_get
        c = TidalClient(session, "http://127.0.0.1:8000", ffmpeg=None)
        tid = c.find_track_id(
            title="More [ANR092] **Highest New Entry - Armada Stream 40**",
            artists="Jan Blomqvist, Elena Pitoulis",
        )
        assert tid == 56013566
        assert "[" not in seen["q"] and "*" not in seen["q"]
        assert "More" in seen["q"]  # the real title survives the cleaning

    def test_clean_search_title_helper(self):
        from lossless.tidal import _clean_search_title

        assert _clean_search_title("More [ANR092] **Highest New Entry**") == "More"
        assert _clean_search_title("Alone") == "Alone"
        assert _clean_search_title("Song (Extended Mix)") == "Song (Extended Mix)"

    def test_returns_none_when_nothing_found(self):
        c = self._client({"i": [], "s": []})
        assert c.find_track_id(isrc="X", title="Alone", artists="Jan Blomqvist") is None

    def test_empty_api_url_returns_none_without_calling(self):
        from lossless.tidal import TidalClient

        session = MagicMock()
        c = TidalClient(session, "", ffmpeg=None)
        assert c.find_track_id(isrc="X", title="Alone", artists="Jan Blomqvist") is None
        session.get.assert_not_called()


class TestTidalFetchPrefersInstanceSearch:
    """_tidal_fetch must resolve via the instance search and only touch the
    Songlink bridge when the instance finds nothing."""

    def _backend(self, **cfg):
        from Spotify_Downloader import MusicScraper

        return MusicScraper(
            download_source="lossless", tidal_api_url="http://127.0.0.1:8000", **cfg
        )._backend

    def _track(self):
        return SimpleNamespace(
            id="abc",
            title="Alone",
            artists="Jan Blomqvist",
            album="Disconnect",
            duration_ms=200000,
            isrc=None,
        )

    def test_uses_instance_id_and_skips_bridge(self, tmp_path):
        b = self._backend()
        # Tidal client resolves directly; bridge must NOT be consulted.
        b._tidal = MagicMock()
        b._tidal.find_track_id.return_value = 279446425
        b._tidal.fetch_flac.return_value = str(tmp_path / "out.flac")
        b._bridge_links = MagicMock(side_effect=AssertionError("bridge must not be called"))
        with patch("backends.real_flac.is_acceptable_duration", return_value=(True, "")):
            (tmp_path / "out.flac").write_bytes(b"fLaC" + b"\x00" * 64)
            path, ext, used = b._tidal_fetch(
                self._track(), "NLF712301168", str(tmp_path / "out.mp3"), 200, False, lambda: False
            )
        b._tidal.find_track_id.assert_called_once()
        b._tidal.fetch_flac.assert_called_once()
        assert b._tidal.fetch_flac.call_args.args[0] == 279446425
        assert ext == "flac"

    def test_falls_back_to_bridge_when_instance_misses(self, tmp_path):
        b = self._backend()
        b._tidal = MagicMock()
        b._tidal.find_track_id.return_value = None  # instance found nothing
        b._tidal.fetch_flac.return_value = str(tmp_path / "out.flac")
        b._bridge_links = MagicMock(return_value={"tidal_url": "https://tidal.com/track/123?u"})
        with patch("backends.real_flac.is_acceptable_duration", return_value=(True, "")):
            (tmp_path / "out.flac").write_bytes(b"fLaC" + b"\x00" * 64)
            b._tidal_fetch(
                self._track(), "ISRC", str(tmp_path / "out.mp3"), 200, False, lambda: False
            )
        b._bridge_links.assert_called_once()
        assert b._tidal.fetch_flac.call_args.args[0] == 123
