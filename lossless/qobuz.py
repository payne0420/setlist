"""Qobuz path — the highest-priority lossless service (no user config needed).

Ports SpotiFLAC's ``qobuz.go`` + ``qobuz_api.go``:
  * a signed (MD5) metadata API that resolves a Spotify track (by ISRC, or by a
    title+artist catalog search for extended mode) to a Qobuz integer track id;
  * three unofficial CDN frontends (WJHE → GDStudio.xyz → GDStudio.org →
    MusicDL-last) that turn a Qobuz track id into a temporary ``.flac`` URL.

The signature is load-bearing — a one-character deviation silently breaks every
signed call — so it is reproduced byte-for-byte and pinned by a test vector
(``md5("tracksearchlimit1queryUSUM..." ) == 80f86ad7...``).
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
from typing import Any

import requests

from . import constants as C
from ._util import aesgcm_decrypt, default_headers
from .errors import NotFoundOnServiceError, ServiceUnavailableError

# Verbatim from qobuz.go: a permissive URL grabber for the frontends' bodies.
_STREAM_URL_RE = re.compile(r"""https?://[^\s"'<>\\)]+""")
# Replacer pairs for normalizeQobuzSearchValue (single-pass, argument order).
_NORM_PAIRS = (("&", " and "), ("feat.", " "), ("ft.", " "), ("/", " "), ("-", " "), ("_", " "))

# Body read caps (qobuz.go) — kept so a hostile/huge body can't exhaust memory.
_WJHE_BODY_CAP = 131072
_GDSTUDIO_BODY_CAP = 262144


# --------------------------------------------------------------------------- #
# Signature (qobuz_api.go) — byte-for-byte
# --------------------------------------------------------------------------- #
def _normalized_path(path: str) -> str:
    """Trim whitespace then leading/trailing '/' (URL form keeps inner slashes)."""
    return path.strip().strip("/")


def signature_payload(path: str, params: dict[str, list[str]], timestamp: str, secret: str) -> str:
    """The exact string MD5'd to form ``request_sig``.

    normalizedPath has ALL '/' removed ("track/search" -> "tracksearch"); then,
    for params sorted by key ascending and EXCLUDING app_id/request_ts/request_sig,
    each value is appended as ``key+value`` (a multi-value key repeats per value);
    then ``request_ts`` (unix seconds) then ``app_secret``.
    """
    normalized = _normalized_path(path).replace("/", "")
    parts = [normalized]
    for k in sorted(k for k in params if k not in ("app_id", "request_ts", "request_sig")):
        vals = params[k]
        if not vals:
            parts.append(k)
        else:
            for v in vals:
                parts.append(k + v)
    parts.append(timestamp)
    parts.append(secret)
    return "".join(parts)


