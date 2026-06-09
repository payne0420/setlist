#
"""
Setlist — a Spotify→YouTube downloader.

A fork of Sunnify (Spotify Downloader) by Sunny Patel, diverged to add a
multi-playlist download queue, extended-mix mode, configurable filename order,
size/length caps, and a restyled UI. Original work © 2024 Sunny Patel; this
fork retains that copyright per the project license (see LICENSE).

EDUCATIONAL PROJECT DISCLAIMER:
This software is a student portfolio project developed for educational purposes only.
It is intended to demonstrate software engineering skills and is provided free of charge.
Users are solely responsible for ensuring compliance with applicable laws in their jurisdiction.
This software should only be used with content you own or have permission to download.
See DISCLAIMER.md for full terms.

For the program to work, the playlist URL pattern must follow the format of
/playlist/abcdefghijklmnopqrstuvwxyz... If the program stops working, email
<sunnypatel124555@gmail.com> or open an issue in the repository.
"""

__version__ = "2.1.0"

import concurrent.futures
import contextlib
import os
import re
import sys
import threading
import webbrowser

import requests
from mutagen.easyid3 import EasyID3
from mutagen.id3 import APIC, ID3
from PyQt5.QtCore import (
    Qt,
    QThread,
    QTimer,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtGui import QCursor, QImage, QPixmap
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from yt_dlp import YoutubeDL

import theme
from download_queue import (
    ACTIVE,
    CANCELLED,
    DONE,
    FAILED,
    PARTIAL,
    PENDING,
    DownloadQueue,
    classify_completion,
    parse_playlist_urls,
)
from spotifydown_api import (
    ExtractionError,
    NetworkError,
    PlaylistClient,
    PlaylistInfo,
    RateLimitError,
    SpotifyDownAPIError,
    detect_spotify_url_type,
    extract_playlist_id,
    sanitize_filename,
)
from ui_main import Ui_MainWindow


def get_ffmpeg_path():
    """Get path to FFmpeg - checks bundled first, then system paths."""
    # Check bundled FFmpeg first (for PyInstaller builds)
    if getattr(sys, "frozen", False):
        base_path = sys._MEIPASS
        if sys.platform == "win32":
            ffmpeg = os.path.join(base_path, "ffmpeg", "ffmpeg.exe")
        else:
            ffmpeg = os.path.join(base_path, "ffmpeg", "ffmpeg")
        if os.path.exists(ffmpeg):
            return os.path.join(base_path, "ffmpeg")

    # Check common system paths (for homebrew/system installs)
    ffmpeg_name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    common_paths = [
        "/opt/homebrew/bin",  # macOS ARM homebrew
        "/usr/local/bin",  # macOS Intel homebrew / Linux
        "/usr/bin",  # Linux system
    ]

    for path in common_paths:
        ffmpeg = os.path.join(path, ffmpeg_name)
        if os.path.exists(ffmpeg):
            return path

    # Check if ffmpeg is in PATH
    import shutil

    ffmpeg_in_path = shutil.which("ffmpeg")
    if ffmpeg_in_path:
        return os.path.dirname(ffmpeg_in_path)

    return None


# Supported output formats. "lossy" means quality/bitrate applies; "lossless"
# means the ffmpeg postprocessor ignores preferredquality.
SUPPORTED_FORMATS = {
    "mp3": {"ext": "mp3", "lossy": True},
    "m4a": {"ext": "m4a", "lossy": True},
    "opus": {"ext": "opus", "lossy": True},
    "flac": {"ext": "flac", "lossy": False},
    "wav": {"ext": "wav", "lossy": False},
}
SUPPORTED_QUALITIES = ("128", "192", "256", "320")

# Resume manifest: a JSON-lines file dropped inside each playlist/album folder
# recording which tracks already downloaded. On a re-run we skip those tracks
# before fetching their metadata, so a huge playlist throttled by Spotify's
# rate limit can be finished across several sessions instead of one long sit
# (closes #40).
MANIFEST_FILENAME = ".setlist-manifest.jsonl"


def _config_dir() -> str:
    """Return the per-user config directory, creating it if needed."""
    import json as _json  # noqa: F401 (used by load/save)

    if sys.platform == "win32":
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.join(os.path.expanduser("~"), ".config"))
    path = os.path.join(base, "Setlist")
    os.makedirs(path, exist_ok=True)
    return path


def _config_path() -> str:
    return os.path.join(_config_dir(), "config.json")


