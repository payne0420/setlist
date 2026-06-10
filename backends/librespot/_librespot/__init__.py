"""Thin adapter over the alpha ``librespot`` package (kokarare1212/librespot-python).

Every direct import of the upstream alpha library is contained HERE so a breaking
upstream change touches exactly one file. Pinned commit:
``18104622b3be02062f1f8abe8dafc396413e9784``.

All upstream imports are lazy (inside functions): importing this module is cheap and
never raises, so callers can probe :func:`is_available` and degrade gracefully. The
helpers below were written against the pinned commit's actual source, which differs
from the goal's illustrative snippets in two load-bearing ways:

* OAuth is fully self-contained. ``Session.Builder.oauth(url_callback)`` uses the
  built-in keymaster client_id + a redirect on ``127.0.0.1:5588`` and runs its OWN
  blocking callback HTTP server; we only supply a callback that opens the browser.
  It blocks until the browser login completes, so callers must run it off the UI
  thread. On success the Session itself writes ``stored_credentials_file``.
* The 167-byte (``0xA7``) Spotify header is skipped INSIDE ``content_feeder().load``
  (``CdnFeedHelper.load_track`` does ``input_stream.skip(0xA7)``), and
  ``Streamer.stream()`` returns that same already-advanced stream — so the bytes
  read from the :func:`load_loaded_stream` result already begin at the first ``OggS`` page.
"""

from __future__ import annotations

import os
from typing import Callable

# Cached import probe: None = not yet checked, True/False = result.
_AVAILABLE: bool | None = None
_IMPORT_ERROR: BaseException | None = None


def is_available() -> bool:
    """True iff the alpha ``librespot`` package and its deps import cleanly.

    Result is cached. On failure the captured exception is available via
    :func:`import_error` so callers can surface a specific reason.
    """
    global _AVAILABLE, _IMPORT_ERROR
    if _AVAILABLE is None:
        try:
            import librespot.audio.decoders  # noqa: F401
            import librespot.core  # noqa: F401
            import librespot.metadata  # noqa: F401

            _AVAILABLE = True
        except BaseException as exc:  # noqa: BLE001 - any import-time failure disables it
            _AVAILABLE = False
            _IMPORT_ERROR = exc
    return _AVAILABLE


def import_error() -> BaseException | None:
    """The exception that made :func:`is_available` return False (or None)."""
    return _IMPORT_ERROR


def _configuration(credentials_path: str):
    """Build a Session.Configuration that persists credentials to *credentials_path*.

    The chunk cache is a no-op stub upstream, so only credential storage matters; we
    point it at the caller's path (default would be ``cwd/credentials.json``).
    """
    from librespot.core import Session

    return (
        Session.Configuration.Builder()
        .set_store_credentials(True)
        .set_stored_credential_file(credentials_path)
        .build()
    )


def login_oauth(
    credentials_path: str,
    on_auth_url: Callable[[str], None],
    *,
    device_name: str = "Setlist",
    success_page: str | None = None,
):
    """Interactive OAuth login (BLOCKING — run off the UI thread).

    ``on_auth_url(url)`` is invoked with the Spotify authorize URL so the caller can
    open a browser; the upstream lib then waits on its own loopback server for the
    redirect. If a credentials file already exists, upstream ``oauth`` short-circuits
    to ``stored_file`` and no server/browser is needed. Returns a connected Session.
    """
    from librespot.core import Session

    conf = _configuration(credentials_path)
    builder = Session.Builder(conf).set_device_name(device_name)
    return builder.oauth(on_auth_url, success_page).create()


def login_stored(credentials_path: str, *, device_name: str = "Setlist"):
    """Resume a session from a cached ``credentials.json`` (no browser). Returns a Session."""
    from librespot.core import Session

    conf = _configuration(credentials_path)
    builder = Session.Builder(conf).set_device_name(device_name)
    return builder.stored_file(credentials_path).create()


def has_stored_credentials(credentials_path: str) -> bool:
    """True iff a cached credentials file exists at *credentials_path*."""
    return bool(credentials_path) and os.path.isfile(credentials_path)


def product_type(session) -> str | None:
    """Account product type: ``"premium"`` | ``"free"`` | None (from user attributes)."""
    return session.get_user_attribute("type")


def account_username(session) -> str | None:
    """Canonical username of the authenticated account, best-effort."""
    try:
        return session.username()
    except Exception:  # noqa: BLE001 - purely informational
        return None


def web_bearer_token(session, scope: str = "user-read-email") -> str:
    """A Spotify Web-API bearer token for *scope* (used by the extended search)."""
    return session.tokens().get(scope)


def vorbis_quality(name: str, *, strict: bool = True):
    """An AudioQualityPicker selecting an OGG/Vorbis file for AudioQuality *name*
    ("VERY_HIGH" -> ~320k, "HIGH" -> ~160k, ...).

    Default ``strict=True`` returns a picker that matches ONLY the requested quality
    tier (no silent downgrade), so the caller's VERY_HIGH->HIGH ladder is real and the
    obtained bitrate is known. Upstream's ``VorbisOnlyAudioQuality`` (``strict=False``)
    instead falls back to any available Vorbis file, which can quietly yield 160/96k
    while we promise 320k.
    """
    from librespot.audio import SuperAudioFormat
    from librespot.audio.decoders import AudioQuality, VorbisOnlyAudioQuality

    preferred = AudioQuality[name]
    if not strict:
        return VorbisOnlyAudioQuality(preferred)

    from librespot.structure import AudioQualityPicker

    class _StrictVorbisQuality(AudioQualityPicker):
        def get_file(self, files):
            for f in preferred.get_matches(files):
                if (
                    f.HasField("format")
                    and SuperAudioFormat.get(f.format) == SuperAudioFormat.VORBIS
                ):
                    return f
            return None  # strict: no any-Vorbis fallback -> caller drops to next tier

    return _StrictVorbisQuality()


def track_id_from_base62(base62: str):
    """A ``TrackId`` from a base62 Spotify track id."""
    from librespot.metadata import TrackId

    return TrackId.from_base62(base62)


def load_loaded_stream(session, track_id, quality):
    """``content_feeder().load(...)`` -> LoadedStream (header already skipped to OggS).

    ``preload=False``, ``halt_listener=None`` per the pinned API.
    """
    return session.content_feeder().load(track_id, quality, False, None)


def track_metadata(session, base62: str):
    """Fetch a track's ``Metadata.Track`` protobuf WITHOUT streaming any audio.

    ``session.api().get_metadata_4_track(...)`` is a metadata-only Mercury
    (extended-metadata) request — no audio key, no CDN stream, no Premium gate. It
    returns the SAME ``Metadata.Track`` proto carried by a ``LoadedStream`` (album,
    artists, track/disc number, cover ids), and it goes over the authenticated
    session, so it is NOT subject to the anonymous Spotify Web-API rate limits. This
    is how the YouTube download path obtains the rich metadata it can't get from the
    no-auth embed (which has no album/track number).
    """
    from librespot.metadata import TrackId

    return session.api().get_metadata_4_track(TrackId.from_base62(base62))
