"""Songlink bridge — ISRC → Tidal track id / Amazon ASIN.

Ports SpotiFLAC's default ``deezer-songlink`` resolver (``songlink.go`` /
``link_resolver.go``): the ISRC is mapped to a **Deezer** track URL
(``api.deezer.com/track/isrc:<ISRC>``), and that Deezer URL — *not* a Spotify
URL — is handed to Songlink (``api.song.link/...?url=<deezer url>``), which
returns the platform links. Qobuz does NOT use this bridge (it resolves by ISRC
against Qobuz's own API). Tidal and Amazon do.

The optional Songstats fallback resolver is intentionally not ported for v1
(goal §5: "not required for v1"); the Deezer→Songlink path is the default.
"""

from __future__ import annotations

import re

import requests

from . import constants as C

# Verbatim from songlink.go.
_AMAZON_ALBUM_TRACK_RE = re.compile(r"/albums/[A-Z0-9]{10}/(B[0-9A-Z]{9})")
_AMAZON_TRACK_RE = re.compile(r"/tracks/(B[0-9A-Z]{9})")
# amazon.go ASIN grab (first B######### anywhere).
_ASIN_RE = re.compile(r"(B[0-9A-Z]{9})")


def normalize_amazon_music_url(raw: str) -> str:
    """Canonicalize any Amazon Music URL to
    ``https://music.amazon.com/tracks/<ASIN>?musicTerritory=US`` or ``""``.

    Order (first match wins): ``trackAsin=`` query param (unvalidated), then
    ``/albums/<10>/(<ASIN>)``, then ``/tracks/(<ASIN>)``. A URL with no
    recognizable ASIN returns ``""`` (the value is dropped)."""
    u = (raw or "").strip()
    if not u:
        return ""
    if "trackAsin=" in u:
        parts = u.split("trackAsin=")
        if len(parts) > 1:
            asin = parts[1].split("&")[0]
            if asin:
                return f"https://music.amazon.com/tracks/{asin}?musicTerritory=US"
    m = _AMAZON_ALBUM_TRACK_RE.search(u)
    if m:
        return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"
    m = _AMAZON_TRACK_RE.search(u)
    if m:
        return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"
    return ""


def extract_amazon_asin(amazon_url: str) -> str | None:
    """First ``B#########`` ASIN anywhere in the URL, or ``None`` (amazon.go)."""
    m = _ASIN_RE.search(amazon_url or "")
    return m.group(1) if m else None


def extract_tidal_track_id(tidal_url: str) -> int | None:
    """Tidal numeric track id: split on ``/track/``, take the int before ``?``.

    Mirrors tidal.go ``GetTrackIDFromURL`` (Sscanf-style leading-int parse)."""
    if not tidal_url or "/track/" not in tidal_url:
        return None
    tail = tidal_url.split("/track/", 1)[1].split("?", 1)[0].strip().strip("/")
    m = re.match(r"\d+", tail)
    if not m:
        return None
    tid = int(m.group(0))
    return tid or None


def deezer_track_json(isrc: str, session: requests.Session) -> dict:
    """ISRC → the full Deezer track JSON (api.deezer.com/track/isrc:<ISRC>).

    Raises on HTTP error. The JSON carries the metadata the Amazon path reuses
    for tagging (title, contributors[].name, album.title/cover_xl,
    track_position, disk_number, release_date) — see :mod:`lossless.metadata`."""
    url = C.DEEZER_ISRC_URL.format(isrc=isrc.strip().upper())
    resp = session.get(url, headers={"User-Agent": C.SONGLINK_USER_AGENT}, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(f"Deezer ISRC API returned status {resp.status_code}")
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _deezer_url_from_json(data: dict, isrc: str) -> str:
    link = (data.get("link") or "").strip()
    if link:
        return _normalize_deezer_url(link)
    if data.get("id"):
        return f"https://www.deezer.com/track/{data['id']}"
    raise RuntimeError(f"deezer track link not found for ISRC {isrc}")


def _deezer_track_url_by_isrc(isrc: str, session: requests.Session) -> str:
    """ISRC → Deezer track URL (api.deezer.com/track/isrc:<ISRC>)."""
    return _deezer_url_from_json(deezer_track_json(isrc, session), isrc)


def _normalize_deezer_url(raw: str) -> str:
    if raw and "/track/" in raw:
        tail = raw.split("/track/", 1)[1].split("?", 1)[0].strip("/ ")
        if tail:
            return f"https://www.deezer.com/track/{tail}"
    return raw.strip()


def _songlink_links(deezer_url: str, session: requests.Session, region: str = "") -> dict:
    """Fetch ``linksByPlatform`` for a Deezer URL via Songlink."""
    params = {"url": deezer_url}
    if region:
        params["userCountry"] = region
    resp = session.get(
        C.SONGLINK_LINKS_URL,
        params=params,
        headers={"User-Agent": C.SONGLINK_USER_AGENT},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"song.link returned status {resp.status_code}")
    if not resp.content:
        raise RuntimeError("song.link returned empty response")
    return resp.json().get("linksByPlatform", {}) or {}


def resolve_platform_links(isrc: str, session: requests.Session, region: str = "") -> dict:
    """ISRC → ``{"tidal_url", "amazon_url"}`` via Deezer→Songlink.

    ``tidal_url`` is the raw Songlink Tidal URL (id-parsed by the caller);
    ``amazon_url`` is canonicalized to the ``music.amazon.com/tracks/<ASIN>``
    form. Missing platforms are absent/empty. Raises on a hard bridge failure
    (no ISRC, Deezer/Songlink down)."""
    if not isrc or not isrc.strip():
        raise RuntimeError("ISRC is required for the Songlink bridge")
    deezer = deezer_track_json(isrc, session)
    deezer_url = _deezer_url_from_json(deezer, isrc)
    platforms = _songlink_links(deezer_url, session, region)
    tidal = (platforms.get("tidal") or {}).get("url") or ""
    amazon = (platforms.get("amazonMusic") or {}).get("url") or ""
    # ``deezer`` (the full track JSON) is carried so the Amazon path can tag from
    # the same self-consistent set without a second request (lossless.metadata).
    return {
        "tidal_url": tidal.strip(),
        "amazon_url": normalize_amazon_music_url(amazon),
        "deezer": deezer,
    }
