"""Amazon Music path — proxy + CENC decrypt, no re-encode (``amazon.go``).

Flow: ASIN → proxy ``/api/track/<ASIN>`` (authed with the AES-GCM-decrypted
``X-Debug-Key``) → ``{streamUrl, decryptionKey}`` → download the encrypted MP4 →
``ffprobe`` the audio codec → **only if it is ``flac``** decrypt with
``ffmpeg -decryption_key <hex> -c copy`` (genuine lossless, no transcode) into a
``.flac``. A non-FLAC (lossy AAC) Amazon result counts as "no lossless here" for
the Real FLAC backend — it raises :class:`NotLosslessError` so the orchestrator
tries the next service (goal §8).
"""

from __future__ import annotations

import contextlib
import os
import subprocess

import requests

from . import constants as C
from ._util import aesgcm_decrypt, default_headers
from .bridge import extract_amazon_asin
from .errors import NotFoundOnServiceError, NotLosslessError, ServiceUnavailableError


class AmazonClient:
    def __init__(self, session: requests.Session, ffmpeg: str | None, ffprobe: str | None) -> None:
        self._session = session
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe
        self._debug_key: str | None = None

    def _get_debug_key(self) -> str:
        if self._debug_key is None:
            self._debug_key = aesgcm_decrypt(
                C.AMAZON_DEBUG_KEY_SEED_PARTS,
                C.AMAZON_DEBUG_KEY_NONCE,
                C.AMAZON_DEBUG_KEY_CIPHERTEXT,
                C.AMAZON_DEBUG_KEY_TAG,
                C.AMAZON_DEBUG_KEY_AAD,
            ).decode()
        return self._debug_key

    def fetch_flac(self, amazon_url: str, out_path: str, cancel, max_bytes: int = 0) -> str:
        """Resolve + download a genuine FLAC to *out_path*. Raises
        :class:`NotLosslessError` when Amazon only has a lossy (AAC) copy."""
        asin = extract_amazon_asin(amazon_url)
        if not asin:
            raise NotFoundOnServiceError(f"could not extract ASIN from {amazon_url}")
        if not self._ffmpeg or not self._ffprobe:
            raise ServiceUnavailableError("ffmpeg/ffprobe required for the Amazon path")

        info_url = f"{C.AMAZON_API_BASE}/api/track/{asin}"
        headers = default_headers({"X-Debug-Key": self._get_debug_key()})
        resp = self._session.get(info_url, headers=headers, timeout=120)
        if resp.status_code != 200:
            raise ServiceUnavailableError(f"Amazon API returned status {resp.status_code}")
        data = resp.json()
        stream_url = (data.get("streamUrl") or "").strip()
        dec_key = (data.get("decryptionKey") or "").strip()
        if not stream_url:
            raise NotFoundOnServiceError("no stream URL in Amazon response")
        # No decryption key => we can only keep the raw encrypted/lossy stream,
        # which isn't a genuine lossless FLAC. Treat as "no lossless here".
        if not dec_key:
            raise NotLosslessError("Amazon returned no decryption key (lossy)")

        enc_path = out_path + ".enc.m4a"
        self._download(stream_url, enc_path, cancel, max_bytes)
        try:
            codec = self._probe_codec(enc_path)
            if codec != "flac":
                raise NotLosslessError(
                    f"Amazon track codec is '{codec or 'unknown'}' (not lossless)"
                )
            self._decrypt_copy(enc_path, dec_key, out_path)
        finally:
            with contextlib.suppress(OSError):
                os.remove(enc_path)
        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise ServiceUnavailableError("Amazon decrypt produced no output")
        return out_path

    def _download(self, url: str, path: str, cancel, max_bytes: int) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            resp = self._session.get(url, headers=default_headers(), stream=True, timeout=120)
            resp.raise_for_status()
            written = 0
            with open(path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if cancel():
                        raise ServiceUnavailableError("cancelled")
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise NotFoundOnServiceError("track exceeds the size limit")
        except (requests.RequestException, OSError) as exc:
            with contextlib.suppress(OSError):
                os.remove(path)
            raise ServiceUnavailableError(f"Amazon stream download failed: {exc}") from exc
        except (NotFoundOnServiceError, ServiceUnavailableError):
            with contextlib.suppress(OSError):
                os.remove(path)
            raise

    def _probe_codec(self, path: str) -> str:
        """codec_name of the first audio stream (CENC leaves it readable). ``""``
        on any ffprobe failure (which then fails the lossless gate)."""
        try:
            out = subprocess.run(
                [
                    self._ffprobe,
                    "-v",
                    "quiet",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_name",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True,
                timeout=60,
            )
            return out.stdout.decode(errors="ignore").strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    def _decrypt_copy(self, enc_path: str, dec_key: str, out_path: str) -> None:
        """CENC-decrypt with ``-c copy`` (no re-encode) into a native ``.flac``."""
        try:
            proc = subprocess.run(
                [
                    self._ffmpeg,
                    "-decryption_key",
                    dec_key,
                    "-i",
                    enc_path,
                    "-c",
                    "copy",
                    # Drop the MP4 ftyp/format tags (major_brand, compatible_brands,
                    # encoder=Lavf...) so they don't leak into the FLAC's Vorbis
                    # comments; -c copy keeps the audio frames bit-identical.
                    "-map_metadata",
                    "-1",
                    "-bitexact",
                    "-y",
                    out_path,
                ],
                capture_output=True,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ServiceUnavailableError(f"Amazon ffmpeg decrypt failed: {exc}") from exc
        if proc.returncode != 0:
            tail = proc.stderr.decode(errors="ignore")[-500:]
            with contextlib.suppress(OSError):
                os.remove(out_path)
            raise ServiceUnavailableError(f"Amazon ffmpeg decrypt failed: {tail}")
