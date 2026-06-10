"""The opt-in librespot backend: Spotify's native ~320k OGG/Vorbis stream.

Authenticated with the user's own Premium account via `librespot-python`, this
backend captures the decrypted-but-still-Vorbis-encoded OGG bytes and writes them
verbatim (no decode, no re-encode) — Spotify's exact stream rather than a YouTube
match. It is strictly opt-in, off by default, and Premium-only; see the goal spec
and DESIGN.md in this directory.

Nothing heavy is imported at module scope: the alpha `librespot` package is loaded
lazily (only when a track is actually fetched) behind the ``_librespot`` adapter,
so importing this package — e.g. from ``make_backend`` — never fails just because
the alpha library is missing.
"""

from __future__ import annotations
