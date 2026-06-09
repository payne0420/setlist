"""Backend-independent track-selection heuristics for extended-mix mode.

These functions were extracted verbatim from MusicScraper._select_youtube_match
so that any audio backend (YouTube today; a Qobuz/Tidal catalog search or a
Spotify search via librespot later) can reuse the exact same extended-cut
selection logic instead of re-implementing it.

A *candidate* is a plain dict ``{"id": ..., "title": str, "duration_s": float|None}``.
``id`` is whatever the backend later downloads/streams by (a YouTube video id, a
Qobuz/Tidal track id, a Spotify track id). The selectors only ever look at
``title`` and ``duration_s`` and return the chosen ``id`` (or ``None``).
"""

from __future__ import annotations

import re

# Max gap (seconds) between the Spotify track length and a candidate's length
# for the candidate to count as the "same" recording. In extended mode it is the
# floor offset an extended cut must clear to count as "genuinely longer".
DURATION_TOLERANCE_S = 7

# A candidate only qualifies as an extended cut if its (lowercased) title
# contains one of these. "extended" is a deliberately broad stem matching
# Extended / Extended Mix / Extended Version / Extended Edit.
EXTENDED_TITLE_KEYWORDS = ("extended", "club mix")

# Upper bound on how much longer than the radio edit an extended cut may be
# ("longer than the edit but never an hour-long mix"). Also the lower divisor
# (expected / ratio) for the already-extended fallback window.
EXTENDED_MAX_RATIO = 2.5

# Default for the user-configurable max_extended_minutes.
DEFAULT_MAX_EXTENDED_MINUTES = 20

RADIO_EDIT_RE = re.compile(
    r"\s*(?:\(\s*radio\s*edit\s*\)|\[\s*radio\s*edit\s*\]|[-–—]\s*radio\s*edit\b)\s*",
    re.IGNORECASE,
)


def strip_radio_edit(title: str) -> str:
    """Remove a "Radio Edit" version descriptor from a title (used only in
    extended-mix mode, where we fetch a longer cut). Leaves the rest of the
    title — including unrelated words like "Radio Ga Ga" — intact."""
    cleaned = RADIO_EDIT_RE.sub(" ", title)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = cleaned.strip(" -–—")
    return cleaned if cleaned else title


def title_boost(title: str) -> bool:
    """True iff *title* contains an extended-cut keyword."""
    t = (title or "").lower()
    return any(kw in t for kw in EXTENDED_TITLE_KEYWORDS)


def select_extended(cands, expected_s, max_track_duration_s):
    """Pick the extended cut among *cands* (or None to fall back).

    Mirrors MusicScraper._select_youtube_match's prefer_extended branch:
      1. Genuinely-longer cut: keyworded candidate in (expected+tol, upper] —
         pick the LONGEST.
      2. Already-extended fallback: if none, widen the low end to expected/ratio
         and pick the keyworded candidate CLOSEST to expected.
      3. No expected duration: require keyword + sane length, take the first
         (most relevant) such candidate.
    Returns the chosen candidate's ``id`` or ``None``.
    """
    timed = [c for c in cands if c.get("duration_s")]
    if expected_s:
        lower = expected_s + DURATION_TOLERANCE_S
        upper = min(expected_s * EXTENDED_MAX_RATIO, max_track_duration_s)
        longer = [c for c in timed if title_boost(c["title"]) and lower < c["duration_s"] <= upper]
        if longer:
            return max(longer, key=lambda c: c["duration_s"])["id"]
        lo = expected_s / EXTENDED_MAX_RATIO
        near = [c for c in timed if title_boost(c["title"]) and lo <= c["duration_s"] <= upper]
        if near:
            return min(near, key=lambda c: abs(c["duration_s"] - expected_s))["id"]
        return None
    sane = [c for c in timed if title_boost(c["title"]) and c["duration_s"] <= max_track_duration_s]
    return sane[0]["id"] if sane else None


def select_normal(cands, expected_s):
    """Pick the best non-extended match: trust the top hit unless its length is
    clearly off, then take the closest-duration candidate. Mirrors the
    non-extended branch of _select_youtube_match. Returns an ``id`` or ``None``.
    """
    if not cands:
        return None
    chosen = cands[0]
    if expected_s:
        top_dur = chosen.get("duration_s")
        top_off = top_dur is None or (abs(top_dur - expected_s) > DURATION_TOLERANCE_S)
        if top_off:
            timed = [c for c in cands if c.get("duration_s")]
            if timed:
                chosen = min(timed, key=lambda c: abs(c["duration_s"] - expected_s))
    return chosen["id"]
