"""Duration validation (no transcode) — ports ``download_validation.go``.

Rejects two failure modes after a lossless file lands on disk:
  * **preview/sample** — ``expected >= 60s`` and ``actual <= 35s``;
  * **wrong recording** — ``expected >= 90s`` and ``|actual - expected| > max(15,
    round(0.25 * expected))``.

CRITICAL (goal §9): in **extended mode** this guard is disabled — the extended
cut is intentionally longer, so the ``max_track_duration_s`` window (applied by
``track_selectors.select_extended``) is the guard instead.

Go's ``math.Round`` is half-away-from-zero; Python's ``round`` is banker's
rounding, which diverges on ``*.5`` (e.g. ``0.25*90 = 22.5`` must round to 23,
not 22), so a half-away-from-zero round is used here.
"""

from __future__ import annotations

import json
import math
import subprocess

PREVIEW_MAX_SECONDS = 35
PREVIEW_EXPECTED_MIN_SECONDS = 60
LARGE_MISMATCH_MIN_EXPECTED = 90
MIN_ALLOWED_DURATION_DIFF = 15
DURATION_DIFF_RATIO = 0.25


def _round_half_away(x: float) -> int:
    """math.Round: round half away from zero (durations here are non-negative)."""
    return int(math.floor(x + 0.5)) if x >= 0 else int(math.ceil(x - 0.5))


def flac_streaminfo_duration(path: str) -> float | None:
    """Duration (seconds) from a FLAC STREAMINFO block, or ``None``.

    Mirrors ``getFlacDuration``: the first metadata block's bytes 10..17 carry
    the sample rate (20 bits) and total samples (36 bits). Avoids an ffprobe
    spawn for the common native-FLAC case.
    """
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"fLaC":
                return None
            # First metadata block header (4 bytes) then its data.
            header = fh.read(4)
            if len(header) < 4:
                return None
            length = (header[1] << 16) | (header[2] << 8) | header[3]
            data = fh.read(length)
            if len(data) < 18:
                return None
            sample_rate = (data[10] << 12) | (data[11] << 4) | (data[12] >> 4)
            total_samples = (
                ((data[13] & 0x0F) << 32)
                | (data[14] << 24)
                | (data[15] << 16)
                | (data[16] << 8)
                | data[17]
            )
            if sample_rate > 0:
                return total_samples / sample_rate
    except OSError:
        return None
    return None


def get_audio_duration(path: str, ffprobe_path: str | None = None) -> float | None:
    """Best-effort float seconds: FLAC STREAMINFO first, else ffprobe. ``None`` if
    undeterminable (which short-circuits validation to 'accept', like the Go)."""
    if path.lower().endswith(".flac"):
        dur = flac_streaminfo_duration(path)
        if dur and dur > 0:
            return dur
    probe = ffprobe_path or "ffprobe"
    try:
        out = subprocess.run(
            [probe, "-v", "quiet", "-print_format", "json", "-show_format", path],
            capture_output=True,
            timeout=30,
        )
        meta = json.loads(out.stdout or b"{}")
        dur_str = (meta.get("format") or {}).get("duration")
        if dur_str:
            return float(dur_str)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def is_acceptable_duration(
    path: str, expected_seconds: int, *, extended: bool = False, ffprobe_path: str | None = None
) -> tuple[bool, str]:
    """Return ``(ok, reason)``. ``ok=False`` means the caller should delete the file.

    Inability to measure the duration → accepted (``ok=True``) just like the Go,
    which never deletes a file it could not probe. In extended mode the guard is
    bypassed entirely (the longer cut is the point).
    """
    if extended:
        return True, ""
    if not path or expected_seconds <= 0:
        return True, ""
    actual = get_audio_duration(path, ffprobe_path)
    if not actual or actual <= 0:
        return True, ""
    actual_seconds = _round_half_away(actual)
    if actual_seconds <= 0:
        return True, ""

    if expected_seconds >= PREVIEW_EXPECTED_MIN_SECONDS and actual_seconds <= PREVIEW_MAX_SECONDS:
        return False, (
            f"detected preview/sample download: file is {actual_seconds}s, "
            f"expected about {expected_seconds}s"
        )
    if expected_seconds >= LARGE_MISMATCH_MIN_EXPECTED:
        allowed = int(
            max(
                float(MIN_ALLOWED_DURATION_DIFF),
                float(_round_half_away(expected_seconds * DURATION_DIFF_RATIO)),
            )
        )
        if abs(actual_seconds - expected_seconds) > allowed:
            return False, (
                f"downloaded file duration mismatch: file is {actual_seconds}s, "
                f"expected about {expected_seconds}s"
            )
    return True, ""
