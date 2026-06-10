"""Tidal path — user-supplied API instance (``tidal.go``).

Disabled unless the user configures ``tidal_api_url``. Given a Tidal track id
(from the Songlink bridge), it asks the instance for a download manifest and
resolves it two ways:
  * **BTS JSON manifest** — ``urls[0]`` is a direct FLAC URL, byte-copied to disk;
  * **DASH MPD manifest** — the highest-bandwidth FLAC representation's segments
    (init + media) are raw-concatenated, then **remuxed with ``ffmpeg -c copy``**
    into a native ``.flac``.

DIVERGENCE (intentional): SpotiFLAC's Go always re-encodes DASH with ``-c:a flac``.
This backend's contract is "genuine lossless, never transcode" (goal §0, §7a), so
the FLAC-codec DASH stream is **remuxed with ``-c copy``** (no re-encode) instead.
The mandatory lossless guard (§7b) still aborts to the next service if the
instance ever hands back a lossy stream for a lossless request.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import subprocess
import xml.etree.ElementTree as ET

import requests

from ._util import default_headers
from .errors import NotFoundOnServiceError, NotLosslessError, ServiceUnavailableError

_INIT_RE = re.compile(r'initialization="([^"]+)"')
_MEDIA_RE = re.compile(r'media="([^"]+)"')
_S_TAG_RE = re.compile(r"<S\s+[^>]*>")
_R_ATTR_RE = re.compile(r'r="(\d+)"')

_LOSSLESS_QUALITIES = ("LOSSLESS", "HI_RES", "HI_RES_LOSSLESS")


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def map_quality(lossless_quality: str) -> str:
    """Map the app's "27"|"7"|"6" tier to a Tidal quality token."""
    return "HI_RES_LOSSLESS" if str(lossless_quality).strip() == "27" else "LOSSLESS"


def _is_hires(quality: str) -> bool:
    return quality.strip().upper() in ("HI_RES", "HI_RES_LOSSLESS")


_LOSSLESS_TIERS = ("LOSSLESS", "HI_RES", "HI_RES_LOSSLESS")
_WORD_RE = re.compile(r"[a-z0-9]+")


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _norm(text: str) -> str:
    """Lowercase + collapse to alphanumeric tokens for loose title/artist match."""
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _primary_artist(artists: str) -> str:
    """First artist name, normalized (Spotify ships 'A, B' / 'A & B' / 'A feat. B')."""
    first = re.split(r"\s*(?:,|&|feat\.?|ft\.?|with|x)\s+", artists or "", maxsplit=1)[0]
    return _norm(first)


# Editorial/promo cruft Spotify bakes into titles ("**Highest New Entry**",
# "[ANR092]") that poisons a Tidal text-search query without changing the song.
_TITLE_CRUFT_RE = re.compile(r"\*\*.*?\*\*|\[[^\]]*\]")


def _clean_search_title(title: str) -> str:
    """Strip promo tags from a title for the text-search QUERY only.

    Removes ``**...**`` emphasis spans and ``[...]`` bracketed tags, then
    collapses whitespace. Normal titles (incl. ``(Extended Mix)``) are
    untouched. Matching in :meth:`_pick` still uses the original title, so this
    only widens what the Tidal catalog search returns — it never loosens the
    accept check."""
    cleaned = _TITLE_CRUFT_RE.sub(" ", title or "")
    return " ".join(cleaned.split()).strip()