def load_config() -> dict:
    """Load persisted user config. Missing or corrupt file returns defaults."""
    import json

    defaults = {
        "version": 1,
        "download_path": None,
        "format": "mp3",
        "quality": "192",
        "extended_mix": False,
        "max_extended_minutes": 20,
        "max_track_mb": 0,
        "artist_first": False,
    }
    try:
        with open(_config_path(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return defaults
        defaults.update({k: v for k, v in data.items() if k in defaults})
        if defaults["format"] not in SUPPORTED_FORMATS:
            defaults["format"] = "mp3"
        if defaults["quality"] not in SUPPORTED_QUALITIES:
            defaults["quality"] = "192"
        try:
            mem = int(defaults["max_extended_minutes"])
            if mem <= 0:
                mem = 20
        except (TypeError, ValueError):
            mem = 20
        defaults["max_extended_minutes"] = mem
        try:
            mb = int(defaults["max_track_mb"])
            if mb < 0:
                mb = 0
        except (TypeError, ValueError):
            mb = 0
        defaults["max_track_mb"] = mb  # 0 = no file-size limit
        return defaults
    except (OSError, json.JSONDecodeError):
        return defaults


def save_config(config: dict) -> None:
    """Persist user config to disk. Best-effort, swallows IO errors."""
    import json

    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except OSError as exc:
        print(f"[*] Could not save config: {exc}")


class MusicScraper(QThread):
    PlaylistCompleted = pyqtSignal(str)
    PlaylistID = pyqtSignal(str)
    song_Album = pyqtSignal(str)
    song_meta = pyqtSignal(dict)
    add_song_meta = pyqtSignal(dict)
    count_updated = pyqtSignal(int)
    dlprogress_signal = pyqtSignal(int)
    Resetprogress_signal = pyqtSignal(int)
    error_signal = pyqtSignal(str)  # Signal for error messages to UI

    # Max concurrent track downloads. 4 is the measured sweet spot:
    # linear speedup through 4, diminishing returns past 6 (CPU-bound ffmpeg).
    MAX_WORKERS = 4

    def __init__(
        self,
        cancel_event: threading.Event | None = None,
        *,
        audio_format: str = "mp3",
        audio_quality: str = "192",
        extended_mix: bool = False,
        max_extended_minutes: int = 20,
        max_track_mb: int = 0,
        artist_first: bool = False,
    ):
        super().__init__()
        self.counter = 0  # Initialize counter to zero
        self.session = requests.Session()
        self.spotifydown_api = None
        self._cancel_event = cancel_event or threading.Event()
        self._failed_tracks: list[str] = []  # Track failed downloads
        # Output options. audio_format must be a key of SUPPORTED_FORMATS;
        # audio_quality only applies to lossy formats (mp3/m4a/opus).
        self.audio_format = audio_format if audio_format in SUPPORTED_FORMATS else "mp3"
        self.audio_quality = audio_quality if audio_quality in SUPPORTED_QUALITIES else "192"
        self.extended_mix = extended_mix
        try:
            mem = int(max_extended_minutes)
        except (TypeError, ValueError):
            mem = MusicScraper._DEFAULT_MAX_EXTENDED_MINUTES
        self.max_track_duration_s = max(1, mem) * 60
        # Max output file size in bytes (0 = no limit). Guards against grabbing
        # a giant upload (e.g. a 2-hour DJ mix) instead of the intended track.
        try:
            self.max_track_bytes = max(0, int(max_track_mb)) * 1024 * 1024
        except (TypeError, ValueError):
            self.max_track_bytes = 0
        self.artist_first = bool(artist_first)
        self._counter_lock = threading.Lock()
        self._failed_lock = threading.Lock()
        self._filename_lock = threading.Lock()
        self._manifest_lock = threading.Lock()
        self._manifest_path: str | None = None
        self._in_flight_files: set[str] = set()
        # Set to True during parallel playlist downloads so workers can suppress
        # per-track UI noise (label flicker, thumbnail spam, progress bar jitter)
        # that only makes sense for a single active download.
        self._parallel_mode = False
        self._total_tracks = 0

    def is_cancelled(self) -> bool:
        """Check if cancellation has been requested."""
        return self._cancel_event.is_set()

    def _get_user_friendly_error(self, error: Exception, track_title: str = "") -> str:
        """Convert exception to user-friendly error message."""
        if isinstance(error, RateLimitError):
            return "Rate limited by Spotify - waiting..."
        if isinstance(error, NetworkError):
            return "Network error - retrying..."
        if isinstance(error, ExtractionError):
            return f"Could not access '{track_title}' - may be unavailable"
        if "HTTP Error 429" in str(error):
            return "YouTube rate limit - waiting..."
        error_text = str(error).lower()
        if (
            "no video formats" in error_text
            or "no playable audio source" in error_text
            or "unavailable" in error_text
        ):
            return f"'{track_title}' not found on YouTube"
        return f"Error: {str(error)[:50]}"

    def ensure_spotifydown_api(self):
        if self.spotifydown_api is None:
            self.spotifydown_api = PlaylistClient(session=self.session)
        return self.spotifydown_api

    def sanitize_text(self, text):
        """Sanitize text for filename usage."""
        return sanitize_filename(text, allow_spaces=True)

    def _format_track_filename(self, sanitized_title, sanitized_artists, suffix=""):
        """Build the .mp3 filename, honoring the artist_first setting."""
        if self.artist_first:
            stem = f"{sanitized_artists} - {sanitized_title}"
        else:
            stem = f"{sanitized_title} - {sanitized_artists}"
        return f"{stem}{suffix}.mp3"

    def format_playlist_name(self, metadata: PlaylistInfo):
        owner = metadata.owner or "Spotify"
        return f"{metadata.name} - {owner}".strip(" -")

    def prepare_playlist_folder(self, base_folder, playlist_name):
        if not os.path.exists(base_folder):
            os.makedirs(base_folder)
        safe_name = "".join(
            character
            for character in playlist_name
            if character.isalnum() or character in [" ", "_"]
        ).strip()
        if not safe_name:
            safe_name = "Setlist Playlist"
        playlist_folder = os.path.join(base_folder, safe_name)
        os.makedirs(playlist_folder, exist_ok=True)
        return playlist_folder

    @staticmethod
    def _widen_search(search_query: str) -> str:
        """Search several YouTube results instead of only the top hit.

        A track's #1 result can be region-locked or removed; `ytsearch1`
        fails the whole download in that case (closes #42). Widening to
        `ytsearch5` lets yt-dlp skip unavailable results and download the
        first one that actually plays.
        """
        if search_query.startswith("ytsearch1:"):
            return "ytsearch5:" + search_query[len("ytsearch1:") :]
        return search_query

    @staticmethod
    def _simplify_search(search_query: str) -> str:
        """Strip parenthetical/bracketed qualifiers for a looser fallback.

        Hyper-specific titles (classical works like `(Wiegenlied, Op. 49,
        No. 4)`, tone tracks like `(528 Hz)`) can return zero YouTube
        matches. Dropping the qualifiers widens the net on a second attempt.
        Returns the original query unchanged if there is nothing to strip.
        """
        _, sep, terms = search_query.partition(":")
        if not sep:
            terms = search_query
        stripped = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", terms)
        stripped = re.sub(r"\s+", " ", stripped).strip()
        if not stripped or stripped == terms.strip():
            return search_query
        return f"ytsearch5:{stripped}"

    # Max gap (seconds) between the Spotify track length and a YouTube
    # candidate's length for the candidate to count as the "same" recording.
    # The top YouTube hit is often the music video (extra intro/skit/outro) or
    # an extended/remix cut, which plays as a different song even though the
    # filename is right. Matching on duration steers us to the real audio.
    _DURATION_TOLERANCE_S = 7
    _EXTENDED_TITLE_KEYWORDS = ("extended", "club mix")
    _EXTENDED_MAX_RATIO = 2.5  # an extended cut is longer than the edit but never an hour-long mix
    _DEFAULT_MAX_EXTENDED_MINUTES = 20
    _RADIO_EDIT_RE = re.compile(
        r"\s*(?:\(\s*radio\s*edit\s*\)|\[\s*radio\s*edit\s*\]|[-–—]\s*radio\s*edit\b)\s*",
        re.IGNORECASE,
    )

    @staticmethod
    def _strip_radio_edit(title: str) -> str:
        """Remove a "Radio Edit" version descriptor from a title (used only in
        extended-mix mode, where we fetch a longer cut). Leaves the rest of the
        title — including unrelated words like "Radio Ga Ga" — intact."""
        cleaned = MusicScraper._RADIO_EDIT_RE.sub(" ", title)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        cleaned = cleaned.strip(" -–—")
        return cleaned if cleaned else title

    @classmethod
    def _extended_title_boost(cls, entry: dict) -> int:
        title = (entry.get("title") or "").lower()
        return int(any(kw in title for kw in cls._EXTENDED_TITLE_KEYWORDS))

    def _meta_title(self, track_title: str) -> str:
        """Title written to the file's metadata tags and shown in the preview.

        In extended-mix mode the downloaded audio is the extended cut rather
        than the Spotify radio edit, so the title tag is annotated to say so.
        Skipped when the Spotify title already mentions "extended" to avoid a
        doubled "(Extended Mix)" on tracks that are themselves extended edits.
        """
        if self.extended_mix and "extended" not in track_title.lower():
            return f"{track_title} (Extended Mix)"
        return track_title

    def _extended_search_query(self, track_title, artists):
        """Build the YouTube search for the extended cut.

        Only the bare word "extended" is appended (not "extended mix audio").
        Empirically the trailing "audio" token, and to a lesser extent "mix",
        push genuine "(Extended Version)" uploads out of YouTube's top results
        entirely, so the strict-extended leg never sees them. "extended" alone
        is the most general qualifier and matches Extended Version/Mix/Edit via
        the _EXTENDED_TITLE_KEYWORDS title filter applied in _select_youtube_match.
        """
        return f"ytsearch10:{track_title} {artists} extended"

    def _select_youtube_match(self, search_query, expected_duration_s, prefer_extended=False):
        """Return the best YouTube watch URL for a search, or None.

        Flat-searches the query (fast, metadata only). The top hit is trusted
        by default (YouTube's relevance ranking is usually right), so behavior
        matches earlier versions for the common case. We only override it when
        its length is clearly off from the Spotify track (the music-video /
        extended-edit case), and then we pick the closest-length candidate.
        This avoids second-guessing a correct top result and accidentally
        preferring a same-length-but-wrong edit (sped-up, nightcore, remix).
        Skips entries with no usable id.
        """
        select_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "ignoreerrors": True,
            "retries": 5,
            "socket_timeout": 15,
            "concurrent_fragment_downloads": 4,
        }
        try:
            with YoutubeDL(select_opts) as ydl:
                info = ydl.extract_info(search_query, download=False)
        except Exception:
            return None
        entries = [e for e in (info or {}).get("entries", []) if e and e.get("id")]
        if not entries:
            return None

        if prefer_extended:
            timed = [e for e in entries if e.get("duration")]
            if expected_duration_s:
                lower = expected_duration_s + self._DURATION_TOLERANCE_S
                upper = min(
                    expected_duration_s * self._EXTENDED_MAX_RATIO, self.max_track_duration_s
                )
                longer = [
                    e
                    for e in timed
                    if self._extended_title_boost(e) and lower < e["duration"] <= upper
                ]
                if longer:
                    chosen = max(longer, key=lambda e: e["duration"])
                    return f"https://www.youtube.com/watch?v={chosen['id']}"
                # No genuinely-longer cut (e.g. the Spotify track is ALREADY the extended
                # version): take the keyworded candidate closest to the expected length
                # within [expected/ratio, upper]. Else give up so the caller falls back.
                lo = expected_duration_s / self._EXTENDED_MAX_RATIO
                near = [
                    e
                    for e in timed
                    if self._extended_title_boost(e) and lo <= e["duration"] <= upper
                ]
                if near:
                    chosen = min(near, key=lambda e: abs(e["duration"] - expected_duration_s))
                    return f"https://www.youtube.com/watch?v={chosen['id']}"
                return None
            else:
                # No Spotify duration to gate on: NEVER take an unbounded top hit. Require
                # the extended keyword AND a sane single-track length; pick the most
                # relevant (first) such result, else return None to fall back.
                sane_kw = [
                    e
                    for e in timed
                    if self._extended_title_boost(e) and e["duration"] <= self.max_track_duration_s
                ]
                if sane_kw:
                    chosen = sane_kw[0]
                    return f"https://www.youtube.com/watch?v={chosen['id']}"
                return None
        else:
            chosen = entries[0]
            if expected_duration_s:
                top_duration = chosen.get("duration")
                top_off = top_duration is None or (
                    abs(top_duration - expected_duration_s) > self._DURATION_TOLERANCE_S
                )
                # Only look past the top hit when its length is clearly wrong.
                if top_off:
                    timed = [e for e in entries if e.get("duration")]
                    if timed:
                        chosen = min(timed, key=lambda e: abs(e["duration"] - expected_duration_s))
        return f"https://www.youtube.com/watch?v={chosen['id']}"

    def _build_youtube_download_plan(self, search_query, fallback_query=None):
        prefer_extended = self.extended_mix
        if prefer_extended:
            plan = [(self._widen_search(search_query), True)]
            if fallback_query:
                plan.append((self._widen_search(fallback_query), False))
                simp = self._simplify_search(fallback_query)
                if simp not in [q for q, _ in plan]:
                    plan.append((simp, False))
        else:
            plan = [(self._widen_search(search_query), False)]
            fb = self._simplify_search(search_query)
            if fb not in [q for q, _ in plan]:
                plan.append((fb, False))
        return plan

    def download_track_audio(
        self, search_query, destination, expected_duration_s=None, fallback_query=None
    ):
        """Download audio from YouTube to *destination*.

        Returns ``(path, used_extended)`` where *used_extended* is True when the
        strict extended-mix search leg produced the file, False when a normal /
        fallback query did.
        """
        # Check for FFmpeg first
        ffmpeg_path = get_ffmpeg_path()
        if not ffmpeg_path:
            raise RuntimeError(
                "FFmpeg not found! Install via: brew install ffmpeg (macOS) "
                "or apt install ffmpeg (Linux)"
            )

        fmt = self.audio_format if self.audio_format in SUPPORTED_FORMATS else "mp3"
        ext = SUPPORTED_FORMATS[fmt]["ext"]
        is_lossy = SUPPORTED_FORMATS[fmt]["lossy"]

        base, _ = os.path.splitext(destination)
        output_template = base + ".%(ext)s"
        postprocessor = {
            "key": "FFmpegExtractAudio",
            "preferredcodec": fmt,
        }
        if is_lossy:
            postprocessor["preferredquality"] = self.audio_quality

        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "no_warnings": True,
            "outtmpl": output_template,
            "ffmpeg_location": ffmpeg_path,
            "retries": 5,
            "socket_timeout": 15,
            "concurrent_fragment_downloads": 4,
            "ignoreerrors": True,
            "postprocessors": [postprocessor],
        }

        expected_path = base + "." + ext

        # Primary query (widened to 5 results), then a simplified fallback if
        # the first pass produced nothing. For each, pick the duration-closest
        # candidate (avoids grabbing the music video / wrong edit) and download
        # that specific video. Success is decided purely by whether an audio
        # file landed on disk, so a search with no playable source fails loudly
        # instead of silently reporting a path that does not exist.
        too_big = False
        for query, pe in self._build_youtube_download_plan(search_query, fallback_query):
            video_url = self._select_youtube_match(query, expected_duration_s, prefer_extended=pe)
            if not video_url:
                continue
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.extract_info(video_url, download=True)
            except Exception:
                # Availability/network errors are absorbed by ignoreerrors; the
                # file-exists check below is the single source of truth.
                pass
            if os.path.exists(expected_path):
                # Enforce the optional max file-size cap on the FINAL file. A
                # too-large file is the wrong-version signal (e.g. an hour-long
                # mix), so discard it and try the next candidate/fallback.
                if self.max_track_bytes and os.path.getsize(expected_path) > self.max_track_bytes:
                    too_big = True
                    with contextlib.suppress(OSError):
                        os.remove(expected_path)
                    continue
                return expected_path, pe

        if too_big:
            raise RuntimeError(
                f"track exceeds the {self.max_track_bytes // (1024 * 1024)} MB size limit"
            )
        raise RuntimeError("no playable audio source found on YouTube for this track")

    def _resolve_extended_output(
        self,
        final_path,
        used_extended,
        folder,
        sanitized_artists,
        track_title,
        display_title,
        track_id,
    ):
        """After download, decide the final path + metadata title. If extended-mix
        mode fell back to the original (used_extended is False), the audio is NOT an
        extended cut, so rename the file and use the unmarked title instead of
        mislabeling it '(Extended Mix)'. Returns (final_path, meta_title)."""
        if not self.extended_mix or used_extended:
            return final_path, display_title
        # extended mode but fell back to the original -> drop the (Extended Mix) marker.
        # Build the unmarked name via _format_track_filename so it honors artist_first.
        sanitized_title = self.sanitize_text(track_title)
        unmarked = os.path.join(
            folder, self._format_track_filename(sanitized_title, sanitized_artists)
        )
        if unmarked != final_path:
            if os.path.exists(unmarked):
                unmarked = os.path.join(
                    folder,
                    self._format_track_filename(
                        sanitized_title, sanitized_artists, suffix=f" [{track_id}]"
                    ),
                )
            try:
                os.rename(final_path, unmarked)
                final_path = unmarked
            except OSError:
                pass
        return final_path, track_title

    def download_http_file(self, url, destination):
        response = self.session.get(url, stream=True, timeout=60)
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))
        downloaded = 0
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as handle:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                handle.write(chunk)
                downloaded += len(chunk)
                if total:
                    progress = int(downloaded / total * 100)
                    self.dlprogress_signal.emit(progress)
        return destination

    def _download_one_track(self, track, playlist_folder_path, default_cover_url, track_num=0):
        """Download a single track. Runs inside a ThreadPoolExecutor worker.

        Returns None on success, the track title on failure (for _failed_tracks).
        Qt signals emitted here cross thread boundaries via queued connections,
        which is safe.

        In parallel mode (self._parallel_mode), per-track UI noise (song_meta
        preview, per-byte progress) is suppressed because those widgets are
        single-track and would flicker with N workers in flight. add_song_meta
        still fires so ID3 tags + cover art get written to every mp3.

        track_num (1-based) is passed through to song_meta so the ID3 TRCK
        frame can be populated for playlist ordering.
        """
        if self.is_cancelled():
            return None

        track_title = track.title
        if self.extended_mix:
            track_title = self._strip_radio_edit(track_title)
        display_title = self._meta_title(track_title)
        artists = track.artists
        sanitized_title = self.sanitize_text(display_title)
        sanitized_artists = self.sanitize_text(artists)
        filename = self._format_track_filename(sanitized_title, sanitized_artists)
        filepath = os.path.join(playlist_folder_path, filename)

        # Filename collision guard: two different tracks can sanitize to the
        # same filename (e.g. "Café" vs "Cafe"). Under parallel downloads the
        # naive os.path.exists check has a TOCTOU race where both workers pass
        # the check and clobber each other's files. Claim the filename via a
        # lock; if taken, suffix with track id to de-dupe.
        with self._filename_lock:
            if filepath in self._in_flight_files:
                filepath = os.path.join(
                    playlist_folder_path,
                    self._format_track_filename(
                        sanitized_title, sanitized_artists, suffix=f" [{track.id}]"
                    ),
                )
            self._in_flight_files.add(filepath)

        # Per-track cover enrichment. Spotify's playlist embed trackList does
        # not include per-track cover URLs at all, so without this enrichment
        # every track ends up falling back to default_cover_url (the playlist
        # cover). That's the reported bug: "all 300 songs have the same cover".
        # Fix: when cover_url is missing, fetch /embed/track/{id} which has
        # the real visualIdentity.image. The request runs synchronously
        # inside the worker before the YouTube search, so it adds roughly
        # 100-300ms per track in sequential mode. In parallel mode that
        # per-track cost overlaps with downloads running in other workers,
        # so aggregate wall-clock impact on a full playlist stays small.
        cover_url = track.cover_url
        release_date = track.release_date or ""
        if (
            not cover_url
            and track.id
            and self.spotifydown_api is not None
            and not self.is_cancelled()
        ):
            try:
                enriched = self.spotifydown_api.get_track(track.id)
                if enriched:
                    if enriched.cover_url:
                        cover_url = enriched.cover_url
                    if not release_date and enriched.release_date:
                        release_date = enriched.release_date
            except SpotifyDownAPIError:
                pass  # Fall through to default_cover_url

        cover_url = cover_url or default_cover_url
        album_name = track.album or ""

        song_meta = {
            "title": display_title,
            "artists": artists,
            "album": album_name,
            "releaseDate": release_date,
            "cover": cover_url or "",
            "file": filepath,
            "trackNumber": track_num,
        }

        # Emit song_meta so the preview panel shows the current track. With
        # multiple workers running, this label races between workers and ends
        # up showing whichever track most recently started, which is fine
        # (and better than a blank panel).
        self.song_meta.emit(dict(song_meta))

        try:
            if os.path.exists(filepath):
                self._record_in_manifest(track.id, filepath)
                self.add_song_meta.emit(song_meta)
                self._finish_track_ui(ok=True)
                return None

            normal_query = f"ytsearch1:{track_title} {artists} audio"
            expected_dur = (track.duration_ms / 1000) if track.duration_ms else None
            try:
                if self.extended_mix:
                    search_query = self._extended_search_query(track_title, artists)
                    final_path, used_extended = self.download_track_audio(
                        search_query,
                        filepath,
                        expected_duration_s=expected_dur,
                        fallback_query=normal_query,
                    )
                else:
                    final_path, used_extended = self.download_track_audio(
                        normal_query, filepath, expected_duration_s=expected_dur
                    )
            except Exception as error_status:
                error_msg = self._get_user_friendly_error(error_status, track_title)
                self.error_signal.emit(error_msg)
                print(f"[*] Error downloading '{track_title}': {error_status}")
                with self._failed_lock:
                    self._failed_tracks.append(track_title)
                self._finish_track_ui(ok=False)
                return track_title

            if not final_path or not os.path.exists(final_path):
                self.error_signal.emit(f"'{track_title}' - download failed")
                print(f"[*] Download did not produce an audio file for: {track_title}")
                with self._failed_lock:
                    self._failed_tracks.append(track_title)
                self._finish_track_ui(ok=False)
                return track_title

            final_path, meta_title = self._resolve_extended_output(
                final_path,
                used_extended,
                playlist_folder_path,
                sanitized_artists,
                track_title,
                display_title,
                track.id,
            )
            self._record_in_manifest(track.id, final_path)
            song_meta["title"] = meta_title
            song_meta["file"] = final_path
            self.add_song_meta.emit(song_meta)
            self._finish_track_ui(ok=True)
            return None
        finally:
            with self._filename_lock:
                self._in_flight_files.discard(filepath)

    def _finish_track_ui(self, ok: bool) -> None:
        """Update counter + progress bar after a track completes or fails."""
        self.increment_counter()
        if self._parallel_mode and self._total_tracks > 0:
            # Aggregate progress across all workers: show how many tracks are
            # done as a percentage. Avoids the N-workers-jittering-one-bar
            # problem where per-byte emits from 4 downloads make the bar jump.
            pct = int(self.counter / self._total_tracks * 100)
            self.dlprogress_signal.emit(min(pct, 100))
        elif ok:
            self.dlprogress_signal.emit(100)

    def _load_manifest(self, folder: str) -> set:
        """Load the set of track IDs already downloaded into `folder`.

        The manifest is a JSON-lines file inside the folder; each line is a
        `{"id", "file"}` record. Entries whose file is missing are ignored so
        a track the user deleted re-downloads. Returns the set of valid IDs
        and arms `_manifest_path` for incremental appends during this run.
        """
        import json

        path = os.path.join(folder, MANIFEST_FILENAME)
        self._manifest_path = path
        done: set[str] = set()
        if not os.path.exists(path):
            return done
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    track_id = record.get("id")
                    filename = record.get("file")
                    if track_id and filename and os.path.exists(os.path.join(folder, filename)):
                        done.add(track_id)
        except OSError:
            return set()
        return done

    def _record_in_manifest(self, track_id, filepath: str) -> None:
        """Append a completed track to the manifest (thread-safe).

        Append-only JSON-lines so recording a track is O(1) regardless of how
        large the playlist is. Failures are swallowed: the manifest is an
        optimization for resuming, never a hard dependency of a download.
        """
        if not track_id or not self._manifest_path:
            return
        import json

        record = json.dumps({"id": track_id, "file": os.path.basename(filepath)})
        with self._manifest_lock:
            try:
                with open(self._manifest_path, "a", encoding="utf-8") as handle:
                    handle.write(record + "\n")
            except OSError:
                pass

    def scrape_playlist(self, spotify_playlist_link, music_folder):
        # Reset mutable state so repeat invocations on the same scraper
        # instance don't carry stale counters or failure lists.
        with self._counter_lock:
            self.counter = 0
        with self._failed_lock:
            self._failed_tracks.clear()
        with self._filename_lock:
            self._in_flight_files.clear()
        self._parallel_mode = False
        self._total_tracks = 0

        # A playlist or an album both flow through here. detect_spotify_url_type
        # returns ("playlist"|"album", id); albums reuse the same embed-parsing
        # path with the album embed endpoint (closes #38).
        content_type, playlist_id = detect_spotify_url_type(spotify_playlist_link)
        if content_type not in ("playlist", "album"):
            raise ValueError("Expected a playlist or album URL")
        self.PlaylistID.emit(playlist_id)

        # Check cancel before doing any network work. Large playlists can
        # spend real time inside iter_playlist_tracks doing spclient + per
        # track embed fetches; if the user already clicked stop we shouldn't
        # bother.
        if self.is_cancelled():
            self.PlaylistCompleted.emit("Download cancelled")
            return

        try:
            spotify_api = self.ensure_spotifydown_api()
        except SpotifyDownAPIError as exc:
            raise RuntimeError(str(exc)) from exc

        metadata = spotify_api.get_playlist_metadata(playlist_id, content_type=content_type)
        playlist_display_name = self.format_playlist_name(metadata)
        self.song_Album.emit(playlist_display_name)

        playlist_folder_path = self.prepare_playlist_folder(music_folder, playlist_display_name)

        # Resume support: skip tracks already downloaded in a previous run of
        # this folder before fetching their (rate-limited) metadata, so a huge
        # playlist can be finished across multiple sessions (closes #40).
        already_done = self._load_manifest(playlist_folder_path)
        if already_done:
            self.error_signal.emit(
                f"Resuming: skipping {len(already_done)} already-downloaded track(s)"
            )

        # Materialize the generator into a list. iter_playlist_tracks is a
        # generator and generators are not thread-safe. Consuming it upfront
        # also lets us pick the right worker count based on track count.
        # Cancel is checked between yields so very large playlists (where
        # iter_playlist_tracks issues hundreds of spclient + per-track embed
        # requests serially) can abort mid-fetch instead of waiting through
        # the full window before the stop button takes effect.
        expected_total = metadata.track_count or 0
        tracks: list = []
        for track in spotify_api.iter_playlist_tracks(
            playlist_id, content_type=content_type, skip_ids=already_done
        ):
            if self.is_cancelled():
                break
            tracks.append(track)
            if expected_total and len(tracks) % 10 == 0:
                self.error_signal.emit(
                    f"Fetching track metadata ({len(tracks)} of {expected_total})..."
                )
        self._total_tracks = len(tracks)

        if self.is_cancelled():
            self.PlaylistCompleted.emit("Download cancelled")
            return

        self.Resetprogress_signal.emit(0)

        # Small playlists don't benefit from parallelism. Keep 1 worker for
        # playlists under 3 tracks to preserve the single-track UI feel.
        worker_count = 1 if len(tracks) < 3 else min(self.MAX_WORKERS, len(tracks))
        self._parallel_mode = worker_count > 1

        if worker_count == 1:
            for idx, track in enumerate(tracks, start=1):
                if self.is_cancelled():
                    break
                # Reset the per-track progress bar at the top of each iteration
                # so the single-track UI behaves the way it always has.
                self.Resetprogress_signal.emit(0)
                self._download_one_track(
                    track, playlist_folder_path, metadata.cover_url, track_num=idx
                )
        else:
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = [
                        executor.submit(
                            self._download_one_track,
                            track,
                            playlist_folder_path,
                            metadata.cover_url,
                            idx,
                        )
                        for idx, track in enumerate(tracks, start=1)
                    ]
                    for future in concurrent.futures.as_completed(futures):
                        if self.is_cancelled():
                            # Cancel remaining futures that haven't started
                            # yet. In-flight downloads check is_cancelled at
                            # their own top and return early.
                            for f in futures:
                                f.cancel()
                            break
                        try:
                            future.result()
                        except Exception as exc:
                            # _download_one_track handles errors internally;
                            # this catches unexpected framework-level
                            # exceptions only. Surface them to the UI instead
                            # of silently logging.
                            msg = f"Unexpected worker error: {exc}"
                            print(f"[*] {msg}")
                            self.error_signal.emit(msg)
            finally:
                # Reset parallel_mode only after the executor has fully shut
                # down (context manager exit waits on in-flight workers). If
                # we reset inside the `with`, workers that are still running
                # after a break would observe False and start emitting
                # single-track UI signals.
                self._parallel_mode = False

        if self.is_cancelled():
            self.PlaylistCompleted.emit("Download cancelled")
            return

        # Report completion with failed track count
        if self._failed_tracks:
            self.PlaylistCompleted.emit(f"Done! {len(self._failed_tracks)} track(s) failed")
        else:
            self.PlaylistCompleted.emit("Download Complete!")

    def returnSPOT_ID(self, link):
        """Extract playlist ID from Spotify URL."""
        return extract_playlist_id(link)

    def scrape_track(self, spotify_track_link, music_folder):
        """Download a single track from Spotify."""
        url_type, track_id = detect_spotify_url_type(spotify_track_link)
        if url_type != "track":
            raise ValueError("Expected a track URL")

        try:
            spotify_api = self.ensure_spotifydown_api()
        except SpotifyDownAPIError as exc:
            raise RuntimeError(str(exc)) from exc

        track = spotify_api.get_track(track_id)
        self.song_Album.emit("Single Track Download")

        if not os.path.exists(music_folder):
            os.makedirs(music_folder)

        self.Resetprogress_signal.emit(0)

        track_title = track.title
        if self.extended_mix:
            track_title = self._strip_radio_edit(track_title)
        display_title = self._meta_title(track_title)
        artists = track.artists
        sanitized_title = self.sanitize_text(display_title)
        sanitized_artists = self.sanitize_text(artists)
        filename = self._format_track_filename(sanitized_title, sanitized_artists)
        filepath = os.path.join(music_folder, filename)

        album_name = track.album or ""
        release_date = track.release_date or ""
        cover_url = track.cover_url

        song_meta = {
            "title": display_title,
            "artists": artists,
            "album": album_name,
            "releaseDate": release_date,
            "cover": cover_url or "",
            "file": filepath,
            "trackNumber": 1,
        }

        self.song_meta.emit(dict(song_meta))

        if os.path.exists(filepath):
            self.add_song_meta.emit(song_meta)
            self.increment_counter()
            self.PlaylistCompleted.emit("Track already exists!")
            return

        # Download via YouTube search
        normal_query = f"ytsearch1:{track_title} {artists} audio"
        expected_dur = (track.duration_ms / 1000) if track.duration_ms else None
        try:
            if self.extended_mix:
                search_query = self._extended_search_query(track_title, artists)
                final_path, used_extended = self.download_track_audio(
                    search_query,
                    filepath,
                    expected_duration_s=expected_dur,
                    fallback_query=normal_query,
                )
            else:
                final_path, used_extended = self.download_track_audio(
                    normal_query, filepath, expected_duration_s=expected_dur
                )
        except Exception as error_status:
            error_msg = self._get_user_friendly_error(error_status, track_title)
            print(f"[*] Error downloading '{track_title}': {error_status}")
            self.PlaylistCompleted.emit(error_msg)
            return

        if not final_path or not os.path.exists(final_path):
            print(f"[*] Download did not produce an audio file for: {track_title}")
            self.PlaylistCompleted.emit("Download failed - no audio file produced")
            return

        final_path, meta_title = self._resolve_extended_output(
            final_path,
            used_extended,
            music_folder,
            sanitized_artists,
            track_title,
            display_title,
            track.id,
        )
        song_meta["title"] = meta_title
        song_meta["file"] = final_path
        self.add_song_meta.emit(song_meta)
        self.increment_counter()
        self.dlprogress_signal.emit(100)
        self.PlaylistCompleted.emit("Download Complete!")

    def increment_counter(self):
        with self._counter_lock:
            self.counter += 1
            current = self.counter
        self.count_updated.emit(current)  # Emit the signal with the updated count


