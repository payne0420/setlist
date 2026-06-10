"""Error taxonomy for the librespot backend.

These live in their own module (no librespot import) so session/audio/search/backend
can raise and catch them without pulling in the alpha library, and so the seam in
Spotify_Downloader.py can map them to friendly messages.
"""

from __future__ import annotations


class LibrespotError(Exception):
    """Base for every librespot-backend failure."""


class LibrespotUnavailable(LibrespotError):
    """The alpha ``librespot`` package (or a transitive dep) could not be imported.

    Signals "the alpha library broke / isn't installed" — the backend degrades to
    YouTube rather than bricking Setlist.
    """


class LibrespotAuthError(LibrespotError):
    """Login failed, or no cached credentials exist and the user hasn't logged in."""


class LibrespotNotPremium(LibrespotError):
    """The authenticated account is not Premium (native 320k OGG is Premium-only)."""


class OggCaptureError(LibrespotError):
    """The native OGG stream could not be captured (no audio file, bad header, etc.)."""


class LibrespotCancelled(LibrespotError):
    """The user cancelled mid-download (cooperative cancel via the cancel callback)."""
