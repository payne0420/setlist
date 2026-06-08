"""Unit tests for the Qt-free download-queue logic (download_queue.py)."""

from download_queue import (
    ACTIVE,
    CANCELLED,
    DONE,
    FAILED,
    PARTIAL,
    PENDING,
    DownloadQueue,
    ParsedURL,
    classify_completion,
    parse_playlist_urls,
)

PL = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
AL = "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy"
TR = "https://open.spotify.com/track/2D9coh76MCXNqDEUCHl5vl"


class TestParsePlaylistUrls:
    def test_empty_and_whitespace_return_nothing(self):
        assert parse_playlist_urls("") == ([], 0)
        assert parse_playlist_urls("   \n\t  ") == ([], 0)
        assert parse_playlist_urls(None) == ([], 0)

    def test_mixed_separators_split_correctly(self):
        # Newlines, commas AND spaces in one blob — the regression case: a blob
        # handed straight to detect_spotify_url_type would only match the first.
        blob = f"{PL}\n{AL} , {TR}"
        items, skipped = parse_playlist_urls(blob)
        assert skipped == 0
        assert [it.spotify_id for it in items] == [
            "37i9dQZF1DXcBWIGoYBM5M",
            "4aawyAB9vmqN3uQ7FjRGTy",
            "2D9coh76MCXNqDEUCHl5vl",
        ]

    def test_invalid_tokens_dropped_and_counted(self):
        blob = f"{PL}\nnot-a-url\nhttps://example.com/foo\n{TR}"
        items, skipped = parse_playlist_urls(blob)
        assert [it.spotify_id for it in items] == [
            "37i9dQZF1DXcBWIGoYBM5M",
            "2D9coh76MCXNqDEUCHl5vl",
        ]
        assert skipped == 2

    def test_order_is_preserved(self):
        items, _ = parse_playlist_urls(f"{TR} {AL} {PL}")
        assert [it.kind for it in items] == ["track", "album", "playlist"]

    def test_dedupe_by_type_and_id_collapses_url_variants(self):
        # canonical, intl-prefixed, ?si= query, and spotify: URI all name the
        # SAME track -> exactly one entry.
        blob = "\n".join(
            [
                TR,
                "https://open.spotify.com/intl-de/track/2D9coh76MCXNqDEUCHl5vl",
                f"{TR}?si=abc123def456",
                "spotify:track:2D9coh76MCXNqDEUCHl5vl",
                "   " + TR + "   ",
            ]
        )
        items, skipped = parse_playlist_urls(blob)
        assert len(items) == 1
        assert items[0].kind == "track"
        assert items[0].spotify_id == "2D9coh76MCXNqDEUCHl5vl"
        assert skipped == 0

    def test_same_id_different_types_not_collapsed(self):
        # A playlist and a track that happen to share an id are distinct items.
        same = "37i9dQZF1DXcBWIGoYBM5M"
        items, _ = parse_playlist_urls(
            f"https://open.spotify.com/playlist/{same} https://open.spotify.com/track/{same}"
        )
        assert {it.kind for it in items} == {"playlist", "track"}
        assert len(items) == 2

    def test_mixed_types_all_typed(self):
        items, _ = parse_playlist_urls(f"{PL}\n{AL}\n{TR}")
        assert [it.kind for it in items] == ["playlist", "album", "track"]


