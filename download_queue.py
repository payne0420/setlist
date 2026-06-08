"""Pure (Qt-free) logic for the multi-playlist download queue.

This module deliberately has NO PyQt5 import so it can be unit-tested without a
QApplication or a display. The Qt-facing parts (QueueDialog, the sequential
controller) live in Spotify_Downloader.py and drive these objects.

Pieces:
- parse_playlist_urls: turn a pasted blob into validated, de-duplicated items.
- classify_completion: map a terminal status message to a queue outcome.
- QueueItem / DownloadQueue: an ordered model addressed by stable ids.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from spotifydown_api import detect_spotify_url_type

# Split a pasted blob on any run of whitespace (incl. newlines) and/or commas.
_TOKEN_SPLIT_RE = re.compile(r"[\s,]+")

# Queue item lifecycle.
PENDING = "pending"
ACTIVE = "active"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"
PARTIAL = "partial"  # finished, but some tracks inside failed
TERMINAL_STATUSES = frozenset({DONE, FAILED, CANCELLED, PARTIAL})


@dataclass(frozen=True)
class ParsedURL:
    """One validated Spotify URL token."""

    url: str
    kind: str  # "playlist" | "album" | "track"
    spotify_id: str


def parse_playlist_urls(text: str) -> tuple[list[ParsedURL], int]:
    """Parse a paste blob into validated, de-duplicated Spotify URLs.

    Returns ``(items, skipped)`` where *items* is a list of :class:`ParsedURL`
    in first-seen order and *skipped* is the count of non-empty tokens that
    were not valid Spotify URLs.

    Tokenize FIRST, validate each token individually. ``detect_spotify_url_type``
    matches with ``re.search``, so handing it a whole multi-line blob would
    validate as only its FIRST embedded URL and silently drop the rest — so we
    must split into tokens (on whitespace / newlines / commas) before
    validating. De-duplication is by ``(kind, spotify_id)`` so canonical,
    ``/intl-xx/``, ``?si=...`` and ``spotify:`` URI variants of the same item
    collapse to a single entry.
    """
    if not text or not text.strip():
        return [], 0

    items: list[ParsedURL] = []
    seen: set[tuple[str, str]] = set()
    skipped = 0

    for token in _TOKEN_SPLIT_RE.split(text.strip()):
        if not token:
            continue
        try:
            kind, spotify_id = detect_spotify_url_type(token)
        except ValueError:
            skipped += 1
            continue
        key = (kind, spotify_id)
        if key in seen:
            continue
        seen.add(key)
        items.append(ParsedURL(url=token, kind=kind, spotify_id=spotify_id))

    return items, skipped


def classify_completion(message: str | None, cancelled: bool = False) -> str:
    """Map a terminal download message to a queue outcome.

    The downloader signals completion via stringly-typed messages, so success
    must be recognized EXPLICITLY and anything unrecognized is treated as a
    failure (fail-closed). This avoids mislabeling error notices like
    "Rate limited by Spotify - waiting..." or "YouTube rate limit - waiting..."
    (which contain no failure keyword) as successes.

    Known terminal messages:
      "Download Complete!"                 -> done
      "Track already exists!"              -> done
      "Done! N track(s) failed"            -> partial
      "Download cancelled"                 -> cancelled
      "Download failed - no audio ..."     -> failed
      any _get_user_friendly_error string  -> failed
      "" (run() swallowed an exception)    -> failed
    """
    msg = (message or "").strip()
    low = msg.lower()
    # Fail-closed: only the cancel flag or the EXACT cancelled message counts as
    # cancelled (so an error string that merely contains "cancel" isn't), and
    # success is an exact match against the known terminal strings — anything
    # unrecognized (incl. "Download incomplete", rate-limit notices, "") is a
    # failure.
    if cancelled or low == "download cancelled":
        return CANCELLED
    if "track(s) failed" in low or "tracks failed" in low:
        return PARTIAL
    if msg in _SUCCESS_MESSAGES:
        return DONE
    return FAILED


# Exact terminal strings the downloader emits on success.
_SUCCESS_MESSAGES = frozenset({"Download Complete!", "Track already exists!"})


@dataclass
class QueueItem:
    """A single queued download, addressed by its stable ``id``."""

    id: int
    url: str
    kind: str
    spotify_id: str
    display_name: str
    status: str = PENDING


class DownloadQueue:
    """An ordered queue of downloads addressed by stable item ids.

    All lookups/updates are by ``item.id`` (never by list index) so that
    clearing the queue or a late cross-thread signal can never mutate the wrong
    row. Ids are monotonically increasing and never reused.
    """

    def __init__(self) -> None:
        self._items: list[QueueItem] = []
        self._next_id = 1

    @property
    def items(self) -> list[QueueItem]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def is_empty(self) -> bool:
        return not self._items

    def add(self, parsed: list[ParsedURL]) -> list[QueueItem]:
        """Append parsed URLs as pending items, skipping any already queued.

        De-dupes against items already in the queue by ``(kind, spotify_id)``
        (regardless of their status) and returns only the newly added items so
        the caller can append matching rows to the UI.
        """
        existing = {(it.kind, it.spotify_id) for it in self._items}
        added: list[QueueItem] = []
        for p in parsed:
            key = (p.kind, p.spotify_id)
            if key in existing:
                continue
            existing.add(key)
            item = QueueItem(
                id=self._next_id,
                url=p.url,
                kind=p.kind,
                spotify_id=p.spotify_id,
                display_name=_default_display_name(p),
            )
            self._next_id += 1
            self._items.append(item)
            added.append(item)
        return added

    def get(self, item_id: int) -> QueueItem | None:
        for it in self._items:
            if it.id == item_id:
                return it
        return None

    def next_pending(self) -> QueueItem | None:
        """Return the first still-pending item, or None if none remain."""
        for it in self._items:
            if it.status == PENDING:
                return it
        return None

    def active_item(self) -> QueueItem | None:
        for it in self._items:
            if it.status == ACTIVE:
                return it
        return None

    def mark(self, item_id: int, status: str) -> bool:
        """Set an item's status by id. No-op (returns False) for unknown ids."""
        item = self.get(item_id)
        if item is None:
            return False
        item.status = status
        return True

    def set_display_name(self, item_id: int, name: str) -> bool:
        item = self.get(item_id)
        if item is None or not name:
            return False
        item.display_name = name
        return True

    def counts(self) -> dict[str, int]:
        """Return per-status counts plus a 'total'."""
        out = {
            "total": len(self._items),
            PENDING: 0,
            ACTIVE: 0,
            DONE: 0,
            FAILED: 0,
            CANCELLED: 0,
            PARTIAL: 0,
        }
        for it in self._items:
            out[it.status] = out.get(it.status, 0) + 1
        return out

    def all_terminal(self) -> bool:
        """True when no item is pending or active (every item has finished)."""
        return all(it.status in TERMINAL_STATUSES for it in self._items)

    def cancel_pending(self) -> None:
        """Mark every still-pending item as cancelled (used when halting)."""
        for it in self._items:
            if it.status == PENDING:
                it.status = CANCELLED

    def reset(self) -> None:
        """Drop all items. Ids keep incrementing (never reused)."""
        self._items.clear()


def _default_display_name(p: ParsedURL) -> str:
    """A readable placeholder until the real playlist/track name arrives."""
    return f"{p.kind.capitalize()} {p.spotify_id[:8]}…"
