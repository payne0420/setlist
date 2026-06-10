"""Spotify track id → ISRC (the bridge key for every lossless service).

Ports SpotiFLAC's ``spotify_totp.go`` + ``isrc_finder.go`` + ``soundplate.go``:

1. RFC-6238 TOTP (the secret is a plain Base32 key — NO XOR transform) →
2. anonymous Web-Player access token (cached to disk, refreshed 30s before expiry) →
3. base62-decode the 22-char track id to a 32-char hex GID →
4. spclient ``/metadata/4/track/<gid>`` → the ``external_id`` entry of type ``isrc`` →
5. Soundplate fallback (third-party scraper) when spclient yields nothing.

The public :func:`resolve_track_isrc` is best-effort and never raises — it
returns an uppercase ISRC or ``None`` so the lossless backend can fall through
to YouTube when the ISRC can't be found (goal §4).
"""

from __future__ import annotations

import re
import threading
import time

import requests

from . import constants as C
from ._util import JsonStore, default_headers

# 2 uppercase letters, 3 alphanumerics, 7 digits (12 chars). firstISRCMatch
# uppercases the whole input before matching, so lowercase ISRCs still match.
_ISRC_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{3}\d{7})\b")

_TOKEN_KEY = "token"
_TOKEN_REFRESH_SKEW_MS = 30_000


def generate_spotify_totp(now_unix: float | None = None) -> tuple[str, int]:
    """Return ``(code, version)`` — the 6-digit RFC-6238 TOTP for the Spotify
    Web-Player secret. Uses pyotp (the secret is a standard Base32 key)."""
    import pyotp

    totp = pyotp.TOTP(C.SPOTIFY_TOTP_SECRET)  # SHA1, 6 digits, 30s period
    code = totp.now() if now_unix is None else totp.at(int(now_unix))
    return code, C.SPOTIFY_TOTP_VERSION


def extract_spotify_track_id(value: str) -> str:
    """Normalize a raw id / ``spotify:track:<id>`` URI / track URL to the 22-char id."""
    import urllib.parse

    value = (value or "").strip()
    if value == "":
        raise ValueError("track input is required")
    if value.startswith("spotify:track:"):
        return value[value.rfind(":") + 1 :]
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme in ("http", "https"):
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "track":
            return parts[1]
        raise ValueError("expected URL like https://open.spotify.com/track/<id>")
    if len(value) == 22:
        return value
    raise ValueError("track must be a Spotify track ID, URL, or URI")


def spotify_entity_id_to_gid(entity_id: str) -> str:
    """base62-decode (digits→lowercase→uppercase alphabet) to a 32-char lowercase
    hex GID, left-zero-padded. A valid 22-char id decodes to exactly 32 hex chars."""
    if not entity_id:
        raise ValueError("entity ID is empty")
    value = 0
    for ch in entity_id:
        idx = C.SPOTIFY_BASE62_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid base62 character: {ch!r}")
        value = value * 62 + idx
    hex_value = format(value, "x")  # lowercase, no leading zeros (like big.Int.Text(16))
    if len(hex_value) < 32:
        hex_value = "0" * (32 - len(hex_value)) + hex_value
    return hex_value


def first_isrc_match(text: str) -> str:
    """First ISRC in *text* (uppercased before matching), or ``""``."""
    if not text:
        return ""
    m = _ISRC_RE.search(text.upper())
    return m.group(1).strip() if m else ""