class TestDownloadQueue:
    @staticmethod
    def _parsed(*kind_id):
        return [ParsedURL(url=f"u/{k}/{i}", kind=k, spotify_id=i) for k, i in kind_id]

    def test_add_dedupes_preserves_order_and_returns_added(self):
        q = DownloadQueue()
        added = q.add(self._parsed(("playlist", "a"), ("album", "b"), ("playlist", "a")))
        assert [it.spotify_id for it in added] == ["a", "b"]
        assert len(q) == 2
        # Adding an already-queued item returns nothing new.
        more = q.add(self._parsed(("album", "b"), ("track", "c")))
        assert [it.spotify_id for it in more] == ["c"]
        assert len(q) == 3

    def test_items_have_stable_unique_ids_and_kind_id(self):
        q = DownloadQueue()
        q.add(self._parsed(("playlist", "a"), ("track", "b")))
        ids = [it.id for it in q.items]
        assert len(set(ids)) == 2
        first = q.items[0]
        assert (first.kind, first.spotify_id) == ("playlist", "a")

    def test_next_pending_empty_returns_none(self):
        assert DownloadQueue().next_pending() is None

    def test_next_pending_skips_non_pending(self):
        q = DownloadQueue()
        q.add(self._parsed(("playlist", "a"), ("playlist", "b"), ("playlist", "c")))
        a, b, c = q.items
        q.mark(a.id, DONE)
        q.mark(b.id, ACTIVE)
        # Only c is still pending.
        assert q.next_pending().id == c.id

    def test_mark_unknown_id_is_noop(self):
        q = DownloadQueue()
        q.add(self._parsed(("playlist", "a")))
        assert q.mark(9999, DONE) is False
        assert q.items[0].status == PENDING

    def test_full_sequential_flow(self):
        q = DownloadQueue()
        q.add(self._parsed(("playlist", "a"), ("album", "b")))
        nxt = q.next_pending()
        assert nxt.spotify_id == "a"
        q.mark(nxt.id, ACTIVE)
        assert q.active_item().id == nxt.id
        q.mark(nxt.id, DONE)
        nxt2 = q.next_pending()
        assert nxt2.spotify_id == "b"
        q.mark(nxt2.id, FAILED)
        assert q.next_pending() is None
        assert q.all_terminal() is True

    def test_counts_and_reset(self):
        q = DownloadQueue()
        q.add(self._parsed(("playlist", "a"), ("album", "b"), ("track", "c")))
        a, b, _ = q.items
        q.mark(a.id, DONE)
        q.mark(b.id, FAILED)
        counts = q.counts()
        assert counts["total"] == 3
        assert counts[DONE] == 1
        assert counts[FAILED] == 1
        assert counts[PENDING] == 1
        q.reset()
        assert q.is_empty()
        assert q.counts()["total"] == 0

    def test_cancel_pending_only_touches_pending(self):
        q = DownloadQueue()
        q.add(self._parsed(("playlist", "a"), ("album", "b"), ("track", "c")))
        a, b, _ = q.items
        q.mark(a.id, ACTIVE)
        q.mark(b.id, DONE)
        q.cancel_pending()
        assert q.get(a.id).status == ACTIVE  # active not touched
        assert q.get(b.id).status == DONE  # done not touched
        assert q.items[2].status == CANCELLED  # the pending one


class TestClassifyCompletion:
    def test_success_strings(self):
        assert classify_completion("Download Complete!") == DONE
        assert classify_completion("Track already exists!") == DONE

    def test_cancelled(self):
        assert classify_completion("Download cancelled") == CANCELLED
        # Cancel flag overrides an otherwise-success message.
        assert classify_completion("Download Complete!", cancelled=True) == CANCELLED

    def test_partial(self):
        assert classify_completion("Done! 3 track(s) failed") == PARTIAL

    def test_failures_including_keywordless_notices(self):
        assert classify_completion("Download failed - no audio file produced") == FAILED
        # These error notices contain NO failure keyword but are still failures.
        assert classify_completion("Rate limited by Spotify - waiting...") == FAILED
        assert classify_completion("YouTube rate limit - waiting...") == FAILED
        assert classify_completion("") == FAILED
        assert classify_completion(None) == FAILED

    def test_fail_closed_against_substrings(self):
        # Anchored matching: a near-miss of a success/cancel word is NOT a pass.
        assert classify_completion("Download incomplete") == FAILED
        assert classify_completion("not complete") == FAILED
        assert classify_completion("Could not cancel cleanly") == FAILED