def request_signature(path: str, params: dict[str, list[str]], timestamp: str, secret: str) -> str:
    return hashlib.md5(signature_payload(path, params, timestamp, secret).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Credentials (qobuz_api.go) — embedded default + opportunistic scrape on 400/401
# --------------------------------------------------------------------------- #
_BUNDLE_SRC_RE = re.compile(r'<script[^>]+src="([^"]+/js/main\.js|/resources/[^"]+/js/main\.js)"')
_BUNDLE_CREDS_RE = re.compile(r'app_id:"(\d{9})",app_secret:"([a-f0-9]{32})"')


class QobuzCredentials:
    """Holds the Qobuz ``app_id``/``app_secret``.

    Defaults to the embedded snapshot (guaranteed-valid today). Only scrapes
    fresh credentials from open.qobuz.com when a signed call rejects the current
    pair with HTTP 400/401 (``get(force_refresh=True)``) — exactly SpotiFLAC's
    ``qobuzShouldRefreshCredentials`` contract, minus the eager periodic scrape.
    """

    def __init__(self, session: requests.Session) -> None:
        self._session = session
        self._app_id = C.QOBUZ_DEFAULT_APP_ID
        self._app_secret = C.QOBUZ_DEFAULT_APP_SECRET

    def get(self, force_refresh: bool = False) -> tuple[str, str]:
        if force_refresh:
            scraped = self._scrape()
            if scraped:
                self._app_id, self._app_secret = scraped
        return self._app_id, self._app_secret

    def _scrape(self) -> tuple[str, str] | None:
        try:
            headers = default_headers()
            resp = self._session.get(C.QOBUZ_OPEN_TRACK_PROBE_URL, headers=headers, timeout=30)
            if resp.status_code != 200:
                return None
            m = _BUNDLE_SRC_RE.search(resp.text)
            if not m:
                return None
            bundle_url = m.group(1)
            if bundle_url.startswith("/"):
                bundle_url = "https://open.qobuz.com" + bundle_url
            bundle = self._session.get(bundle_url, headers=headers, timeout=30)
            if bundle.status_code != 200:
                return None
            cm = _BUNDLE_CREDS_RE.search(bundle.text)
            if not cm:
                return None
            return cm.group(1), cm.group(2)
        except requests.RequestException:
            return None


# --------------------------------------------------------------------------- #
# Scoring (qobuz.go)
# --------------------------------------------------------------------------- #
def _go_replace(s: str, pairs: tuple[tuple[str, str], ...]) -> str:
    """Single-pass, non-overlapping, argument-order replace (strings.NewReplacer)."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        for old, new in pairs:
            if old and s.startswith(old, i):
                out.append(new)
                i += len(old)
                break
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def normalize_search_value(value: str) -> str:
    normalized = (value or "").strip().lower()
    normalized = _go_replace(normalized, _NORM_PAIRS)
    return " ".join(normalized.split())


def _display_artist(track: dict) -> str:
    for v in (
        (track.get("performer") or {}).get("name"),
        ((track.get("album") or {}).get("artist") or {}).get("name"),
    ):
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _supports_hires(track: dict) -> bool:
    if track.get("hires") or track.get("hires_streamable"):
        return True
    return (track.get("maximum_bit_depth") or 0) >= 24 or (
        track.get("maximum_sampling_rate") or 0
    ) > 48


def score_candidate(track: dict, sp_title: str, sp_artist: str, sp_album: str) -> int:
    score = 0
    t_n, t_h = normalize_search_value(sp_title), normalize_search_value(track.get("title") or "")
    if t_n and t_h == t_n:
        score += 1000
    elif t_n and (t_n in t_h or t_h in t_n):
        score += 500
    a_n, a_h = normalize_search_value(sp_artist), normalize_search_value(_display_artist(track))
    if a_n and a_h == a_n:
        score += 300
    elif a_n and a_h and (a_n in a_h or a_h in a_n):
        score += 180
    b_n, b_h = (
        normalize_search_value(sp_album),
        normalize_search_value((track.get("album") or {}).get("title") or ""),
    )
    if b_n and b_h == b_n:
        score += 150
    elif b_n and b_h and (b_n in b_h or b_h in b_n):
        score += 90
    if _supports_hires(track):
        score += 40
    elif (track.get("maximum_bit_depth") or 0) >= 16:
        score += 20
    return score


def _looks_streamable(raw: str) -> bool:
    raw = (raw or "").strip()
    if not raw:
        return False
    try:
        u = urllib.parse.urlparse(raw)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class QobuzClient:
    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._creds = QobuzCredentials(self._session)
        self._musicdl_key: str | None = None

    # -- signed API --------------------------------------------------------- #
    def _signed_get(self, path: str, params: dict[str, str], timeout: int = 20) -> dict:
        list_params = {k: [v] for k, v in params.items()}
        for attempt in range(2):
            app_id, app_secret = self._creds.get(force_refresh=(attempt == 1))
            ts = str(int(time.time()))
            sig = request_signature(_normalized_path(path), list_params, ts, app_secret)
            query = dict(params)
            query.update({"app_id": app_id, "request_ts": ts, "request_sig": sig})
            url = f"{C.QOBUZ_API_BASE_URL}/{_normalized_path(path)}?" + urllib.parse.urlencode(
                sorted(query.items())
            )
            headers = {
                "User-Agent": C.DEFAULT_DOWNLOADER_USER_AGENT,
                "Accept": "application/json",
                "X-App-Id": app_id,
            }
            try:
                resp = self._session.get(url, headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                raise ServiceUnavailableError(f"Qobuz API request failed: {exc}") from exc
            if resp.status_code in (400, 401) and attempt == 0:
                continue  # stale creds -> scrape fresh + retry once
            if resp.status_code != 200:
                raise ServiceUnavailableError(f"Qobuz request failed: HTTP {resp.status_code}")
            try:
                return resp.json()
            except ValueError as exc:
                raise ServiceUnavailableError(f"Qobuz returned invalid JSON: {exc}") from exc
        raise ServiceUnavailableError("Qobuz request failed after credential refresh")

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Raw ``track/search`` items for *query* (used by extended catalog search)."""
        resp = self._signed_get("track/search", {"query": query.strip(), "limit": str(limit)})
        tracks = resp.get("tracks") or {}
        return list(tracks.get("items") or [])

    def search_by_isrc(self, isrc: str, title: str = "", artist: str = "", album: str = "") -> dict:
        """Resolve a track to the best-scoring Qobuz ``QobuzTrack`` dict.

        Tries the ISRC query first, then a ``"<title> <artist>"`` query; the first
        query that returns any items decides the result (highest score wins, first
        max on ties). Raises :class:`NotFoundOnServiceError` if nothing matches.
        """
        queries = [isrc.strip()]
        secondary = f"{title} {artist}".strip()
        if secondary:
            queries.append(secondary)
        last_err = ""
        for q in queries:
            if not q:
                continue
            try:
                items = self.search(q, limit=10)
            except ServiceUnavailableError as exc:
                last_err = str(exc)
                continue
            if not items:
                last_err = f"track not found for query: {q}"
                continue
            best, best_score = items[0], None
            for idx, item in enumerate(items):
                sc = score_candidate(item, title, artist, album)
                if idx == 0 or sc > best_score:
                    best, best_score = item, sc
            return best
        raise NotFoundOnServiceError(last_err or f"track not found for ISRC: {isrc}")

    # -- download URL resolution ------------------------------------------- #
    def get_download_url(
        self, track_id: int, quality: str = "27", allow_fallback: bool = True
    ) -> str:
        """Resolve a Qobuz track id to a temporary CDN ``.flac`` URL.

        Runs all frontends in order (MusicDL always last) at the requested
        quality, then walks the one-directional 27→7→6 fallback ladder.
        """
        quality = (quality or "").strip()
        if quality in ("", "5"):
            quality = "6"

        url = self._run_frontends(track_id, quality)
        if url:
            return url
        if allow_fallback:
            if quality == "27":
                url = self._run_frontends(track_id, "7")
                if url:
                    return url
                quality = "7"
            if quality == "7":
                url = self._run_frontends(track_id, "6")
                if url:
                    return url
        raise NotFoundOnServiceError("all Qobuz frontends and quality fallbacks failed")

    def _run_frontends(self, track_id: int, quality: str) -> str | None:
        # WJHE -> GDStudio.xyz -> GDStudio.org -> MusicDL (pinned last).
        attempts = (
            lambda: self._wjhe(track_id, quality),
            lambda: self._gdstudio(track_id, quality, C.QOBUZ_GDSTUDIO_API_URL_XYZ),
            lambda: self._gdstudio(track_id, quality, C.QOBUZ_GDSTUDIO_API_URL_ORG),
            lambda: self._musicdl(track_id, quality),
        )
        for attempt in attempts:
            try:
                url = attempt()
                if url and _looks_streamable(url):
                    return url
            except (requests.RequestException, ServiceUnavailableError, ValueError):
                continue
        return None

    # -- WJHE --------------------------------------------------------------- #
    @staticmethod
    def _wjhe_quality(quality: str) -> tuple[int, str]:
        q = quality.strip()
        if q in ("27", "7"):
            return 2000, "flac"
        if q in ("", "6"):
            return 1000, "flac"
        return 320, "mp3"

    def _wjhe(self, track_id: int, quality: str) -> str | None:
        wq, wf = self._wjhe_quality(quality)
        params = {"ID": str(track_id), "quality": str(wq), "format": wf}
        url = C.QOBUZ_WJHE_STREAM_API_URL + "?" + urllib.parse.urlencode(sorted(params.items()))
        headers = default_headers()
        resp = self._session.head(url, headers=headers, timeout=20, allow_redirects=False)
        if resp.status_code in (405, 501):
            resp = self._session.get(
                url, headers=headers, timeout=20, allow_redirects=False, stream=True
            )
        loc = (resp.headers.get("Location") or "").strip()
        if _looks_streamable(loc):
            return loc
        body = resp.content[:_WJHE_BODY_CAP] if resp.content else b""
        return self._extract_streaming_url(body)

    # -- GDStudio ----------------------------------------------------------- #
    @staticmethod
    def _gdstudio_padded_version() -> str:
        parts = [p.strip() for p in C.QOBUZ_GDSTUDIO_VERSION.split(".")]
        return "".join(("0" + p) if len(p) == 1 else p for p in parts)

    @staticmethod
    def _gdstudio_escaped_value(value: str) -> str:
        return urllib.parse.quote_plus(value.strip()).replace("+", "%20")

    @staticmethod
    def _gdstudio_bitrate(quality: str) -> str:
        q = quality.strip()
        if q in ("27", "7"):
            return "999"
        if q in ("", "6"):
            return "740"
        return "320"

    def _gdstudio_ts9(self, host: str) -> str:
        fallback = str(int(time.time() * 1000))[:9]
        try:
            resp = self._session.get(f"https://{host}/time", headers=default_headers(), timeout=10)
            ts = resp.content[:64].decode(errors="ignore").strip()
            return ts[:9] if len(ts) >= 9 else fallback
        except requests.RequestException:
            return fallback

    def _gdstudio(self, track_id: int, quality: str, api_url: str) -> str | None:
        host = urllib.parse.urlparse(api_url).netloc
        if not host:
            return None
        track_id_str = str(track_id)
        ts9 = self._gdstudio_ts9(host)
        base = f"{host}|{self._gdstudio_padded_version()}|{ts9}|{self._gdstudio_escaped_value(track_id_str)}"
        sig = hashlib.md5(base.encode()).hexdigest()[-8:].upper()
        form = {
            "types": "url",
            "id": track_id_str,
            "source": "qobuz",
            "br": self._gdstudio_bitrate(quality),
            "s": sig,
        }
        headers = default_headers(
            {
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": f"https://{host}",
                "Referer": f"https://{host}/",
            }
        )
        resp = self._session.post(
            api_url, data=urllib.parse.urlencode(sorted(form.items())), headers=headers, timeout=60
        )
        if resp.status_code != 200:
            return None
        return self._extract_streaming_url(resp.content[:_GDSTUDIO_BODY_CAP])

    # -- MusicDL ------------------------------------------------------------ #
    def _get_musicdl_key(self) -> str:
        if self._musicdl_key is None:
            self._musicdl_key = aesgcm_decrypt(
                C.QOBUZ_MUSICDL_DEBUG_KEY_SEED_PARTS,
                C.QOBUZ_MUSICDL_DEBUG_KEY_NONCE,
                C.QOBUZ_MUSICDL_DEBUG_KEY_CIPHERTEXT,
                C.QOBUZ_MUSICDL_DEBUG_KEY_TAG,
                C.QOBUZ_MUSICDL_DEBUG_KEY_AAD,
            ).decode()
        return self._musicdl_key

    def _musicdl(self, track_id: int, quality: str) -> str | None:
        q = quality.strip() or "6"
        body = {"url": f"https://open.qobuz.com/track/{track_id}", "quality": q}
        headers = default_headers(
            {"Content-Type": "application/json", "X-Debug-Key": self._get_musicdl_key()}
        )
        resp = self._session.post(
            C.QOBUZ_MUSICDL_DOWNLOAD_API_URL, json=body, headers=headers, timeout=60
        )
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not data.get("success"):
            return None
        return (data.get("download_url") or "").strip() or None

    # -- shared stream-URL extraction (qobuz.go) ---------------------------- #
    def _extract_streaming_url(self, body: bytes | str) -> str | None:
        text = body.decode(errors="ignore") if isinstance(body, bytes) else body
        trimmed = (text or "").strip()
        if not trimmed:
            return None

        # 1. Typed probe in exact order.
        try:
            data = __import__("json").loads(trimmed)
        except ValueError:
            data = None
        if isinstance(data, dict):
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            for cand in (
                data.get("download_url"),
                data.get("url"),
                inner.get("download_url"),
                inner.get("url"),
            ):
                if isinstance(cand, str) and _looks_streamable(cand):
                    return cand
            # 2. Generic recursive walk (priority keys first).
            hit = self._walk_for_url(data)
            if hit:
                return hit
        elif data is not None:
            hit = self._walk_for_url(data)
            if hit:
                return hit

        # 3. JSONP unwrap: callback( ... )
        open_idx, close_idx = trimmed.find("("), trimmed.rfind(")")
        if open_idx >= 0 and close_idx > open_idx + 1:
            inner = trimmed[open_idx + 1 : close_idx].strip()
            if inner and inner != trimmed:
                hit = self._extract_streaming_url(inner)
                if hit:
                    return hit

        # 4. Regex fallback.
        for match in _STREAM_URL_RE.findall(trimmed):
            candidate = match.replace("\\/", "/")
            if _looks_streamable(candidate):
                return candidate
        return None

    _PRIORITY_KEYS = ("download_url", "url", "play_url", "stream_url", "link", "file")

    def _walk_for_url(self, node: Any) -> str | None:
        if isinstance(node, str):
            candidate = node.replace("\\/", "/")
            return candidate if _looks_streamable(candidate) else None
        if isinstance(node, list):
            for item in node:
                hit = self._walk_for_url(item)
                if hit:
                    return hit
            return None
        if isinstance(node, dict):
            for key in self._PRIORITY_KEYS:
                if key in node:
                    hit = self._walk_for_url(node[key])
                    if hit:
                        return hit
            for k, v in node.items():
                if k in self._PRIORITY_KEYS:
                    continue
                hit = self._walk_for_url(v)
                if hit:
                    return hit
        return None
