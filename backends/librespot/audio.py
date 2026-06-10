"""Native OGG/Vorbis 320k capture — the core deliverable.

Reads the decrypted-but-still-Vorbis-encoded bytes from a librespot ``LoadedStream``
and writes them verbatim to ``.ogg``: NO decode, NO re-encode, NO ffmpeg. This is the
deliberate divergence from the Rust ``spotify-dl`` reference, which decodes to PCM and
re-encodes ("fake lossless"). We keep Spotify's exact stream.

Header handling: the pinned librespot commit already skips the 167-byte (``0xA7``)
Spotify header inside ``content_feeder().load`` and returns a stream positioned at the
first ``OggS`` page, so the bytes we read already start with ``OggS``. :func:`capture_ogg`
still trims defensively to the first ``OggS`` so a future upstream change that stops
skipping can't corrupt the output (in the normal case byte 0 is ``OggS`` and the scan is
a no-op that never reaches into real audio).
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import Callable

from . import _librespot as adapter
from . import track_metadata
from .errors import LibrespotCancelled, OggCaptureError

OGG_MAGIC = b"OggS"
DEFAULT_CHUNK = 64 * 1024

# The Spotify proprietary header is 167 (0xA7) bytes and the pinned librespot commit
# already skips it, so a valid stream begins with "OggS" at offset 0. We only scan a
# small bounded prefix for the first page so we NEVER trim into real audio (a later
# "OggS" page boundary); if no "OggS" appears within this window the stream is
# mispositioned/corrupt and we fail (the backend then falls back to YouTube).
MAX_HEADER_SCAN = 1024

# An Ogg page header is 27 bytes (capture pattern "OggS" + version + header_type +
# granule + serial + seqno + crc + segment count) followed by the segment table.
# Spotify's native stream can carry a few trailing bytes AFTER the final page. mutagen
# tolerates such a tail when READING but raises when REWRITING tags (its page renumber
# walks to EOF and trips on the stray bytes), so the tag writer silently fails. We trim
# back to the end-of-stream page so the file is safe to tag — but only a SMALL trailer,
# and only past a real EOS page, so a truncated capture is never reshaped (see
# :func:`_trim_to_last_page`).
OGG_PAGE_HEADER_LEN = 27
MAX_OGG_TRAILER = 4096

# Audio-key/CDN fetches fail transiently ("Failed fetching audio key"); mirror the
# Rust reference's 3 retries with 10–30s exponential backoff.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFFS = (10, 20, 30)

DEFAULT_QUALITIES = ("VERY_HIGH", "HIGH")


def _is_unavailable(exc: Exception) -> bool:
    """No suitable Vorbis file exists (FeederException) — trying again won't help."""
    msg = str(exc).lower()
    return "suitable audio file" in msg


