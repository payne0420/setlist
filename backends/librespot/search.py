"""Extended-version search for the librespot backend.

librespot streams ONE exact track id — it has no "give me the extended cut" notion.
So extended support is a search-then-load step: query Spotify's own Web API (reusing
the librespot session's bearer token — no extra app credentials), build candidates,
filter out covers / sped-up / remix uploads, then defer to the SHARED
``track_selectors`` heuristics (identical to the YouTube path). The chosen Spotify
track id is then streamed natively by :mod:`.audio`.

Non-extended mode does NOT search — the backend streams the pasted/playlist id directly.
"""

from __future__ import annotations

import requests

import track_selectors

from . import _librespot as adapter

_SEARCH_URL = "https://api.spotify.com/v1/search"
_SEARCH_LIMIT = 20

# Version descriptors that mean "a different recording than the one we want" in
# extended mode. We reject candidates whose title carries these unless the title
# ALSO carries an extended keyword (an "Extended Remix" is still acceptable).
_REJECT_KEYWORDS = ("sped up", "spedup", "sped-up", "slowed", "nightcore", "radio edit")
_REJECT_UNLESS_EXTENDED = ("remix",)


def _norm(text: str) -> str:
    return "".join(ch.lower() for ch in (text or "") if ch.isalnum() or ch.isspace()).strip()


def _primary_artist(artists: str) -> str:
    """First artist from Setlist's comma-joined artists string."""
    return (artists or "").split(",")[0].strip()


def _fetch_candidates(
    session_raw, query: str, *, limit: int = _SEARCH_LIMIT, token: str | None = None
) -> list[dict]:
    """Query the Web API and map results onto the selector candidate shape (+artists).

    *token* is a pre-resolved Web-API bearer. When given (the user configured their own
    app's Client-Credentials token — see :mod:`.webapi`) it is used as-is; that token's
    own rate-limit bucket is what makes ``/v1/search`` usable. When ``None`` we fall back
    to librespot's keymaster token, which Spotify rate-limits hard.

    SINGLE shot — deliberately NO retry. On the keymaster token a repeat request *within*
    the cooldown makes Spotify ESCALATE the penalty (observed Retry-After jumping
    30-60s -> 86400s, a 24h ban). So a 429 must propagate immediately; the caller streams
    the original track (and may defer to YouTube) rather than hammering the endpoint.
    """
    token = token or adapter.web_bearer_token(session_raw)
    resp = requests.get(
        _SEARCH_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={"q": query, "type": "track", "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    items = (resp.json().get("tracks") or {}).get("items") or []
    cands: list[dict] = []
    for it in items:
        if not it or not it.get("id"):
            continue
        dur_ms = it.get("duration_ms") or 0
        cands.append(
            {
                "id": it["id"],
                "title": it.get("name") or "",
                "duration_s": (dur_ms / 1000) if dur_ms else None,
                "artists": [a.get("name", "") for a in (it.get("artists") or [])],
            }
        )
    return cands


def _artist_matches(cand: dict, primary: str) -> bool:
    """True iff the expected primary artist appears among the candidate's artists.

    Avoids covers/tributes by a different artist while still allowing the real cut
    that credits an extra remixer alongside the original artist.
    """
    if not primary:
        return True
    want = _norm(primary)
    return any(want and want in _norm(name) for name in cand.get("artists", []))


def _title_rejected(title: str) -> bool:
    t = _norm(title)
    if any(kw in t for kw in _REJECT_KEYWORDS):
        return True
    # "remix" etc. are only rejected when the title is NOT also flagged extended
    # (an "Extended Remix" is acceptable).
    return not track_selectors.title_boost(title) and any(kw in t for kw in _REJECT_UNLESS_EXTENDED)


def _filter(cands: list[dict], primary_artist: str) -> list[dict]:
    return [
        c for c in cands if _artist_matches(c, primary_artist) and not _title_rejected(c["title"])
    ]


def find_extended_id(
    session_raw,
    *,
    title: str,
    artists: str,
    expected_s: float | None,
    max_track_duration_s: float,
    web_token: str | None = None,
) -> str | None:
    """Resolve the Spotify track id of the extended cut, or None when none qualifies.

    Builds the bare ``"<title> <artists> extended"`` query (no "mix"/"audio" tokens —
    they bury genuine Extended Versions), filters covers/sped-up/remix, then uses
    ``track_selectors.select_extended``.

    Returns a base62 id, or ``None`` when the search SUCCEEDED but found no acceptable
    extended candidate. PROPAGATES the exception on a search/network/token failure so
    the caller can tell "no extended cut exists" (None) apart from "the search failed"
    (raise) — the two warrant different fallbacks (stream original vs. retry/YouTube).
    """
    clean_title = track_selectors.strip_radio_edit(title)
    query = f"{clean_title} {artists} extended".strip()
    cands = _fetch_candidates(session_raw, query, token=web_token)
    filtered = _filter(cands, _primary_artist(artists))
    return track_selectors.select_extended(filtered, expected_s, max_track_duration_s)
