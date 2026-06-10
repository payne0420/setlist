"""The Real FLAC backend: genuine lossless from Qobuz / Tidal / Amazon.

Implements the foundation's ``AudioBackend`` protocol. For each track it resolves
the Spotify ISRC, then tries each lossless service in order (Qobuz → Amazon, with
Tidal prepended when the user configured an instance); the first that yields a
**validated, genuinely-lossless** ``.flac`` wins. If every lossless service
fails, it falls through to the YouTube backend so the track still downloads —
labelled honestly as non-lossless (goal §3, §9, §13).

Honesty is enforced at the byte level: a file is only kept as ``.flac`` when its
bytes actually start with the ``fLaC`` magic. We never transcode a lossy stream
into a fake-lossless container.
"""

from __future__ import annotations

import contextlib
import os
import threading

import requests

from lossless import metadata as meta_extract
from lossless._util import normalize_tidal_api_url
from lossless.errors import LosslessError, NotFoundOnServiceError, ServiceUnavailableError
from lossless.qobuz import QobuzClient
from lossless.spotify_isrc import SpotifyIsrcResolver
from lossless.validate import is_acceptable_duration

from .youtube import YouTubeBackend

_FLAC_MAGIC = b"fLaC"


def _ffmpeg_dir() -> str | None:
    """Locate the bundled/system ffmpeg directory via the app's resolver."""
    try:
        from Spotify_Downloader import get_ffmpeg_path

        return get_ffmpeg_path()
    except Exception:
        return None


def _tool_path(name: str) -> str | None:
    """Absolute path to an ffmpeg-family tool (``ffmpeg``/``ffprobe``)."""
    import sys

    exe = f"{name}.exe" if sys.platform == "win32" else name
    d = _ffmpeg_dir()
    if d:
        candidate = os.path.join(d, exe)
        if os.path.exists(candidate):
            return candidate
    import shutil

    return shutil.which(name)