def _is_restricted(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "restricted" in msg or "unrecognized" in msg


def _is_transient(exc: Exception) -> bool:
    """Audio-key / network / chunk errors that warrant a backoff+retry."""
    if isinstance(exc, (IOError, OSError)):
        return True
    # Audio-key timeouts surface as queue.Empty; chunk/CDN waits as Timeout — match by
    # class name so we don't have to import those types here.
    name = type(exc).__name__.lower()
    if name in ("empty", "timeout", "timeouterror", "chunkexception", "cdnexception"):
        return True
    msg = str(exc).lower()
    return (
        "audio key" in msg
        or "timed out" in msg
        or "timeout" in msg
        or "connection" in msg
        or "chunk" in msg
        or "status" in msg
    )


def _interruptible_sleep(seconds: float, cancel: Callable[[], bool], sleep) -> None:
    """Sleep up to *seconds*, polling *cancel* ~2x/sec so Stop stays responsive."""
    remaining = float(seconds)
    step = 0.5
    while remaining > 0:
        if cancel():
            raise LibrespotCancelled("cancelled during retry backoff")
        sleep(min(step, remaining))
        remaining -= step


def capture_ogg(
    loaded_stream,
    dest_tmp: str,
    cancel: Callable[[], bool],
    *,
    chunk_size: int = DEFAULT_CHUNK,
) -> int:
    """Pump the native OGG bytes from *loaded_stream* into *dest_tmp*.

    Returns the number of bytes written. Polls *cancel* each iteration. Trims any
    leading bytes before the first ``OggS`` page (defensive; usually a no-op).
    Terminates on EOF, NOT on ``written == size`` — ``size`` includes the 167-byte
    header the library already skipped, so we read fewer bytes than ``size``.
    """
    in_stream = loaded_stream.input_stream
    reader = in_stream.stream()
    written = 0
    try:
        with open(dest_tmp, "wb") as out:
            # First, locate the first OGG page within a bounded prefix and trim any
            # leading bytes before it (normally zero — the lib already skipped 0xA7).
            head = _read_to_ogg_start(reader, cancel, chunk_size)
            out.write(head)
            written += len(head)
            # Then pump the remainder verbatim until EOF.
            while True:
                if cancel():
                    raise LibrespotCancelled("cancelled during OGG capture")
                data = _safe_read(reader, chunk_size)
                if not data:
                    break
                out.write(data)
                written += len(data)
    finally:
        with contextlib.suppress(Exception):
            reader.close()

    if written <= 0:
        raise OggCaptureError("librespot stream produced no audio bytes")
    return written


def _safe_read(reader, n: int) -> bytes:
    """``reader.read(n)`` but treat the upstream EOF-at-128KiB-boundary as EOF.

    The pinned ``AbsChunkedInputStream.read(n>0)`` does not guard ``pos == size`` for
    positive reads, so when the total size is an exact 128 KiB multiple the read past
    the end indexes past the chunk list and raises ``IndexError`` instead of returning
    ``b""``. That only happens once every byte has been delivered, so we map it to EOF
    (otherwise a fully-downloaded file would be discarded as a failed attempt)."""
    try:
        return reader.read(n)
    except IndexError:
        return b""


def _trim_to_last_page(path: str) -> int:
    """Truncate a small trailer past the Ogg end-of-stream page; return bytes removed.

    Walks the page chain from the start using each page's segment table. Trims the
    remainder ONLY when the last COMPLETE page is the logical end-of-stream page (the
    EOS flag, ``header_type & 0x04``, is set) AND the trailer is small
    (``< MAX_OGG_TRAILER``). Gating on EOS is the safety guarantee: a genuinely
    truncated capture (stream cut mid-song, so the last complete page is NOT EOS and a
    partial real page follows) is left ALONE rather than being reshaped into a
    short-but-clean file that masquerades as complete. A well-formed Spotify stream
    always ends in an EOS page, so its few trailing bytes are still trimmed. Best-effort:
    any error leaves the file as-is. The point is to make the native ``.ogg`` rewritable
    by mutagen so tag writing — album, cover, etc. — doesn't silently fail on tracks
    whose stream carries a trailing byte or two past the final page.
    """
    try:
        with open(path, "r+b") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            off = 0
            last_good = 0
            last_is_eos = False
            while True:
                fh.seek(off)
                header = fh.read(OGG_PAGE_HEADER_LEN)
                if len(header) < OGG_PAGE_HEADER_LEN or header[:4] != OGG_MAGIC:
                    break  # not the start of a complete page -> end of the page chain
                nsegs = header[26]
                seg_table = fh.read(nsegs)
                if len(seg_table) < nsegs:
                    break  # truncated segment table -> incomplete final page
                page_end = off + OGG_PAGE_HEADER_LEN + nsegs + sum(seg_table)
                if page_end > size:
                    break  # page body runs past EOF -> incomplete final page
                off = last_good = page_end
                last_is_eos = bool(header[5] & 0x04)  # header_type: 0x04 = end-of-stream
            remainder = size - last_good
            if last_is_eos and 0 < remainder < MAX_OGG_TRAILER:
                fh.truncate(last_good)
                return remainder
    except OSError:
        pass
    return 0


def _read_to_ogg_start(reader, cancel: Callable[[], bool], chunk_size: int) -> bytes:
    """Read until the first ``OggS`` page is found in a bounded prefix; return the
    buffered bytes starting at that page. Raises if no page header is found near the
    start (mispositioned/corrupt stream) so the caller can fall back rather than
    write a file mutagen can't parse."""
    buf = b""
    limit = MAX_HEADER_SCAN + len(OGG_MAGIC)
    while len(buf) < limit:
        if cancel():
            raise LibrespotCancelled("cancelled during OGG capture")
        data = _safe_read(reader, chunk_size)
        if not data:
            break
        buf += data
    idx = buf.find(OGG_MAGIC, 0, limit)
    if idx < 0:
        raise OggCaptureError("no OggS page near stream start (bad header skip / not Vorbis?)")
    return buf[idx:]


def fetch_track_ogg(
    session_raw,
    base62: str,
    *,
    dest: str,
    cancel: Callable[[], bool],
    on_status: Callable[[str], None] | None = None,
    on_metadata: Callable[[dict | None], None] | None = None,
    qualities=DEFAULT_QUALITIES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoffs=DEFAULT_BACKOFFS,
    sleep=time.sleep,
) -> str:
    """Capture native OGG for Spotify track *base62* to *dest* (atomic via .part).

    Tries each quality tier in *qualities* with a STRICT picker (VERY_HIGH = ~320k
    OGG, HIGH = ~160k OGG — both native ``.ogg``). A tier with no matching Vorbis file
    raises FeederException and we drop to the next tier (and tell the user the 320k cut
    wasn't available, rather than silently shipping a lower bitrate). Transient
    audio-key/network errors retry with exponential backoff. Raises
    :class:`OggCaptureError` if no native OGG could be captured, or
    :class:`LibrespotCancelled` if the user cancelled.

    On a successful capture, *on_metadata* (if given) is called once with the clean
    Spotify metadata extracted from the ``LoadedStream.track`` protobuf — the album
    name + clean artist names the spotifydown embed lacks. It is invoked
    best-effort (any extraction error is swallowed) so a metadata hiccup can never
    turn a fully-downloaded file into a failed attempt.
    """
    track_id = adapter.track_id_from_base62(base62)
    tmp = dest + ".part"
    last_error: Exception | None = None

    for tier_index, quality_name in enumerate(qualities):
        quality = adapter.vorbis_quality(quality_name)
        for attempt in range(max_attempts):
            if cancel():
                _cleanup(tmp)
                raise LibrespotCancelled("cancelled before stream load")
            try:
                loaded = adapter.load_loaded_stream(session_raw, track_id, quality)
                capture_ogg(loaded, tmp, cancel)
                # Trim any trailing non-page bytes so the file is rewritable by the
                # mutagen tag writer (Spotify streams can leave a tiny trailer).
                trimmed = _trim_to_last_page(tmp)
                if trimmed:
                    _emit(on_status, f"Trimmed {trimmed} trailing byte(s) past the last Ogg page.")
                os.replace(tmp, dest)
                if on_metadata is not None:
                    # Best-effort: surface the real Spotify track metadata captured
                    # alongside the stream. NEVER let an extraction error fail an
                    # already-written file (it would be retried/discarded otherwise).
                    with contextlib.suppress(Exception):
                        on_metadata(track_metadata.extract_track_metadata(loaded))
                if tier_index > 0:
                    _emit(
                        on_status,
                        f"Native 320k OGG unavailable for this track; saved {quality_name} "
                        "OGG instead.",
                    )
                return dest
            except LibrespotCancelled:
                _cleanup(tmp)
                raise
            except Exception as exc:  # noqa: BLE001 - upstream raises broad types
                last_error = exc
                _cleanup(tmp)
                if _is_unavailable(exc) or _is_restricted(exc):
                    break  # no point retrying this quality; try the next one
                if attempt + 1 < max_attempts and _is_transient(exc):
                    _emit(
                        on_status,
                        f"Spotify stream hiccup ({quality_name}), retrying "
                        f"{attempt + 1}/{max_attempts}…",
                    )
                    _interruptible_sleep(backoffs[min(attempt, len(backoffs) - 1)], cancel, sleep)
                    continue
                break  # non-transient, or out of attempts -> next quality
    raise OggCaptureError(f"could not capture native OGG for {base62}: {last_error}")


def _cleanup(path: str) -> None:
    with contextlib.suppress(OSError):
        if os.path.exists(path):
            os.remove(path)


def _emit(on_status: Callable[[str], None] | None, msg: str) -> None:
    if on_status is not None:
        with contextlib.suppress(Exception):
            on_status(msg)