class TidalClient:
    def __init__(self, session: requests.Session, api_url: str, ffmpeg: str | None) -> None:
        self._session = session
        self._api_url = (api_url or "").strip().rstrip("/")
        self._ffmpeg = ffmpeg
        # A normalized http:// instance URL is always loopback (see
        # normalize_tidal_api_url). The manifest call to it must NOT be routed
        # through an HTTP_PROXY from the environment — that would defeat the
        # "stays local" guarantee. Segment downloads (_download) still honor the
        # session/env proxy, since they fetch from the remote Tidal CDN.
        self._manifest_proxies = (
            {"http": None, "https": None} if self._api_url.startswith("http://") else None
        )

    def fetch_flac(
        self, track_id: int, out_path: str, quality: str, cancel, max_bytes: int = 0
    ) -> str:
        """Resolve + download a genuine FLAC. Walks the quality fallback
        (HI_RES* → LOSSLESS) before giving up."""
        if not self._api_url:
            raise NotFoundOnServiceError("no configured Tidal instance")
        qualities = [quality]
        if _is_hires(quality):
            qualities.append("LOSSLESS")
        last: Exception | None = None
        for qual in qualities:
            try:
                return self._fetch_one(track_id, out_path, qual, cancel, max_bytes)
            except NotLosslessError as exc:
                last = exc  # lossy at this tier -> try a lower tier / next service
            except (NotFoundOnServiceError, ServiceUnavailableError) as exc:
                last = exc
        raise last or NotFoundOnServiceError("Tidal resolution failed")

    # -- track-id resolution via the instance's OWN search ------------------ #
    def find_track_id(
        self, isrc: str | None = None, title: str = "", artists: str = ""
    ) -> int | None:
        """Resolve a Tidal track id using the configured instance's search.

        Tries the authoritative ISRC lookup first (``/search/?i=<isrc>`` →
        Tidal's native ``/v1/tracks?isrc=`` index, the exact recording), then a
        title+artist text search (``/search/?s=``). Returns the first streamable,
        lossless-capable match, or ``None``.

        This is the primary Tidal resolver: it hits the user's own authenticated
        instance directly, so unlike the public Deezer→Songlink bridge it isn't
        subject to third-party rate limits (api.song.link 429s under load) or
        missing-mapping gaps — the chief cause of "no lossless source found" on
        tracks that plainly exist on Tidal.
        """
        if not self._api_url:
            return None
        if isrc and isrc.strip():
            # ISRC pins the exact recording; trust it (don't require a title match).
            tid = self._pick(self._search("i", isrc.strip().upper()), strict=False)
            if tid:
                return tid
        if title:
            query_title = _clean_search_title(title) or title
            query = " ".join(p for p in (artists, query_title) if p).strip()
            tid = self._pick(self._search("s", query), title=title, artists=artists, strict=True)
            if tid:
                return tid
        return None

    def fetch_track_info(self, track_id: int) -> dict | None:
        """The instance's ``/info/?id=<id>`` track JSON (or ``None``).

        Carries the rich tag fields (title, artists[], album.title, trackNumber,
        volumeNumber, streamStartDate, album cover uuid) that
        :func:`lossless.metadata.extract_tidal_metadata` turns into tags.
        Best-effort: any error returns ``None`` so tagging never fails a
        download. Reuses the loopback proxy bypass."""
        if not self._api_url or not track_id:
            return None
        try:
            resp = self._session.get(
                f"{self._api_url}/info/",
                params={"id": track_id},
                headers=default_headers(),
                timeout=5,
                proxies=self._manifest_proxies,
            )
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _search(self, param: str, value: str) -> list:
        """One instance ``/search/`` call → its ``data.items`` list (or ``[]``)."""
        try:
            resp = self._session.get(
                f"{self._api_url}/search/",
                params={param: value, "limit": 25},
                headers=default_headers(),
                timeout=5,
                proxies=self._manifest_proxies,
            )
        except requests.RequestException:
            return []
        if resp.status_code != 200:
            return []
        try:
            body = resp.json()
        except ValueError:
            return []
        data = body.get("data") if isinstance(body, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []

    def _pick(self, items, title: str = "", artists: str = "", strict: bool = False) -> int | None:
        """Choose the best track id from search ``items``.

        Skips non-streamable entries. In ``strict`` mode (text search) the
        candidate title must overlap the requested title and the primary artist
        must appear, to avoid grabbing an unrelated song.

        A single ISRC maps to MANY Tidal releases (single, album, compilations,
        DJ/"mini mix" entries) — all the same recording, so the audio is
        identical, but the album metadata is not. Rank by Tidal ``popularity``
        (then lowest track number) so the canonical single/original release wins
        over an obscure compilation — e.g. "Alone" (pop 41, trk 1) beats "Jan
        Blomqvist Mini Mix" (pop 16, trk 7). Lossless-capable entries always
        outrank lossy ones; the fetch's lossless guard is the final backstop."""
        want_t = _norm(title)
        want_a = _primary_artist(artists)
        best: int | None = None
        best_key = None
        for it in items:
            if not isinstance(it, dict):
                continue
            tid = it.get("id")
            if not tid:
                continue
            if it.get("allowStreaming") is False or it.get("streamReady") is False:
                continue
            if strict:
                cand_t = _norm(it.get("title") or "")
                names = _norm(
                    " ".join(
                        a.get("name", "") for a in (it.get("artists") or []) if isinstance(a, dict)
                    )
                )
                if want_t and want_t not in cand_t and cand_t not in want_t:
                    continue
                if want_a and want_a not in names:
                    continue
            lossless = (it.get("audioQuality") or "").strip().upper() in _LOSSLESS_TIERS
            pop = _safe_int(it.get("popularity"))
            trk = _safe_int(it.get("trackNumber"))
            # Higher tier, then higher popularity, then lower track number (singles
            # are track 1). Negate the track number so smaller sorts larger; a
            # missing/0 number sorts LAST (not ahead of track 1) via the big floor.
            key = (1 if lossless else 0, pop, -(trk or 10**9))
            if best is None or key > best_key:
                best, best_key = int(tid), key
        return best

    def _fetch_one(self, track_id, out_path, quality, cancel, max_bytes) -> str:
        url = f"{self._api_url}/track/?id={track_id}&quality={quality}"
        try:
            resp = self._session.get(
                url, headers=default_headers(), timeout=5, proxies=self._manifest_proxies
            )
        except requests.RequestException as exc:
            raise ServiceUnavailableError(f"Tidal instance request failed: {exc}") from exc
        if resp.status_code != 200:
            raise ServiceUnavailableError(f"Tidal instance returned status {resp.status_code}")
        body = resp.json()

        # v2 object: data.manifest (base64) — some instances put the manifest at
        # the top level instead, so accept both. legacy array: [{OriginalTrackUrl}].
        if isinstance(body, dict):
            data = body.get("data")
            manifest = (data.get("manifest") if isinstance(data, dict) else None) or body.get(
                "manifest"
            )
            if manifest:
                return self._from_manifest(manifest, out_path, quality, cancel, max_bytes)
            raise NotFoundOnServiceError("no manifest in Tidal v2 response")
        if isinstance(body, list):
            for item in body:
                direct = (
                    (item.get("OriginalTrackUrl") or "").strip() if isinstance(item, dict) else ""
                )
                if direct:
                    self._download(direct, out_path, cancel, max_bytes)
                    return out_path
            raise NotFoundOnServiceError("no download URL in Tidal response")
        raise NotFoundOnServiceError("unexpected Tidal response shape")

    def _lossless_guard(self, quality: str, mime_type: str) -> None:
        # Case-sensitive exact match, like the Go. Empty mime counts as lossless.
        is_requested = quality in _LOSSLESS_QUALITIES
        is_actual = ("flac" in mime_type.lower()) or mime_type == ""
        if is_requested and not is_actual:
            raise NotLosslessError(f"Tidal provided lossy format ({mime_type})")

    def _from_manifest(self, manifest_b64, out_path, quality, cancel, max_bytes) -> str:
        raw = base64.b64decode(manifest_b64)
        text = raw.decode(errors="ignore")
        if text.strip().startswith("{"):
            return self._from_bts(text, out_path, quality, cancel, max_bytes)
        return self._from_dash(text, out_path, quality, cancel, max_bytes)

    def _from_bts(self, text, out_path, quality, cancel, max_bytes) -> str:
        import json

        manifest = json.loads(text)
        urls = manifest.get("urls") or []
        if not urls:
            raise NotFoundOnServiceError("no URLs in BTS manifest")
        self._lossless_guard(quality, manifest.get("mimeType", ""))
        # Direct FLAC URL — byte-copy, no ffmpeg.
        self._download(urls[0], out_path, cancel, max_bytes)
        return out_path

    def _from_dash(self, text, out_path, quality, cancel, max_bytes) -> str:
        init_url, media_urls, mime = self._parse_dash(text)
        self._lossless_guard(quality, mime)
        if not init_url or not media_urls:
            raise NotFoundOnServiceError("no segments in Tidal DASH manifest")
        if not self._ffmpeg:
            raise ServiceUnavailableError("ffmpeg required for Tidal DASH remux")

        temp = out_path + ".m4a.tmp"
        try:
            self._download(init_url, temp, cancel, max_bytes, append=False)
            for seg in media_urls:
                if cancel():
                    raise ServiceUnavailableError("cancelled")
                self._download(seg, temp, cancel, max_bytes, append=True)
            # Remux the fragmented-MP4 FLAC stream to a native .flac with NO
            # re-encode (-c copy), honoring the never-transcode contract.
            # -map_metadata -1 -bitexact drop the MP4 ftyp/format tags (major_brand,
            # compatible_brands=mp41dash, encoder=Lavf...) that would otherwise leak
            # into the FLAC's Vorbis comments; -c copy keeps the audio bit-identical.
            proc = subprocess.run(
                [
                    self._ffmpeg,
                    "-y",
                    "-i",
                    temp,
                    "-c",
                    "copy",
                    "-map_metadata",
                    "-1",
                    "-bitexact",
                    out_path,
                ],
                capture_output=True,
                timeout=600,
            )
            if proc.returncode != 0:
                tail = proc.stderr.decode(errors="ignore")[-500:]
                raise ServiceUnavailableError(f"Tidal ffmpeg remux failed: {tail}")
        finally:
            with contextlib.suppress(OSError):
                os.remove(temp)
        return out_path

    def _parse_dash(self, text: str):
        """Return ``(init_url, [media_urls], mime)`` for the highest-bandwidth
        FLAC representation. XML first, regex fallback."""
        init_url = media_template = ""
        mime = ""
        seg_count = 0
        try:
            root = ET.fromstring(text)
            best_bw = 0
            best_tmpl = None
            best_codecs = best_mime = ""
            for aset in root.iter():
                if _local(aset.tag) != "AdaptationSet":
                    continue
                aset_mime = aset.get("mimeType", "")
                aset_codecs = aset.get("codecs", "")
                # AdaptationSet-level SegmentTemplate is the initial default,
                # used only if no Representation provides a better one (tidal.go).
                aset_tmpl = next((c for c in aset if _local(c.tag) == "SegmentTemplate"), None)
                if aset_tmpl is not None and best_tmpl is None:
                    best_tmpl = aset_tmpl
                    best_codecs = aset_codecs
                    best_mime = aset_mime
                for rep in aset:
                    if _local(rep.tag) != "Representation":
                        continue
                    # A Representation may carry its own SegmentTemplate or rely on
                    # the AdaptationSet-level one. Considering both (and capturing
                    # the Representation's codecs) lets the lossless guard see a
                    # lossy codec rather than false-passing on an empty mime — a
                    # deliberate honesty improvement over SpotiFLAC's Go, which
                    # only inspects reps that have their own template.
                    rep_tmpl = next(
                        (c for c in rep if _local(c.tag) == "SegmentTemplate"), aset_tmpl
                    )
                    if rep_tmpl is None:
                        continue
                    bw = int(rep.get("bandwidth", "0") or "0")
                    if bw > best_bw:
                        best_bw = bw
                        best_tmpl = rep_tmpl
                        best_codecs = rep.get("codecs") or aset_codecs
                        best_mime = aset_mime
            if best_tmpl is not None:
                init_url = best_tmpl.get("initialization", "")
                media_template = best_tmpl.get("media", "")
                timeline = next((c for c in best_tmpl if _local(c.tag) == "SegmentTimeline"), None)
                if timeline is not None:
                    for s in timeline:
                        if _local(s.tag) == "S":
                            seg_count += int(s.get("r", "0") or "0") + 1
                # Build the mime from codecs even when mimeType is absent, so the
                # lossless guard sees a lossy codec instead of an empty "" that
                # would false-pass (codex). Only skip when we know nothing at all.
                if best_mime or best_codecs:
                    mime = f'{best_mime}; codecs="{best_codecs}"'
        except ET.ParseError:
            pass

        if not (seg_count > 0 and init_url and media_template):
            # Regex fallback over the raw manifest.
            init_m = _INIT_RE.search(text)
            media_m = _MEDIA_RE.search(text)
            init_url = init_m.group(1) if init_m else ""
            media_template = media_m.group(1) if media_m else ""
            if not init_url:
                raise NotFoundOnServiceError("no initialization URL in Tidal manifest")
            seg_count = 0
            for tag in _S_TAG_RE.findall(text):
                rm = _R_ATTR_RE.search(tag)
                seg_count += (int(rm.group(1)) if rm else 0) + 1
            if seg_count == 0:
                raise NotFoundOnServiceError("no segments in Tidal manifest")

        init_url = init_url.replace("&amp;", "&")
        media_template = media_template.replace("&amp;", "&")
        media_urls = [media_template.replace("$Number$", str(i)) for i in range(1, seg_count + 1)]
        return init_url, media_urls, mime

    def _download(self, url, path, cancel, max_bytes, append: bool = False) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        mode = "ab" if append else "wb"
        try:
            resp = self._session.get(url, headers=default_headers(), stream=True, timeout=120)
            resp.raise_for_status()
            with open(path, mode) as fh:
                start = fh.tell() if append else 0
                written = start
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
            if not append:
                with contextlib.suppress(OSError):
                    os.remove(path)
            raise ServiceUnavailableError(f"Tidal segment download failed: {exc}") from exc
        except (NotFoundOnServiceError, ServiceUnavailableError):
            # cancel / size-cap on the direct-copy path: remove the partial .flac
            # so a stopped/rejected download never leaves a truncated file behind.
            if not append:
                with contextlib.suppress(OSError):
                    os.remove(path)
            raise
