"""Exception hierarchy for the lossless backend.

These map to friendly UI messages in ``MusicScraper._get_user_friendly_error``
(goal §2) and, more importantly, let the per-service orchestrator in
``RealFlacBackend`` distinguish "this track isn't on this service" (try the next
service) from "this resolver host is down" (also try the next, but a different
message) — both of which ultimately fall through to YouTube (goal §9, §13).
"""

from __future__ import annotations


class LosslessError(RuntimeError):
    """Base class for every lossless-resolver failure."""


class IsrcLookupError(LosslessError):
    """Could not determine the track's ISRC (the bridge key for every service)."""


class NotFoundOnServiceError(LosslessError):
    """The track has no match on this service (try the next service)."""


class ServiceUnavailableError(LosslessError):
    """A resolver host errored / timed out / changed shape (try the next)."""


class RegionLockedError(NotFoundOnServiceError):
    """The track exists but is not streamable in the resolved region."""


class NotLosslessError(LosslessError):
    """The service could only offer a lossy stream — never transcode to fake
    FLAC; fall through to the next service (goal §0, §7b)."""