# Scraper Thread
class ScraperThread(QThread):
    progress_update = pyqtSignal(str)
    # Terminal outcome of THIS download: (status, message) where status is one
    # of download_queue.{DONE,FAILED,CANCELLED,PARTIAL}. The queue controller
    # keys an item's result off this — never off QThread.finished (which always
    # fires, even when run() swallowed an exception) nor off error_signal
    # (which carries non-fatal per-track notices).
    item_finished = pyqtSignal(str, str)

    def __init__(
        self,
        spotify_link,
        music_folder=None,
        cancel_event: threading.Event | None = None,
        *,
        audio_format: str = "mp3",
        audio_quality: str = "192",
        extended_mix: bool = False,
        max_extended_minutes: int = 20,
        max_track_mb: int = 0,
        artist_first: bool = False,
    ):
        super().__init__()
        self.spotify_link = spotify_link
        self.music_folder = music_folder or os.path.join(os.getcwd(), "music")
        self._cancel_event = cancel_event or threading.Event()
        self._last_message = ""
        self.scraper = MusicScraper(
            cancel_event=self._cancel_event,
            audio_format=audio_format,
            audio_quality=audio_quality,
            extended_mix=extended_mix,
            max_extended_minutes=max_extended_minutes,
            max_track_mb=max_track_mb,
            artist_first=artist_first,
        )
        # Capture the terminal status string synchronously IN the worker thread.
        # PlaylistCompleted is emitted from run()'s thread; a DirectConnection
        # slot runs in that same thread, so self._last_message is set before
        # scrape_*() returns (a queued connection could land after run() ends).
        self.scraper.PlaylistCompleted.connect(self._record_completion, Qt.DirectConnection)

    def request_cancel(self):
        """Request cancellation of the download."""
        self._cancel_event.set()

    def _record_completion(self, message):
        self._last_message = message

    def run(self):
        self.progress_update.emit("Scraping started...")
        try:
            # Detect URL type and handle accordingly
            url_type, _ = detect_spotify_url_type(self.spotify_link)
            if url_type == "track":
                self.scraper.scrape_track(self.spotify_link, self.music_folder)
            else:
                self.scraper.scrape_playlist(self.spotify_link, self.music_folder)
            self.progress_update.emit("Scraping completed.")
            status = classify_completion(self._last_message, self._cancel_event.is_set())
        except Exception as e:
            # run() must never raise out of a QThread; a swallowed exception is
            # a failed item (unless the user cancelled).
            self.progress_update.emit(f"{e}")
            self._last_message = str(e)
            status = CANCELLED if self._cancel_event.is_set() else FAILED
        self.item_finished.emit(status, self._last_message)


