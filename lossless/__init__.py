"""Genuine-lossless resolver clients for the Real FLAC backend.

This subpackage ports SpotiFLAC's reverse-engineered resolvers (the Go app at
``SpotiFLAC/backend/*.go``) to Python. It pulls *real* FLAC bytes from
Qobuz / Tidal / Amazon Music HD — never a transcode of a lossy source.

Layout:
  * :mod:`lossless.constants`   — every point-in-time snapshot constant + host
    endpoint, isolated here so a server-side change is a one-file edit (goal §13).
  * :mod:`lossless.spotify_isrc`— Spotify track id → ISRC (TOTP/anon-token/spclient
    + Soundplate fallback + on-disk cache).
  * :mod:`lossless.bridge`      — Songlink bridge (ISRC → Tidal id / Amazon ASIN).
  * :mod:`lossless.qobuz`       — signed Qobuz API + the three CDN frontends.
  * :mod:`lossless.tidal`       — user-instance Tidal manifest resolver.
  * :mod:`lossless.amazon`      — Amazon proxy + CENC decrypt (ffmpeg ``-c copy``).
  * :mod:`lossless.validate`    — duration validation (no transcode).

These resolvers are unofficial third-party hosts that can rate-limit, change
shape, or vanish, and the Amazon/Tidal paths bypass service DRM. Everything is
built to fail gracefully per-service so the app falls through to the next
service and ultimately back to YouTube rather than bricking (goal §13).
"""
