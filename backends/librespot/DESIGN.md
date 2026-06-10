# Librespot 320k OGG backend — design (verified against pinned commit)

Pinned: `kokarare1212/librespot-python@18104622b3be02062f1f8abe8dafc396413e9784`.
All claims below were read from that commit's source, not the goal's illustrative snippets.

## Verified library facts (these override the goal's example code where they differ)

1. **OAuth is fully self-contained.** `Session.Builder.oauth(url_callback, success_page=None)`
   hardcodes `MercuryRequests.keymaster_client_id` + redirect `http://127.0.0.1:5588/login`,
   spins up its OWN blocking `http.server` on 5588, and calls `url_callback(auth_url)` so the
   caller can open a browser. It is BLOCKING (`run_callback_server()` loops until the code
   arrives). => We do NOT run our own loopback server and we do NOT use the Rust ref's
   client_id/port 8898. We pass a callback that does `QDesktopServices.openUrl`. The whole
   `...oauth(cb).create()` call runs on a background QThread (it blocks).
2. **`oauth()` auto-reuses a cached file**: if `conf.stored_credentials_file` exists it calls
   `stored_file(None)` first. On a successful connect the Session itself writes
   `stored_credentials_file` — the **reusable AP auth credentials** (`reusable_auth_credentials`),
   NOT a PKCE token and never the password (still sensitive, hence chmod 0600). => point that path
   at our config dir and login persists across launches with no re-login.
3. **Header skip is internal.** `content_feeder().load(TrackId, VorbisOnlyAudioQuality(VERY_HIGH),
   False, None)` -> `load_track` -> `load_stream` -> `CdnFeedHelper.load_track`, which does
   `input_stream.skip(0xA7)` (167 bytes) and returns a `LoadedStream`. `LoadedStream.input_stream`
   is the `Streamer`; `Streamer.stream()` returns the SAME `InternalStream` instance already
   advanced to byte 167 = the first real `OggS` page. So the bytes we read already begin at `OggS`.
   `.input_stream.size` is the FULL file size incl. the 167-byte header, so we read `size-167`
   bytes and must terminate on empty read, not on `written < size`.
4. `session.get_user_attribute("type")` -> "premium" | "free" (premium gate).
5. `session.tokens().get("user-read-email")` -> Web-API bearer (Login5). Used by search.py.
6. `TrackId.from_uri("spotify:track:<base62>")` / `TrackId.from_base62(<base62>)` both exist.
7. Deps (from requirements.txt): defusedxml, protobuf==3.20.1, pycryptodomex (`Cryptodome`),
   pyogg, requests, websocket-client, **zeroconf**. pyogg + zeroconf are NOT imported on our
   capture path (pyogg = decode-only; zeroconf = discovery), so they're auto-installed by pip but
   need not be PyInstaller hiddenimports. (Verified: the whole adapter + a real OGG round-trip work
   under Python 3.13 with protobuf 3.20.1, despite the old pin.)

## Module layout (all under backends/librespot/)

- `_librespot/__init__.py` — the ONLY place that imports the alpha `librespot` package. Lazy:
  `load()` imports on first use; `is_available()` returns False (caching the ImportError) if the
  alpha lib/its deps are missing. Exposes thin helpers: `build_session(creds_path, on_auth_url)`,
  `track_id(base62)`, `vorbis_quality(name)`, `load_stream(session, tid, quality)`,
  `product_type(session)`, `web_token(session)`. This contains an upstream break to ONE file.
- `session.py` — `LibrespotSession`: resolve creds path (QStandardPaths AppConfigLocation,
  chmod 0600), OAuth-or-stored login (delegates to adapter), `is_premium()`, `close()`.
- `audio.py` — `capture_ogg(stream, dest_tmp, cancel, *, chunk_size=64*1024)`: pure byte pump,
  defensive OggS-trim, returns bytes written. `fetch_track_ogg(session, base62, *, dest, cancel,
  on_status)`: load at VERY_HIGH, fall back to HIGH, audio-key retry w/ exponential backoff;
  writes to `dest.tmp` then atomic rename to `dest`. NEVER re-encodes (OGG-only).
- `search.py` — `find_extended_id(session, *, title, artists, expected_s, max_track_duration_s)`
  and `find_normal_id(...)`: Web-API search, build `{"id","title","duration_s"}` candidates,
  require primary-artist match (reject covers), reject sped-up/remix unless the title itself has
  it, then defer to `track_selectors.select_extended/select_normal`. Returns a base62 id or None.
- `backend.py` — `LibrespotBackend(scraper)`: `max_concurrency = 1`. `fetch(...)` orchestrates:
  consent/premium already gated in UI; resolve the id (extended search or the pasted id),
  capture OGG, return `(final_path, "ogg", used_extended)`. Inter-track jitter sleep + cancel.