def _fetch_cover_bytes(url: str) -> bytes | None:
    """Download cover image bytes, returning None on any failure."""
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200 and resp.content:
            return resp.content
    except (requests.RequestException, OSError) as exc:
        print(f"[*] Error fetching cover: {exc}")
    return None


def _write_metadata_mp3(filename: str, tags: dict, cover_bytes: bytes | None) -> None:
    """Write ID3 tags + embedded cover art to an MP3."""
    audio = EasyID3(filename)
    audio["title"] = tags.get("title", "")
    audio["artist"] = tags.get("artists", "")
    audio["album"] = tags.get("album", "")
    audio["date"] = tags.get("releaseDate", "")
    track_num = tags.get("trackNumber") or 0
    if track_num:
        audio["tracknumber"] = str(track_num)
    audio.save()
    if cover_bytes:
        id3 = ID3(filename)
        id3["APIC"] = APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes)
        id3.save()


def _write_metadata_m4a(filename: str, tags: dict, cover_bytes: bytes | None) -> None:
    """Write iTunes atom tags + embedded cover art to an M4A/MP4."""
    from mutagen.mp4 import MP4, MP4Cover

    audio = MP4(filename)
    audio["\xa9nam"] = tags.get("title", "")
    audio["\xa9ART"] = tags.get("artists", "")
    audio["\xa9alb"] = tags.get("album", "")
    date = tags.get("releaseDate", "")
    if date:
        audio["\xa9day"] = date
    track_num = tags.get("trackNumber") or 0
    if track_num:
        audio["trkn"] = [(int(track_num), 0)]
    if cover_bytes:
        audio["covr"] = [MP4Cover(cover_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def _write_metadata_flac(filename: str, tags: dict, cover_bytes: bytes | None) -> None:
    """Write Vorbis comments + embedded cover art to a FLAC."""
    from mutagen.flac import FLAC, Picture

    audio = FLAC(filename)
    audio["title"] = tags.get("title", "")
    audio["artist"] = tags.get("artists", "")
    audio["album"] = tags.get("album", "")
    date = tags.get("releaseDate", "")
    if date:
        audio["date"] = date
    track_num = tags.get("trackNumber") or 0
    if track_num:
        audio["tracknumber"] = str(track_num)
    if cover_bytes:
        pic = Picture()
        pic.type = 3  # Front cover
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = cover_bytes
        audio.add_picture(pic)
    audio.save()


_METADATA_WRITERS = {
    ".mp3": _write_metadata_mp3,
    ".m4a": _write_metadata_m4a,
    ".flac": _write_metadata_flac,
}


class WritingMetaTagsThread(QThread):
    tags_success = pyqtSignal(str)

    def __init__(self, tags, filename):
        super().__init__()
        self.tags = tags
        self.filename = filename

    def run(self):
        """Write tags + cover art synchronously, dispatching on file extension.

        Each container uses a different tag system (ID3 for mp3, iTunes atoms
        for m4a, Vorbis comments for flac). Opus/WAV are skipped with a log
        line; those formats have limited or no standard cover-art story that
        would repay the extra dependency surface for this project's scope.
        """
        try:
            print("[*] FileName : ", self.filename)
            ext = os.path.splitext(self.filename)[1].lower()
            writer = _METADATA_WRITERS.get(ext)
            if writer is None:
                self.tags_success.emit("Tags skipped (unsupported container)")
                return

            cover_bytes = _fetch_cover_bytes(self.tags.get("cover", ""))
            writer(self.filename, self.tags, cover_bytes)
            self.tags_success.emit("Tags added successfully")
        except Exception as e:
            print(f"[*] Error writing meta tags: {e}")


class DownloadThumbnail(QThread):
    thumbnail_ready = pyqtSignal(bytes)  # Signal to safely update UI from main thread

    def __init__(self, url, main_UI):
        super().__init__()
        self.url = url
        self.main_UI = main_UI
        self.thumbnail_ready.connect(self._update_ui)

    def run(self):
        if not self.url:
            return
        try:
            response = requests.get(self.url, stream=True, timeout=10)
            if response.status_code == 200:
                self.thumbnail_ready.emit(response.content)
        except Exception:
            pass  # Silently fail for thumbnails

    def _update_ui(self, data):
        """Update UI from main thread via signal."""
        pic = QImage()
        pic.loadFromData(data)
        self.main_UI.CoverImg.setPixmap(QPixmap(pic))
        self.main_UI.CoverImg.show()


class SettingsDialog(QDialog):
    """Download folder + audio format + quality in one dialog."""

    def __init__(self, parent, config: dict):
        super().__init__(parent)
        self.setWindowTitle("Setlist Settings")
        self.setModal(True)
        # Min width + resizable so the full path fits on any screen; wider
        # default because macOS path strings are long (`/Users/.../Music/...`).
        self.setMinimumWidth(560)
        self.resize(620, self.sizeHint().height())
        self._config = dict(config)

        from PyQt5.QtWidgets import QLineEdit, QSpinBox

        # QLineEdit (read-only) handles arbitrarily long paths cleanly: it
        # elides mid-path with horizontal scroll on focus, rather than
        # truncating and leaving the user guessing. Tooltip always shows the
        # full value.
        self._folder_label = QLineEdit(self._config.get("download_path") or "(not set)")
        self._folder_label.setReadOnly(True)
        self._folder_label.setFrame(False)
        self._folder_label.setCursorPosition(0)
        self._folder_label.setToolTip(self._folder_label.text())
        self._folder_label.setStyleSheet(
            "QLineEdit { background: transparent; color: #FFFFFF; border: none; padding: 0; }"
        )
        browse = QPushButton("Choose folder")
        browse.clicked.connect(self._choose_folder)

        folder_row = QHBoxLayout()
        folder_row.addWidget(self._folder_label, 1)
        folder_row.addWidget(browse)

        header = QLabel("Settings")
        header.setObjectName("settingsHeader")

        self._format_cb = theme.ThemedComboBox()
        for key in SUPPORTED_FORMATS:
            self._format_cb.addItem(key)
        self._format_cb.setCurrentText(self._config.get("format", "mp3"))
        self._format_cb.currentTextChanged.connect(self._on_format_change)

        self._quality_cb = theme.ThemedComboBox()
        for q in SUPPORTED_QUALITIES:
            self._quality_cb.addItem(f"{q} kbps")
        current_q = self._config.get("quality", "192")
        self._quality_cb.setCurrentText(f"{current_q} kbps")
        self._on_format_change(self._format_cb.currentText())

        self._extended_mix_cb = QCheckBox("Prefer extended mix / club mix versions")
        self._extended_mix_cb.setChecked(bool(self._config.get("extended_mix", False)))

        self._max_extended_minutes_spin = QSpinBox()
        self._max_extended_minutes_spin.setRange(1, 180)
        self._max_extended_minutes_spin.setValue(int(self._config.get("max_extended_minutes", 20)))
        self._max_extended_minutes_spin.setToolTip(
            "Reject an extended candidate longer than this (guards against "
            "grabbing an hour-long mix instead of the real extended cut)."
        )

        self._max_track_mb_spin = QSpinBox()
        self._max_track_mb_spin.setRange(0, 2000)
        self._max_track_mb_spin.setValue(int(self._config.get("max_track_mb", 0)))
        self._max_track_mb_spin.setSpecialValueText("No limit")  # shown when value == 0
        self._max_track_mb_spin.setSuffix(" MB")
        self._max_track_mb_spin.setToolTip(
            "Discard any download whose file exceeds this size and try the next "
            "match. 0 = no limit. Applies to all downloads."
        )

        # Filename order as a dropdown (two naming formats) rather than a
        # checkbox. Index 0 -> "Title - Artist" (artist_first=False, default),
        # index 1 -> "Artist - Title" (artist_first=True). Mirrors the stems
        # built in ScraperThread._format_track_filename.
        self._filename_order_cb = theme.ThemedComboBox()
        self._filename_order_cb.addItem("Title - Artist")
        self._filename_order_cb.addItem("Artist - Title")
        self._filename_order_cb.setCurrentIndex(
            1 if bool(self._config.get("artist_first", False)) else 0
        )
        self._filename_order_cb.setToolTip("How downloaded files are named.")

        form = QFormLayout()
        form.addRow("Download folder:", folder_row)
        form.addRow("Audio format:", self._format_cb)
        form.addRow("Audio quality:", self._quality_cb)
        form.addRow("Extended mix:", self._extended_mix_cb)
        form.addRow("Max extended-mix length (minutes):", self._max_extended_minutes_spin)
        form.addRow("Max file size:", self._max_track_mb_spin)
        form.addRow("Filename order:", self._filename_order_cb)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        ok_btn = btns.button(QDialogButtonBox.Ok)
        ok_btn.setObjectName("settingsOkBtn")
        cancel_btn = btns.button(QDialogButtonBox.Cancel)
        cancel_btn.setObjectName("settingsCancelBtn")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(header)
        layout.addLayout(form)
        form.setSpacing(12)
        layout.addWidget(btns)

    def _choose_folder(self):
        start = (
            self._folder_label.text()
            if os.path.isdir(self._folder_label.text())
            else os.path.expanduser("~")
        )
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Download Folder",
            start,
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if folder:
            # Only append "Setlist" when the user picked a non-Setlist folder,
            # otherwise re-selecting the existing destination creates nested
            # Setlist/Setlist/... paths.
            chosen = (
                folder
                if os.path.basename(folder.rstrip(os.sep)) == "Setlist"
                else os.path.join(folder, "Setlist")
            )
            self._folder_label.setText(chosen)
            self._folder_label.setCursorPosition(0)
            self._folder_label.setToolTip(chosen)

    def _on_format_change(self, fmt: str) -> None:
        """Lossless formats (flac/wav) ignore the bitrate selector."""
        is_lossy = SUPPORTED_FORMATS.get(fmt, {}).get("lossy", True)
        self._quality_cb.setEnabled(is_lossy)

    def result_config(self) -> dict:
        self._config["download_path"] = self._folder_label.text()
        self._config["format"] = self._format_cb.currentText()
        self._config["quality"] = self._quality_cb.currentText().split()[0]
        self._config["extended_mix"] = self._extended_mix_cb.isChecked()
        self._config["max_extended_minutes"] = self._max_extended_minutes_spin.value()
        self._config["max_track_mb"] = self._max_track_mb_spin.value()
        self._config["artist_first"] = self._filename_order_cb.currentIndex() == 1
        return self._config


class QueuePanel(QWidget):
    """Embeddable panel to paste many Spotify URLs and watch them download.

    Lives as a page inside the main window's content stack. It holds NO
    download logic: it parses / queues / starts / stops / clears through the
    controller (MainWindow) and re-renders its list from the controller's
    DownloadQueue snapshot whenever the controller asks it to.
    """

    _STATUS_ICON = {
        PENDING: "•",
        ACTIVE: "⬇",
        DONE: "✅",
        FAILED: "❌",
        CANCELLED: "⏹",
        PARTIAL: "⚠",
    }

    def __init__(self, controller):
        super().__init__()
        self._controller = controller

        intro = QLabel(
            "Paste Spotify playlist / album / track URLs — one per line "
            "(spaces and commas work too). Playlists and albums each download "
            "into their own folder; loose tracks share the base folder."
        )
        intro.setWordWrap(True)

        self._paste = QPlainTextEdit()
        self._paste.setPlaceholderText(
            "https://open.spotify.com/playlist/...\n"
            "https://open.spotify.com/album/...\n"
            "https://open.spotify.com/track/..."
        )
        self._paste.setMinimumHeight(96)

        self._add_btn = QPushButton("Add to queue")
        self._add_btn.setObjectName("QueueBtn")
        self._add_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._add_btn.clicked.connect(self._on_add)
        add_row = QHBoxLayout()
        add_row.addStretch(1)
        add_row.addWidget(self._add_btn)

        self._list = QListWidget()
        self._list.setObjectName("trackList")
        self._list.setSelectionMode(QAbstractItemView.NoSelection)
        self._list.setFocusPolicy(Qt.NoFocus)

        self._start_btn = QPushButton("Start")
        self._start_btn.setObjectName("DownloadBtn")
        self._start_btn.setMinimumHeight(38)
        self._start_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._start_btn.clicked.connect(lambda: self._controller.start_queue())
        self._stop_btn = QPushButton("Stop")
        self._stop_btn.setObjectName("QueueBtn")
        self._stop_btn.setMinimumHeight(38)
        self._stop_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._stop_btn.clicked.connect(lambda: self._controller.stop_queue())
        self._stop_btn.setEnabled(False)
        self._clear_btn = QPushButton("Clear")
        self._clear_btn.setObjectName("QueueBtn")
        self._clear_btn.setMinimumHeight(38)
        self._clear_btn.setCursor(QCursor(Qt.PointingHandCursor))
        self._clear_btn.clicked.connect(lambda: self._controller.clear_queue())
        btn_row = QHBoxLayout()
        btn_row.addWidget(self._start_btn)
        btn_row.addWidget(self._stop_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self._clear_btn)

        self._summary = QLabel("")
        self._summary.setObjectName("statusMsg")
        self._summary.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(intro)
        layout.addWidget(self._paste)
        layout.addLayout(add_row)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._summary)
        layout.addLayout(btn_row)

    def _on_add(self):
        added, skipped = self._controller.queue_add_text(self._paste.toPlainText())
        if added:
            self._paste.clear()
        parts = [f"Added {added}"]
        if skipped:
            parts.append(f"skipped {skipped} invalid")
        self._summary.setText(", ".join(parts))

    # ---- driven by the controller to reflect queue state ----
    def refresh(self, items):
        """Rebuild the list from a DownloadQueue snapshot (id-addressed, so a
        full rebuild can never update the wrong row)."""
        self._list.clear()
        for it in items:
            icon = self._STATUS_ICON.get(it.status, "•")
            QListWidgetItem(f"{icon}   {it.display_name}    ·  {it.kind}", self._list)

    def set_running(self, running):
        self._start_btn.setEnabled(not running)
        self._stop_btn.setEnabled(running)
        self._clear_btn.setEnabled(not running)
        self._add_btn.setEnabled(not running)

    def set_summary(self, text):
        self._summary.setText(text)


# Main Window
class MainWindow(QMainWindow, Ui_MainWindow):
    def __init__(self):
        """MainWindow constructor"""
        super().__init__()
        self.setupUi(self)

        # Load persisted user config so format/quality/folder survive restarts
        self._config = load_config()
        self.download_path = self._config.get("download_path") or self._get_default_download_path()
        self._download_path_set = bool(self._config.get("download_path"))
        self._active_threads = []  # Keep references to running threads to prevent GC crashes
        # Download lifecycle: "idle" | "single" | "queue" | "stopping". Replaces
        # the old single _is_downloading bool so the single-URL flow and the
        # multi-item queue can't clobber each other's button / cancel state.
        self._mode = "idle"
        # While _mode == "stopping", records what we're stopping ("single" or
        # "queue") so queue_is_running() and the Download/Stop button don't
        # confuse a single-download stop with a queue stop.
        self._stopping_from = None
        self._cancel_event = threading.Event()  # Cooperative cancel for the active thread
        self.scraper_thread = None

        # Multi-playlist queue: paste many URLs, download them sequentially.
        self._queue = DownloadQueue()
        self._queue_halted = False  # set by Stop so the finished handler won't auto-advance
        self.queue_dialog = None

        # Live per-track download list (Home) + the row currently downloading.
        self._current_track_item = None
        self._current_track_label = ""

        self.PlaylistLink.returnPressed.connect(self.on_returnButton)
        self.DownloadBtn.clicked.connect(self.on_returnButton)

        self.showPreviewCheck.stateChanged.connect(self.show_preview)
        self.show_preview(self.showPreviewCheck.checkState())  # apply initial state

        self.Select_Home.clicked.connect(self.Linkedin)
        self.SettingsBtn.clicked.connect(self.open_settings)

        # Sidebar navigation switches the content stack. Drive it off `toggled`
        # rather than `clicked`: the buttons are checkable and exclusive, and
        # `toggled` fires for every activation path — mouse, keyboard (Space),
        # and assistive tech. (VoiceOver's AXPress flips the checked state but
        # does NOT emit `clicked`, so a clicked-based wiring is dead for AT.)
        self.navHome.toggled.connect(lambda on: self.content.setCurrentIndex(0) if on else None)
        self.navQueue.toggled.connect(lambda on: self._show_queue_page() if on else None)
        self.navHistory.toggled.connect(lambda on: self.content.setCurrentIndex(2) if on else None)

        # Queue is an embedded page now. The controller drives it through
        # self.queue_dialog (refresh / set_running / set_summary), the same
        # interface the old floating dialog exposed.
        self.queue_dialog = QueuePanel(self)
        self.queuePageLayout.addWidget(self.queue_dialog, 1)

        # The Home "Add to Download Queue" button jumps to the Queue page.
        self.QueueBtn.clicked.connect(self.open_queue_dialog)

        # History page.
        self.clearHistoryBtn.clicked.connect(self.clear_history)

        # Hide the Album row in the preview panel: Spotify's unauthenticated
        # embed endpoints do not expose album name anywhere we can reach it,
        # so the field would always be blank. A missing row reads better than
        # a permanently empty label.
        self.label_8.hide()
        self.AlbumText.hide()

        # Idle placeholder in the (empty) track list.
        self.reset_track_list()

    def _get_default_download_path(self):
        """Get a sensible default download path that's writable."""
        # Try user's Music folder first
        home = os.path.expanduser("~")
        music_folder = os.path.join(home, "Music", "Setlist")

        # On Windows, Music might be in a different location
        if sys.platform == "win32":
            try:
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
                )
                music_folder = os.path.join(winreg.QueryValueEx(key, "My Music")[0], "Setlist")
                winreg.CloseKey(key)
            except Exception:
                music_folder = os.path.join(home, "Music", "Setlist")

        return music_folder

    def _ensure_download_path(self):
        """Ensure download path exists and is writable. Returns True if valid."""
        try:
            os.makedirs(self.download_path, exist_ok=True)
            # Test write access
            test_file = os.path.join(self.download_path, ".setlist_test")
            with open(test_file, "w") as f:
                f.write("test")
            os.remove(test_file)
            return True
        except OSError:
            return False

    def _prompt_download_location(self):
        """Prompt user to select download location. Returns True if selected."""
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Download Folder",
            os.path.expanduser("~"),
            QFileDialog.ShowDirsOnly | QFileDialog.DontResolveSymlinks,
        )
        if folder:
            # Keep downloads contained in a "Setlist" subfolder, but avoid
            # creating nested Setlist/Setlist/... paths when the user picked
            # a folder that's already named Setlist.
            if os.path.basename(folder.rstrip(os.sep)) == "Setlist":
                self.download_path = folder
            else:
                self.download_path = os.path.join(folder, "Setlist")
            self._download_path_set = True
            self._config["download_path"] = self.download_path
            save_config(self._config)
            return True
        return False

    @staticmethod
    def _merge_dialog_settings(config: dict, new: dict, download_path: str) -> dict:
        """Apply a SettingsDialog result onto the config.

        Persists EVERY key the dialog returns rather than a hand-maintained
        allowlist — a previous allowlist silently dropped new settings
        (artist_first, max_extended_minutes), so toggling them did nothing.
        download_path is forced to the resolved value.
        """
        merged = dict(config)
        merged.update(new)
        merged["download_path"] = download_path
        return merged

    def open_settings(self):
        """Full settings dialog: folder + audio format + bitrate."""
        cfg_for_dialog = dict(self._config)
        cfg_for_dialog["download_path"] = self.download_path
        dialog = SettingsDialog(self, cfg_for_dialog)
        if dialog.exec_() == QDialog.Accepted:
            new = dialog.result_config()
            if new.get("download_path"):
                self.download_path = new["download_path"]
                self._download_path_set = True
            self._config = self._merge_dialog_settings(self._config, new, self.download_path)
            save_config(self._config)
            self.statusMsg.setText("Settings saved")

    def _ensure_ready_to_download(self):
        """Prompt for / validate the download folder. Returns True if usable."""
        if not self._download_path_set:
            self.statusMsg.setText("Select download location...")
            if not self._prompt_download_location():
                self.statusMsg.setText("Download cancelled - no folder selected")
                return False
        if not self._ensure_download_path():
            self.statusMsg.setText("Cannot write to download folder")
            QMessageBox.warning(
                self,
                "Invalid Download Location",
                f"Cannot write to:\n{self.download_path}\n\nPlease select a different folder.",
            )
            if not self._prompt_download_location():
                return False
        return True

    def _start_scraper(self, url, cancel_event, on_item_finished, item_id=None):
        """Build, wire and start ONE ScraperThread. Shared by the single-URL
        flow and each queue item (so both honor the same format/quality/extended
        settings). ``on_item_finished(status, message)`` is the authoritative
        terminal callback; thread teardown happens in _cleanup_scraper_thread."""
        thread = ScraperThread(
            url,
            self.download_path,
            cancel_event=cancel_event,
            audio_format=self._config.get("format", "mp3"),
            audio_quality=self._config.get("quality", "192"),
            extended_mix=bool(self._config.get("extended_mix", False)),
            max_extended_minutes=self._config.get("max_extended_minutes", 20),
            max_track_mb=self._config.get("max_track_mb", 0),
            artist_first=self._config.get("artist_first", False),
        )
        self.scraper_thread = thread
        self._active_threads.append(thread)  # strong ref until fully finished

        s = thread.scraper
        thread.progress_update.connect(self.update_progress)
        s.song_meta.connect(self.update_song_META)
        s.add_song_meta.connect(self.add_song_META)
        s.dlprogress_signal.connect(self.update_song_progress)
        s.Resetprogress_signal.connect(self.Reset_song_progress)
        s.count_updated.connect(self.update_counter)
        s.song_Album.connect(self.update_AlbumName)
        # error_signal + PlaylistCompleted carry transient/terminal TEXT only;
        # the authoritative item outcome is item_finished, never these.
        s.error_signal.connect(self.update_progress)
        s.PlaylistCompleted.connect(self.update_progress)

        if item_id is not None:
            # Also keep this queue item's row label in sync. song_Album carries
            # the playlist/album name; for a track it emits "Single Track
            # Download", so track names come from song_meta instead.
            s.song_Album.connect(
                lambda name, iid=item_id: self._set_queue_item_name(iid, name, "album")
            )
            s.song_meta.connect(
                lambda meta, iid=item_id: self._set_queue_item_name(
                    iid, meta.get("title", ""), "track"
                )
            )

        thread.item_finished.connect(on_item_finished)
        thread.finished.connect(lambda t=thread: self._cleanup_scraper_thread(t))
        thread.start()

    def _cleanup_scraper_thread(self, thread):
        """Disconnect a finished thread's signals and release it. Disconnecting
        here (rather than relying on deleteLater, which is deferred) stops any
        late queued signals from the old scraper landing on the next item."""
        with contextlib.suppress(TypeError, RuntimeError):
            thread.scraper.disconnect()
        with contextlib.suppress(TypeError, RuntimeError):
            thread.disconnect()
        if thread in self._active_threads:
            self._active_threads.remove(thread)
        # Drop the dangling reference so nothing dereferences a deleted QObject.
        if self.scraper_thread is thread:
            self.scraper_thread = None
        thread.deleteLater()

    @pyqtSlot()
    def on_returnButton(self):
        # The main Download button doubles as a stop control while busy. Route
        # by the real lifecycle so a single-download stop and a queue stop never
        # cross over (and an accidental Enter during "stopping" is harmless).
        if self.queue_is_running():
            self.stop_queue()
            return
        if self._mode in ("single", "stopping"):
            self._stop_download()
            return

        spotify_url = self.PlaylistLink.text().strip()
        if not spotify_url:
            self.statusMsg.setText("Please enter a Spotify URL")
            return

        if not self._ensure_ready_to_download():
            return

        try:
            url_type, _ = detect_spotify_url_type(spotify_url)
        except ValueError as e:
            self.statusMsg.setText(str(e))
            return

        self.statusMsg.setText(f"Detected: {url_type}")
        self._mode = "single"
        self.reset_track_list()
        self.content.setCurrentIndex(0)
        self.navHome.setChecked(True)
        self.DownloadBtn.setText("Stop")
        self.DownloadBtn.setEnabled(True)
        self._cancel_event = threading.Event()
        self._start_scraper(
            spotify_url,
            self._cancel_event,
            on_item_finished=lambda status, msg: self._on_single_finished(status, msg),
        )

    def _on_single_finished(self, status, message):
        """Reset UI after a single (non-queue) download finishes. The terminal
        text is already shown via PlaylistCompleted/error_signal."""
        self._mode = "idle"
        self._stopping_from = None
        self.DownloadBtn.setText("Download")
        self.DownloadBtn.setEnabled(True)

    def _stop_download(self):
        """Stop the current single download via cooperative cancellation."""
        self.statusMsg.setText("Stopping download...")
        self.DownloadBtn.setEnabled(False)
        self._mode = "stopping"
        self._stopping_from = "single"
        self._cancel_event.set()
        if self.scraper_thread is not None and self.scraper_thread.isRunning():
            self.scraper_thread.request_cancel()
        # UI resets when item_finished -> _on_single_finished fires.

    # ---------------------------------------------------------------- queue ---
    def _show_queue_page(self):
        """Refresh + show the embedded Queue page (content stack index 1)."""
        self._refresh_queue_dialog()
        self.queue_dialog.set_running(self.queue_is_running())
        self.content.setCurrentIndex(1)

    def open_queue_dialog(self):
        """Jump to the Queue page via the sidebar nav (e.g. from the Home CTA)."""
        if self.navQueue.isChecked():
            self._show_queue_page()  # already selected — just refresh/show
        else:
            self.navQueue.setChecked(True)  # fires toggled -> _show_queue_page

    def queue_is_running(self):
        # A single-download stop also enters "stopping"; only report the queue
        # as running when the queue itself is the thing running/stopping.
        return self._mode == "queue" or (
            self._mode == "stopping" and self._stopping_from == "queue"
        )

    def queue_add_text(self, text):
        """Parse a paste blob and append valid, de-duplicated URLs to the queue.
        Returns (n_added, n_skipped)."""
        parsed, skipped = parse_playlist_urls(text)
        added = self._queue.add(parsed)
        self._refresh_queue_dialog()
        return len(added), skipped

    def clear_queue(self):
        if self.queue_is_running():
            self.statusMsg.setText("Stop the queue before clearing")
            return
        self._queue.reset()
        self._refresh_queue_dialog()
        if self.queue_dialog is not None:
            self.queue_dialog.set_summary("")

    def start_queue(self):
        if self._mode != "idle":
            self.statusMsg.setText("Finish the current download first")
            return
        if self._queue.next_pending() is None:
            self.statusMsg.setText("Queue is empty — add some URLs first")
            return
        if not self._ensure_ready_to_download():
            return
        self._mode = "queue"
        self._queue_halted = False
        self.reset_track_list()
        self.DownloadBtn.setText("Stop Queue")
        self.DownloadBtn.setEnabled(True)
        self.SettingsBtn.setEnabled(False)  # freeze settings while a queue runs
        if self.queue_dialog is not None:
            self.queue_dialog.set_running(True)
        self._advance_queue()

    def stop_queue(self):
        if not self.queue_is_running():
            return
        self._queue_halted = True  # set BEFORE cancel so finished won't advance
        self._mode = "stopping"
        self._stopping_from = "queue"
        self.statusMsg.setText("Stopping queue…")
        self.DownloadBtn.setEnabled(False)
        self._queue.cancel_pending()  # not-yet-started items become cancelled
        self._refresh_queue_dialog()
        self._cancel_event.set()
        if self.scraper_thread is not None and self.scraper_thread.isRunning():
            self.scraper_thread.request_cancel()
        # The active item's item_finished -> _on_queue_item_finished sees the
        # halt flag and routes to _finish_queue.

    def _advance_queue(self):
        if self._queue_halted:
            self._finish_queue()
            return
        item = self._queue.next_pending()
        if item is None:
            self._finish_queue()
            return
        self._queue.mark(item.id, ACTIVE)
        self._refresh_queue_dialog()
        # Reset the shared progress bar for the new item so a late signal from
        # the previous item can't leave a stale value on screen.
        self.Reset_song_progress(0)
        counts = self._queue.counts()
        done_so_far = counts[DONE] + counts[FAILED] + counts[CANCELLED] + counts[PARTIAL]
        self.statusMsg.setText(f"[{done_so_far + 1}/{counts['total']}] {item.display_name}")
        self._cancel_event = threading.Event()  # FRESH event per item
        self._start_scraper(
            item.url,
            self._cancel_event,
            on_item_finished=lambda status, msg, iid=item.id: self._on_queue_item_finished(
                iid, status, msg
            ),
            item_id=item.id,
        )

    def _on_queue_item_finished(self, item_id, status, message):
        self._queue.mark(item_id, status)
        self._refresh_queue_dialog()
        # Defer so all of this item's queued signals drain before the next item
        # launches (else the new scraper's counter reads 0 and its "Scraping
        # started..." overwrites this item's result text).
        if self._queue_halted:
            QTimer.singleShot(0, self._finish_queue)
        else:
            QTimer.singleShot(0, self._advance_queue)

    def _finish_queue(self):
        if self._mode == "idle":
            return  # already finalized — guard against a double call
        self._mode = "idle"
        self._stopping_from = None
        self._queue_halted = False
        self.DownloadBtn.setText("Download")
        self.DownloadBtn.setEnabled(True)
        self.SettingsBtn.setEnabled(True)
        counts = self._queue.counts()
        summary = f"Queue finished — {counts[DONE]} done"
        if counts[PARTIAL]:
            summary += f", {counts[PARTIAL]} partial"
        if counts[FAILED]:
            summary += f", {counts[FAILED]} failed"
        if counts[CANCELLED]:
            summary += f", {counts[CANCELLED]} cancelled"
        self.statusMsg.setText(summary)
        if self.queue_dialog is not None:
            self.queue_dialog.set_running(False)
            self.queue_dialog.set_summary(summary)

    def _set_queue_item_name(self, item_id, name, source):
        """Update a queue item's display name. ``source`` is "album"
        (playlist/album name via song_Album) or "track" (title via song_meta);
        each is applied only to the matching item kind so the "Single Track
        Download" placeholder never overwrites a real name."""
        item = self._queue.get(item_id)
        if item is None or not name:
            return
        if source == "album" and item.kind == "track":
            return
        if source == "track" and item.kind != "track":
            return
        if self._queue.set_display_name(item_id, name):
            self._refresh_queue_dialog()

    def _refresh_queue_dialog(self):
        if self.queue_dialog is not None:
            self.queue_dialog.refresh(self._queue.items)

    def closeEvent(self, event):
        # Cancel and briefly wait on running download threads so we don't exit
        # with a live QThread (Qt aborts with "QThread destroyed while still
        # running"). The queue makes long multi-item runs common.
        self._queue_halted = True
        self._cancel_event.set()
        for t in list(self._active_threads):
            with contextlib.suppress(RuntimeError):
                if hasattr(t, "request_cancel"):
                    t.request_cancel()
                if t.isRunning():
                    t.wait(3000)  # bounded so close can't hang
        super().closeEvent(event)

    def update_progress(self, message):
        self.statusMsg.setText(message)

    @pyqtSlot(dict)
    def update_song_META(self, song_meta):
        """Update UI with current track info (called BEFORE download starts)."""
        if self.showPreviewCheck.isChecked():
            cover_url = song_meta.get("cover", "")
            if cover_url:
                thumb_thread = DownloadThumbnail(cover_url, self)
                self._active_threads.append(thumb_thread)
                thumb_thread.finished.connect(lambda: self._cleanup_thread(thumb_thread))
                thumb_thread.start()
            artists_full = song_meta.get("artists", "")
            artist_list = [a.strip() for a in artists_full.split(",") if a.strip()]
            if len(artist_list) > 2:
                artists_display = f"{artist_list[0]}, {artist_list[1]} +{len(artist_list) - 2}"
            else:
                artists_display = artists_full
            self.ArtistNameText.setText(artists_display)
            self.ArtistNameText.setToolTip(artists_full)
            self.AlbumText.setText(song_meta.get("album", ""))
            self.SongName.setText(song_meta.get("title", ""))
            self.YearText.setText(song_meta.get("releaseDate", ""))

        self.MainSongName.setText(song_meta.get("title", "") + " - " + song_meta.get("artists", ""))
        self._add_track_row(song_meta.get("title", ""), song_meta.get("artists", ""))
        # NOTE: Meta tags are written in add_song_META (after file exists), not here

    @pyqtSlot(dict)
    def add_song_META(self, song_meta):
        # Each emit means a file finished writing -> mark the current track done.
        self._mark_track_done()
        if self.AddMetaDataCheck.isChecked():
            meta_thread = WritingMetaTagsThread(song_meta, song_meta["file"])
            meta_thread.tags_success.connect(lambda x: self.statusMsg.setText(f"{x}"))
            self._active_threads.append(meta_thread)
            meta_thread.finished.connect(lambda: self._cleanup_thread(meta_thread))
            meta_thread.start()

    def _cleanup_thread(self, thread):
        """Remove finished thread from active list."""
        if thread in self._active_threads:
            self._active_threads.remove(thread)

    @pyqtSlot(str)
    def update_AlbumName(self, AlbumName):
        self.AlbumName.setText("Playlist Name : " + AlbumName)

    @pyqtSlot(int)
    def update_counter(self, count):
        total = 0
        if hasattr(self, "scraper_thread") and self.scraper_thread is not None:
            try:
                total = self.scraper_thread.scraper._total_tracks or 0
            except AttributeError:
                total = 0
        if total > 0:
            self.CounterLabel.setText(f"Songs downloaded {count} of {total}")
        else:
            self.CounterLabel.setText("Songs downloaded " + str(count))

    @pyqtSlot(int)
    def update_song_progress(self, progress):
        self.SongDownloadprogress.setValue(progress)

    @pyqtSlot(int)
    def Reset_song_progress(self, progress):
        self.SongDownloadprogress.setValue(0)

    # ---- live track list (Home) + session history ----
    def reset_track_list(self):
        """Clear the live track list and show the idle placeholder hint."""
        self.trackList.clear()
        self._current_track_item = None
        self._current_track_label = ""
        hint = QListWidgetItem("Tracks will appear here as they download")
        hint.setFlags(Qt.NoItemFlags)  # non-interactive, dimmed
        self.trackList.addItem(hint)
        self._tracks_placeholder = True

    def _add_track_row(self, title, artists):
        if getattr(self, "_tracks_placeholder", False):
            self.trackList.clear()  # drop the idle hint before the first real row
            self._tracks_placeholder = False
        label = f"{title} — {artists}" if artists else (title or "Unknown track")
        item = QListWidgetItem(f"⬇   {label}")
        self.trackList.addItem(item)
        self.trackList.scrollToBottom()
        self._current_track_item = item
        self._current_track_label = label

    def _mark_track_done(self):
        if self._current_track_item is not None:
            with contextlib.suppress(RuntimeError):  # row may have been cleared mid-flight
                self._current_track_item.setText(f"✓   {self._current_track_label}")
            QListWidgetItem(f"✓   {self._current_track_label}", self.historyList)
            self._current_track_item = None

    def clear_history(self):
        self.historyList.clear()

    def show_preview(self, state):
        """Toggle the inline cover/meta preview inside the now-playing card.

        Accepts either a Qt.CheckState or the raw int (2 == checked) that
        QCheckBox.stateChanged emits."""
        on = int(state) == int(Qt.Checked)
        self.previewBox.setVisible(on)
        self.MainSongName.setVisible(not on)

    def Linkedin(self):
        webbrowser.open("https://www.linkedin.com/in/sunny-patel-30b460204/")


# Main
if __name__ == "__main__":
    app = QApplication(sys.argv)
    theme.apply(app)
    Screen = MainWindow()
    Screen.setWindowTitle("Setlist")
    Screen.show()
    sys.exit(app.exec())
