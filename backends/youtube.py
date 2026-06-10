"""The default audio backend: YouTube via yt-dlp.

A thin adapter over the existing MusicScraper YouTube logic
(``download_track_audio`` + ``_select_youtube_match``). Keeping the heavy lifting
on the scraper preserves the public method contract the test suite pins, while
the AudioBackend seam lets other download sources slot in. The query-building
that used to sit inline at the two call sites lives here now, so the call sites
are backend-agnostic.
"""

from __future__ import annotations

import os


class YouTubeBackend:
    """Resolve a track to a YouTube search and download via the scraper."""

    # Matches MusicScraper.MAX_WORKERS — the measured sweet spot for parallel
    # yt-dlp downloads.
    max_concurrency = 4

    def __init__(self, scraper):
        self._scraper = scraper

    def fetch(self, *, track, destination, extended, audio_format, audio_quality, cancel):
        s = self._scraper
        # In extended mode the title is stripped of a "Radio Edit" descriptor so
        # the search isn't anchored to the short edit (matches the pre-refactor
        # behavior in _download_one_track / scrape_track).
        track_title = s._strip_radio_edit(track.title) if extended else track.title
        artists = track.artists
        expected_dur = (track.duration_ms / 1000) if track.duration_ms else None
        normal_query = f"ytsearch1:{track_title} {artists} audio"
        if extended:
            search_query = s._extended_search_query(track_title, artists)
            path, used = s.download_track_audio(
                search_query,
                destination,
                expected_duration_s=expected_dur,
                fallback_query=normal_query,
                source_title=track_title,
            )
        else:
            path, used = s.download_track_audio(
                normal_query, destination, expected_duration_s=expected_dur
            )
        ext = os.path.splitext(path)[1].lstrip(".")
        return path, ext, used