class RealFlacBackend:
    """Resolve a Spotify track to a genuine FLAC across Qobuz/Tidal/Amazon."""

    # Per-track HTTP is fine at 4 concurrent fetches (goal §1).
    max_concurrency = 4

    def __init__(self, scraper):
        self._scraper = scraper
        self._session: requests.Session = getattr(scraper, "session", None) or requests.Session()
        self._isrc = SpotifyIsrcResolver(self._session)
        self._qobuz = QobuzClient(self._session)
        self._youtube = YouTubeBackend(scraper)
        self._amazon = None  # lazily built on first Amazon attempt
        self._tidal = None
        self._bridge_cache: dict[str, dict] = {}  # ISRC -> {tidal_url, amazon_url, deezer}
        # Provider-native metadata captured during fetch(), keyed by Spotify
        # track id: {"source": "qobuz"|"tidal"|"amazon"|"youtube", "meta": {...}}.
        # The download seam reads it via provider_metadata_for() to tag the FLAC
        # from the SAME release the bytes came from. Guarded for the 4-worker pool.
        self._provider_meta: dict[str, dict] = {}
        self._provider_meta_lock = threading.Lock()

        # Lossless config threaded through MusicScraper (goal §11). Safe defaults
        # so the backend works even if the scraper predates the config wiring.
        self._quality = str(getattr(scraper, "lossless_quality", "27") or "27")
        self._tidal_api_url = self._normalize_tidal_url(getattr(scraper, "tidal_api_url", "") or "")
        self._service_order_cfg = str(
            getattr(scraper, "lossless_service_order", "qobuz,amazon") or "qobuz,amazon"
        )
        # When every lossless service fails: fall back to YouTube (a non-lossless
        # file) if True, else fail the track so only genuine lossless ever lands.
        self._youtube_fallback = bool(getattr(scraper, "lossless_youtube_fallback", True))

    # -- helpers ------------------------------------------------------------ #
    @staticmethod
    def _normalize_tidal_url(value: str) -> str:
        return normalize_tidal_api_url(value)

    def _service_order(self, extended: bool = False) -> list[str]:
        """Mirror SpotiFLAC config.go auto-order: qobuz,amazon by default; Tidal
        prepended only when a custom instance is configured.

        In **extended mode**, Qobuz is forced to the front: it is the only service
        with a catalog search, so it is the only one that can reach the extended
        cut. Tidal/Amazon resolve the exact recording by ISRC (the radio edit), so
        letting them win first would defeat extended mode entirely.
        """
        order = [s.strip().lower() for s in self._service_order_cfg.split(",") if s.strip()]
        order = [s for s in order if s in ("qobuz", "amazon", "tidal")]
        seen: set[str] = set()
        deduped = [s for s in order if not (s in seen or seen.add(s))]
        if len(deduped) < 2:
            deduped = ["qobuz", "amazon"]
        if self._tidal_api_url and "tidal" not in deduped:
            deduped = ["tidal", *deduped]
        elif not self._tidal_api_url:
            deduped = [s for s in deduped if s != "tidal"]
        if extended and "qobuz" in deduped:
            deduped = ["qobuz", *[s for s in deduped if s != "qobuz"]]
        return deduped

    def _expected_seconds(self, track) -> int:
        return int(track.duration_ms / 1000) if getattr(track, "duration_ms", None) else 0

    def _flac_path_for(self, destination: str) -> str:
        return os.path.splitext(destination)[0] + ".flac"

    # -- provider metadata store (read by the download seam) ---------------- #
    def provider_metadata_for(self, track_id) -> dict | None:
        """The provider-native record for *track_id*, or ``None``.

        Returns ``{"source": <service>, "meta": <tag dict>}`` recorded by the
        winning service during :meth:`fetch`. ``source == "youtube"`` marks a
        degraded (non-lossless) fallback so the seam can treat metadata as such."""
        if not track_id:
            return None
        with self._provider_meta_lock:
            rec = self._provider_meta.get(str(track_id))
            # Copy the nested meta too so a caller can't mutate the stored dict.
            return {"source": rec["source"], "meta": dict(rec.get("meta") or {})} if rec else None

    def _record_provider_meta(self, track_id, source: str, meta: dict | None) -> None:
        """Record the winning service's native metadata for *track_id* (thread-safe)."""
        if not track_id:
            return
        with self._provider_meta_lock:
            self._provider_meta[str(track_id)] = {"source": source, "meta": meta or {}}

    # -- fetch -------------------------------------------------------------- #
    def fetch(self, *, track, destination, extended, audio_format, audio_quality, cancel):
        expected_s = self._expected_seconds(track)

        # Resolve ISRC once (cached on the track + on disk). Best-effort: a missing
        # ISRC only blocks the by-ISRC path; extended catalog search still works.
        isrc = getattr(track, "isrc", None)
        if not isrc and not cancel():
            isrc = self._isrc.resolve(track.id)
            with contextlib.suppress(Exception):
                track.isrc = isrc

        dispatch = {"qobuz": self._try_qobuz, "amazon": self._try_amazon, "tidal": self._try_tidal}
        for service in self._service_order(extended):
            if cancel():
                break
            try:
                result = dispatch[service](
                    track=track,
                    isrc=isrc,
                    destination=destination,
                    extended=extended,
                    expected_s=expected_s,
                    cancel=cancel,
                )
                if result:
                    return result
            except LosslessError:
                continue  # this service can't serve it -> try the next
            except Exception as exc:  # defensive: a resolver host can break in novel ways
                print(f"[*] lossless service '{service}' failed: {exc}")
                continue

        # Stop was pressed mid-resolution: don't kick off a YouTube download the
        # user no longer wants (the seam treats this as a failed/cancelled track).
        if cancel():
            raise LosslessError("download cancelled")

        # Every lossless service failed. With the fallback toggle off, fail the
        # track instead of fetching a lossy YouTube copy (pure-lossless mode).
        if not self._youtube_fallback:
            raise NotFoundOnServiceError("no lossless source found (YouTube fallback off)")

        # Otherwise fall back to YouTube so the track still downloads (non-lossless).
        # Mark the source degraded so the seam knows album/track-number metadata
        # is unavailable from a provider (it still enriches from Spotify spclient).
        self._record_provider_meta(track.id, "youtube", None)
        with contextlib.suppress(Exception):
            self._scraper.error_signal.emit(
                "Lossless source unavailable — fell back to YouTube (not lossless)"
            )
        return self._youtube.fetch(
            track=track,
            destination=destination,
            extended=extended,
            audio_format=audio_format,
            audio_quality=audio_quality,
            cancel=cancel,
        )

    # -- Qobuz -------------------------------------------------------------- #
    def _try_qobuz(self, *, track, isrc, destination, extended, expected_s, cancel):
        used_extended = False
        qobuz_track = None  # the full Qobuz dict of the winning track, for tagging
        if extended:
            qobuz_track = self._qobuz_extended_match(track, expected_s)
            used_extended = qobuz_track is not None
        if qobuz_track is None:
            qobuz_track = self._qobuz_normal_match(track, isrc)
        track_id = int(qobuz_track["id"]) if qobuz_track and qobuz_track.get("id") else None
        if track_id is None:
            raise NotFoundOnServiceError("no Qobuz match")

        url = self._qobuz.get_download_url(track_id, self._quality)
        flac_path = self._flac_path_for(destination)
        self._download(url, flac_path, cancel)
        # Bypass the duration guard only when an extended cut actually won — a
        # fall-back to the normal recording (used_extended False) is radio-edit
        # length and SHOULD be duration-validated (codex: wrong-recording escape).
        self._enforce_lossless_and_duration(flac_path, expected_s, used_extended)
        # Capture provider-native metadata from the winning Qobuz dict (both the
        # normal/ISRC path and the extended path now carry the full item).
        self._record_provider_meta(
            track.id, "qobuz", meta_extract.extract_qobuz_metadata(qobuz_track)
        )
        return flac_path, "flac", used_extended

    def _qobuz_normal_match(self, track, isrc) -> dict | None:
        """The best-scoring Qobuz track dict for *track* (or ``None``)."""
        try:
            if isrc:
                best = self._qobuz.search_by_isrc(
                    isrc, track.title, track.artists, track.album or ""
                )
            else:
                best = self._qobuz.search_by_isrc("", track.title, track.artists, track.album or "")
        except NotFoundOnServiceError:
            return None
        return best if best and best.get("id") else None

    def _qobuz_extended_match(self, track, expected_s) -> dict | None:
        """Extended mode: SEARCH the catalog (ISRC would return the radio edit).

        Build the query with the shared selector's radio-edit strip + the bare
        word "extended" (project memory: extra tokens bury the real Extended
        Version), map results to candidates, and let track_selectors choose.
        Returns the FULL Qobuz dict of the chosen extended cut (so the seam can
        tag from the same release), or ``None``.
        """
        import track_selectors

        title = track_selectors.strip_radio_edit(track.title)
        query = f"{title} {track.artists} extended".strip()
        try:
            items = self._qobuz.search(query, limit=10)
        except LosslessError:
            return None
        by_id: dict[int, dict] = {}
        cands = []
        for it in items:
            tid = it.get("id")
            if not tid:
                continue
            by_id[int(tid)] = it
            # Fold the Qobuz `version` field into the title so the keyword filter
            # ("extended"/"club mix") can see "(Extended Mix)" etc.
            full_title = f"{it.get('title') or ''} {it.get('version') or ''}".strip()
            cands.append({"id": int(tid), "title": full_title, "duration_s": it.get("duration")})
        max_dur = getattr(self._scraper, "max_track_duration_s", 1200)
        chosen = track_selectors.select_extended(
            cands,
            expected_s or None,
            max_dur,
            source_title=track.title,
            strict_title=getattr(self._scraper, "extended_strict_match", True),
        )
        return by_id.get(int(chosen)) if chosen else None

    # -- Amazon / Tidal (implemented in task-5 wiring) ---------------------- #
    def _try_amazon(self, *, track, isrc, destination, extended, expected_s, cancel):
        if self._amazon is None:
            from lossless.amazon import AmazonClient

            self._amazon = AmazonClient(
                self._session, ffmpeg=_tool_path("ffmpeg"), ffprobe=_tool_path("ffprobe")
            )
        return self._amazon_fetch(track, isrc, destination, expected_s, extended, cancel)

    def _try_tidal(self, *, track, isrc, destination, extended, expected_s, cancel):
        if not self._tidal_api_url:
            raise NotFoundOnServiceError("no Tidal instance configured")
        if self._tidal is None:
            from lossless.tidal import TidalClient

            self._tidal = TidalClient(
                self._session, self._tidal_api_url, ffmpeg=_tool_path("ffmpeg")
            )
        return self._tidal_fetch(track, isrc, destination, expected_s, extended, cancel)

    def _bridge_links(self, isrc: str | None) -> dict:
        """ISRC → ``{tidal_url, amazon_url}`` via Songlink, memoized per ISRC."""
        if not isrc:
            raise NotFoundOnServiceError("no ISRC for the Songlink bridge")
        if isrc not in self._bridge_cache:
            from lossless.bridge import resolve_platform_links

            try:
                self._bridge_cache[isrc] = resolve_platform_links(isrc, self._session)
            except LosslessError:
                raise
            except Exception as exc:
                raise ServiceUnavailableError(f"Songlink bridge failed: {exc}") from exc
        return self._bridge_cache[isrc]

    def _amazon_fetch(self, track, isrc, destination, expected_s, extended, cancel):
        # Amazon resolves the exact recording by ISRC; it has no catalog search,
        # so extended mode falls back to the original cut (used_extended=False).
        amazon_url = self._bridge_links(isrc).get("amazon_url")
        if not amazon_url:
            raise NotFoundOnServiceError("no Amazon match")
        flac_path = self._flac_path_for(destination)
        self._amazon.fetch_flac(
            amazon_url, flac_path, cancel, getattr(self._scraper, "max_track_bytes", 0)
        )
        # Amazon always returns the exact ISRC recording (radio-edit length), so
        # the duration guard always applies — never bypass it here.
        self._enforce_lossless_and_duration(flac_path, expected_s, extended=False)
        # Tag from the same Deezer JSON the bridge already fetched (no new request).
        with contextlib.suppress(Exception):
            deezer = self._bridge_links(isrc).get("deezer")
            self._record_provider_meta(
                track.id, "amazon", meta_extract.extract_deezer_metadata(deezer)
            )
        return flac_path, "flac", False

    def _tidal_fetch(self, track, isrc, destination, expected_s, extended, cancel):
        from lossless.tidal import map_quality

        # Resolve the Tidal id via the instance's OWN search first (ISRC, then
        # title+artist). This is authoritative and not subject to the public
        # Songlink bridge's rate limits / missing mappings — the bridge 429s
        # under playlist concurrency and silently drops most tracks. Songlink is
        # kept only as a last-resort fallback so behavior never regresses.
        track_id = self._tidal.find_track_id(isrc=isrc, title=track.title, artists=track.artists)
        if not track_id:
            from lossless.bridge import extract_tidal_track_id

            tidal_url = ""
            with contextlib.suppress(Exception):
                tidal_url = self._bridge_links(isrc).get("tidal_url")
            track_id = extract_tidal_track_id(tidal_url) if tidal_url else None
        if not track_id:
            raise NotFoundOnServiceError("no Tidal match")
        flac_path = self._flac_path_for(destination)
        self._tidal.fetch_flac(
            track_id,
            flac_path,
            map_quality(self._quality),
            cancel,
            getattr(self._scraper, "max_track_bytes", 0),
        )
        # Tidal also resolves the exact ISRC recording -> always duration-validate.
        self._enforce_lossless_and_duration(flac_path, expected_s, extended=False)
        # Capture provider-native metadata from the instance's /info/ endpoint.
        with contextlib.suppress(Exception):
            info = self._tidal.fetch_track_info(track_id)
            if info:
                self._record_provider_meta(
                    track.id, "tidal", meta_extract.extract_tidal_metadata(info)
                )
        return flac_path, "flac", False

    # -- download + validation ---------------------------------------------- #
    def _download(self, url: str, path: str, cancel) -> None:
        """Raw byte-copy the resolved CDN URL to *path* (no decode/re-encode).

        Polls *cancel* during the copy so Stop stays responsive (≤3s), and
        enforces the optional ``max_track_bytes`` cap (a too-big file is the
        wrong-version signal)."""
        max_bytes = getattr(self._scraper, "max_track_bytes", 0)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            resp = self._session.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0) or 0)
            written = 0
            with open(path, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if cancel():
                        raise LosslessError("cancelled")
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise NotFoundOnServiceError("track exceeds the size limit")
                    if total:
                        with contextlib.suppress(Exception):
                            self._scraper.dlprogress_signal.emit(
                                min(int(written / total * 100), 100)
                            )
        except (requests.RequestException, OSError) as exc:
            self._cleanup(path)
            raise NotFoundOnServiceError(f"download failed: {exc}") from exc
        except LosslessError:
            self._cleanup(path)
            raise

    def _enforce_lossless_and_duration(self, path: str, expected_s: int, extended: bool) -> None:
        """Reject the file (and delete it) unless it is genuinely lossless FLAC and
        passes the duration guard (the guard is bypassed in extended mode)."""
        try:
            with open(path, "rb") as fh:
                magic = fh.read(4)
        except OSError as exc:
            raise NotFoundOnServiceError(f"missing download: {exc}") from exc
        if magic != _FLAC_MAGIC:
            self._cleanup(path)
            raise NotFoundOnServiceError("resolved stream was not genuine FLAC")
        ok, reason = is_acceptable_duration(
            path, expected_s, extended=extended, ffprobe_path=_tool_path("ffprobe")
        )
        if not ok:
            self._cleanup(path)
            raise NotFoundOnServiceError(reason or "duration validation rejected the file")

    @staticmethod
    def _cleanup(path: str) -> None:
        with contextlib.suppress(OSError):
            os.remove(path)