class SpotifyIsrcResolver:
    """Resolves Spotify track ids to ISRCs with a disk cache + token reuse.

    One instance per scraper run; the token + ISRC caches are shared on disk
    across runs. Thread-safe (the playlist worker pool calls :meth:`resolve`
    from up to ``max_concurrency`` workers).
    """

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()
        self._tokens = JsonStore("spotify-token.json")
        self._isrc_cache = JsonStore("isrc-cache.json")
        self._token_lock = threading.Lock()
        # In-memory caches for the spclient track JSON and its parsed tag dict,
        # shared so the ISRC lookup and the rich-metadata lookup hit the network
        # at most once per track id. Guarded for the 4-worker playlist pool.
        self._meta_lock = threading.Lock()
        self._meta_json_cache: dict[str, dict] = {}
        self._meta_cache: dict[str, dict | None] = {}

    # -- token -------------------------------------------------------------- #
    def _token_is_valid(self, tok: dict | None) -> bool:
        if not tok or not tok.get("accessToken"):
            return False
        exp = tok.get("accessTokenExpirationTimestampMs", 0)
        if not exp:
            return False
        return int(time.time() * 1000) < exp - _TOKEN_REFRESH_SKEW_MS

    def _get_token(self) -> str:
        with self._token_lock:
            cached = self._tokens.get(_TOKEN_KEY)
            if self._token_is_valid(cached):
                return cached["accessToken"]
            code, version = generate_spotify_totp()
            params = {
                "reason": "init",
                "productType": "web-player",
                "totp": code,
                "totpServer": code,  # same value as totp
                "totpVer": str(version),
            }
            # The Go original sends no app headers; we add a Chrome UA + JSON
            # content-type (goal §4) so the endpoint never rejects the default
            # python-requests UA. Harmless: the endpoint doesn't gate on UA.
            headers = default_headers({"Content-Type": "application/json;charset=UTF-8"})
            resp = self._session.get(
                C.SPOTIFY_SESSION_TOKEN_URL, params=params, headers=headers, timeout=30
            )
            resp.raise_for_status()
            data = resp.json()
            token = {
                "accessToken": data.get("accessToken", ""),
                "accessTokenExpirationTimestampMs": int(
                    data.get("accessTokenExpirationTimestampMs", 0) or 0
                ),
            }
            if not token["accessToken"]:
                raise RuntimeError("Spotify anonymous token response had no accessToken")
            self._tokens.set(_TOKEN_KEY, token)
            return token["accessToken"]

    # -- spclient metadata -------------------------------------------------- #
    def _spclient_track_json(self, track_id: str) -> dict:
        """GET (and cache) the full spclient track metadata JSON for *track_id*.

        Raises on HTTP / non-JSON error so the ISRC path can fall through to
        Soundplate (preserving the original behavior). The cache makes the ISRC
        lookup and :meth:`resolve_metadata` share a single network round-trip.
        The fetch runs OUTSIDE the lock; only the cache check/store is guarded
        (mirrors the librespot metadata service's lock discipline)."""
        with self._meta_lock:
            cached = self._meta_json_cache.get(track_id)
        if cached is not None:
            return cached
        gid = spotify_entity_id_to_gid(track_id)
        url = C.SPOTIFY_GID_METADATA_URL.format(etype="track", gid=gid)
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Accept": "application/json",
            "User-Agent": C.SONGLINK_USER_AGENT,
        }
        resp = self._session.get(url, headers=headers, timeout=30)
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(f"spclient metadata returned status {resp.status_code}")
        # A body that doesn't parse as JSON is a metadata error (-> Soundplate).
        data = resp.json()
        if isinstance(data, dict):
            with self._meta_lock:
                self._meta_json_cache[track_id] = data
        return data

    def _metadata_isrc(self, track_id: str) -> str:
        import json as _json

        data = self._spclient_track_json(track_id)
        for ext in data.get("external_id", []) or []:
            if isinstance(ext, dict) and (ext.get("type") or "").strip().lower() == "isrc":
                isrc = first_isrc_match(ext.get("id") or "")
                if isrc:
                    return isrc
        # Whole-body regex fallback over the JSON we already have.
        return first_isrc_match(_json.dumps(data))

    def resolve_metadata(self, track_id: str) -> dict | None:
        """Rich tag dict for *track_id* from the spclient metadata, or ``None``.

        Best-effort and thread-safe: any token/network/parse failure (or a track
        with no usable metadata) returns ``None`` so the caller keeps its
        embed-derived ``song_meta``. Cached per normalized id; shares the JSON
        fetch with :meth:`resolve` (the ISRC path), so enriching a track that was
        already ISRC-resolved costs no extra request."""
        raw = (track_id or "").strip()
        if not raw:
            return None
        try:
            nid = extract_spotify_track_id(raw)
        except ValueError:
            return None
        with self._meta_lock:
            if nid in self._meta_cache:
                return self._meta_cache[nid]
        try:
            data = self._spclient_track_json(nid)
        except Exception:
            return None  # best-effort: never fail/block a download on metadata
        from .metadata import extract_spotify_metadata

        meta = extract_spotify_metadata(data)
        if meta is not None:
            with self._meta_lock:
                self._meta_cache[nid] = meta
        return meta

    # -- Soundplate fallback ------------------------------------------------ #
    def _soundplate(self, track_id: str) -> tuple[str, str]:
        spotify_url = f"https://open.spotify.com/track/{track_id}"
        headers = {
            "User-Agent": C.SOUNDPLATE_USER_AGENT,
            "Accept": "*/*",
            "Referer": C.SOUNDPLATE_REFERER_URL,
            "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
            "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Priority": "u=1, i",
        }
        resp = self._session.get(
            C.SOUNDPLATE_SPOTIFY_API_URL, params={"q": spotify_url}, headers=headers, timeout=30
        )
        body = resp.text
        if resp.status_code != 200:
            raise RuntimeError(f"Soundplate returned status {resp.status_code}")
        # A non-JSON 200 is an error body (Go errors before the whole-body regex);
        # resp.json() raising here propagates to resolve()'s except -> None.
        payload = resp.json()
        isrc = first_isrc_match(payload.get("isrc", "") if isinstance(payload, dict) else "")
        if not isrc:
            isrc = first_isrc_match(body)
        if not isrc:
            raise RuntimeError("ISRC missing in Soundplate response")
        resolved = ""
        spu = payload.get("spotify_url", "") if isinstance(payload, dict) else ""
        if spu:
            try:
                resolved = extract_spotify_track_id(spu)
            except ValueError:
                resolved = ""
        return isrc, resolved

    # -- public ------------------------------------------------------------- #
    def resolve(self, track_id: str) -> str | None:
        """Resolve *track_id* to an uppercase ISRC, or ``None``. Never raises.

        Order: disk cache (raw key) → normalize → disk cache (normalized key) →
        spclient metadata → Soundplate. The result is cached under both the raw
        input and the normalized id (and any Soundplate-resolved id)."""
        raw = (track_id or "").strip()
        if not raw:
            return None
        cached = self._isrc_cache.get(raw)
        if cached:
            return cached.strip().upper()

        try:
            normalized = extract_spotify_track_id(raw)
        except ValueError:
            return None
        cached = self._isrc_cache.get(normalized)
        if cached:
            return cached.strip().upper()

        isrc = ""
        try:
            isrc = self._metadata_isrc(normalized)
        except Exception:
            isrc = ""
        resolved_id = ""
        if not isrc:
            try:
                isrc, resolved_id = self._soundplate(normalized)
            except Exception:
                isrc = ""

        if not isrc:
            return None
        isrc = isrc.strip().upper()
        for key in {raw, normalized, resolved_id}:
            if key:
                self._isrc_cache.set(key, isrc)
        return isrc


def resolve_track_isrc(track_id: str, session: requests.Session | None = None) -> str | None:
    """Best-effort one-shot ISRC resolution (never raises). Prefer reusing a
    :class:`SpotifyIsrcResolver` for token/cache reuse across many tracks."""
    return SpotifyIsrcResolver(session).resolve(track_id)