## fetch() control flow (backend.py)

```
dest_ogg = splitext(destination)[0] + ".ogg"          # destination arrives as ...mp3 from the seam
if exists(dest_ogg): return (dest_ogg, "ogg", extended)
session = scraper-cached LibrespotSession (built once, reused; serialized by max_concurrency=1)
if not session.is_premium(): raise LibrespotError("Spotify Premium required for 320k OGG")
base62 = track.id
used_extended = False
if extended:
    cand = search.find_extended_id(session, title=strip_radio_edit(track.title), artists=track.artists,
                                   expected_s=track.duration_ms/1000, max_track_duration_s=scraper.max_track_duration_s)
    if cand: base62, used_extended = cand, True      # else fall through to original id (normal)
audio.fetch_track_ogg(session, base62, dest=dest_ogg, cancel=cancel, on_status=scraper.error_signal.emit)
return (dest_ogg, "ogg", used_extended)
```

`used_extended` drives `_resolve_extended_output` / `_meta_title` exactly like YouTube.

## Header-skip strategy (audio.capture_ogg)

The library already positions at `OggS`. We still trim defensively so a future upstream change
can't corrupt output: read the first chunk; if it does NOT start with `OggS`, find the first
`OggS` and drop everything before it; otherwise write as-is. In the pinned commit byte0 == `OggS`
so the scan is a no-op and never reaches into audio data. Loop terminates on empty read; poll
`cancel()` each iteration; assert `>=1` byte and a leading `OggS` after trim.

## Error taxonomy + graceful degradation

- `LibrespotUnavailable` (adapter import failed) — raised by session build and re-raised by
  backend.fetch as a classified "advance" error: the user's `fallback_order` chain
  (`backends/chain.py`) decides whether the track diverts to another source, so the alpha lib
  breaking never bricks Setlist (YouTube stays default source).
- `LibrespotAuthError` (not logged in) — always aborts the track, even with a fallback chain
  configured (a silent source swap would be misleading); friendly per-track error, UI also gates.
- `LibrespotNotPremium` (positively-known free account) — classified "advance" error for the chain.
- Transient `Failed fetching audio key` / load errors — retry 3x exponential backoff (10s base,
  30s cap, mirrors Rust ref), then a friendly error via `_get_user_friendly_error`.
- VERY_HIGH vorbis unavailable for a track -> retry HIGH (still native .ogg). If both unavailable
  -> `OggCaptureError`, another "advance" error for the chain (goal §10.5's graceful degradation,
  now user-ordered instead of hardwired to YouTube).
- `NoExtendedCutError` — extended-mix mode only: the extended search succeeded but found no
  extended cut AND a fallback step exists (`has_fallback=True`); the chain advances with
  `extended=True` so the next source runs its own extended search. With no fallback configured
  the backend streams the original Spotify track natively instead (file left unmarked).

## Threading / cancel

`max_concurrency = 1` (foundation clamps the worker pool). The session is created once and reused
across tracks in a run (a single account can't safely run parallel native streams). Small jitter
sleep between tracks (cancel-aware). The read loop polls `cancel` so Stop is responsive (<=3s).

## Metadata + formats (Spotify_Downloader.py)

- `SUPPORTED_FORMATS["ogg"] = {"ext":"ogg","lossy":True}` — but the librespot OGG is the FINAL
  file; it never goes through the yt-dlp transcode/quality path (that path is YouTube-only).
- `_write_metadata_ogg` via `mutagen.oggvorbis.OggVorbis` + `METADATA_BLOCK_PICTURE`
  (base64 FLAC Picture). Register `".ogg"` in `_METADATA_WRITERS`. Reuses the existing tags dict.

## Filename-honesty fix (small, in-scope)

`_resolve_extended_output` builds the unmarked name via `_format_track_filename`, which hardcodes
`.mp3`. For a real `.ogg` (or `.flac`) that rename would mislabel the container. Fix: preserve the
real extension of `final_path` when building the unmarked path. No-op for the YouTube+mp3 case
(keeps existing tests green); corrects ogg + the latent flac case.

## Config / Settings / packaging

- Append `("Spotify (librespot 320k OGG)","librespot")` to the source dropdown + add "librespot"
  to `KNOWN_DOWNLOAD_SOURCES`. New config keys `spotify_credentials_path`, `librespot_consented`.
- "SPOTIFY ACCOUNT" settings card: Log in/out (runs OAuth on a QThread), shows account + premium,
  one-time consent dialog persisting `librespot_consented`, disclaimer. Greyed unless source==librespot.
- Thread new keys _start_scraper -> ScraperThread -> MusicScraper -> make_backend.
- req.txt + Setlist.spec hiddenimports: librespot (pinned) + Cryptodome, google.protobuf,
  websocket, defusedxml. ffmpeg NOT required for OGG path.
```
