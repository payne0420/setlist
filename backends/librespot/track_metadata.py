"""Extract clean track metadata from a librespot ``LoadedStream`` protobuf.

The download seam builds ``song_meta`` from the spotifydown EMBED metadata
(``TrackInfo``), which has **no album** for any source and joins artist names with
a non-breaking space (``\\xa0``). The librespot backend uniquely holds the REAL
Spotify metadata in ``LoadedStream.track`` — a ``Metadata.Track`` protobuf fetched
for free during capture (no extra Web-API call, so it is NOT subject to the
``/v1/me`` rate limits that motivated reading librespot's own session attributes
first; see :mod:`backends.librespot.session`). This module turns that protobuf into
the small dict the tag writer consumes.

Kept deliberately dependency-light: it only does ``getattr`` on the protobuf, so it
works against either the real ``Metadata.Track`` or a plain stand-in in tests, and a
malformed/partial protobuf degrades to whatever fields ARE present rather than
raising (a metadata hiccup must never fail an otherwise-successful download).
"""

from __future__ import annotations

# Spotify CDN base for album art addressed by hex file_id (image bytes are fetched
# later by the tag-writing thread's _fetch_cover_bytes).
SCDN_IMAGE_BASE = "https://i.scdn.co/image/"

# Metadata.Image.Size enum ints: DEFAULT=0, SMALL=1, LARGE=2, XLARGE=3. Pixel
# ordering (smallest -> largest) is SMALL < DEFAULT < LARGE < XLARGE, so when the
# width/height fields are unset (commonly 0 in metadata responses) we rank by this.
_SIZE_RANK = {1: 0, 0: 1, 2: 2, 3: 3}


def extract_track_metadata(loaded) -> dict | None:
    """Return a clean tag dict from a librespot ``LoadedStream`` (or ``None``).

    Keys mirror the seam's ``song_meta`` (``title``, ``artists``, ``album``,
    ``releaseDate``, ``trackNumber``, ``discNumber``, ``cover``); only fields the
    protobuf actually carries are included, so the caller can merge selectively.
    Returns ``None`` when there is no usable track (e.g. an episode/podcast load).
    """
    return extract_from_proto(getattr(loaded, "track", None))


def extract_from_proto(track) -> dict | None:
    """Like :func:`extract_track_metadata` but takes the ``Metadata.Track`` proto
    directly — used by the metadata-only Mercury fetch on the YouTube path, which
    returns a bare ``Metadata.Track`` (no enclosing ``LoadedStream``)."""
    if track is None:
        return None

    meta: dict = {}

    title = _clean(getattr(track, "name", ""))
    if title:
        meta["title"] = title

    artists = _join_artists(getattr(track, "artist", ()) or ())
    if artists:
        meta["artists"] = artists

    number = _as_int(getattr(track, "number", 0))
    if number > 0:
        meta["trackNumber"] = number

    disc = _as_int(getattr(track, "disc_number", 0))
    if disc > 0:
        meta["discNumber"] = disc

    duration = _as_int(getattr(track, "duration", 0))
    if duration > 0:
        meta["durationMs"] = duration

    album = getattr(track, "album", None)
    if album is not None:
        album_name = _clean(getattr(album, "name", ""))
        if album_name:
            meta["album"] = album_name
        date = _format_date(getattr(album, "date", None))
        if date:
            meta["releaseDate"] = date
        cover = _cover_url(album)
        if cover:
            meta["cover"] = cover

    return meta or None


def _clean(value) -> str:
    """Trim and replace the non-breaking space the spotifydown embed leaks in."""
    if not value:
        return ""
    return str(value).replace("\xa0", " ").strip()


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _join_artists(artist_list) -> str:
    """Join protobuf artist names with ", " (clean, no nbsp)."""
    names = []
    for artist in artist_list:
        name = _clean(getattr(artist, "name", ""))
        if name:
            names.append(name)
    return ", ".join(names)


def _format_date(date) -> str:
    """Format a ``Metadata.Date`` (year/month/day) as ``YYYY[-MM[-DD]]`` (or "")."""
    if date is None:
        return ""
    year = _as_int(getattr(date, "year", 0))
    if year <= 0:
        return ""
    month = _as_int(getattr(date, "month", 0))
    day = _as_int(getattr(date, "day", 0))
    if month > 0 and day > 0:
        return f"{year:04d}-{month:02d}-{day:02d}"
    if month > 0:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}"


def _cover_url(album) -> str:
    """Highest-resolution album-cover URL from the protobuf (or "").

    Prefers ``album.cover_group.image`` (the modern field), falling back to the
    legacy repeated ``album.cover``. Picks the largest image by known pixel area,
    then by the size-enum rank when dimensions are unset.
    """
    cover_group = getattr(album, "cover_group", None)
    images = list(getattr(cover_group, "image", ()) or ()) if cover_group is not None else []
    if not images:
        images = list(getattr(album, "cover", ()) or ())

    best = None
    best_key = None
    for img in images:
        if not (getattr(img, "file_id", b"") or b""):
            continue
        key = _image_sort_key(img)
        if best is None or key > best_key:
            best, best_key = img, key
    if best is None:
        return ""

    file_id = best.file_id
    hex_id = file_id.hex() if isinstance(file_id, (bytes, bytearray)) else str(file_id)
    return f"{SCDN_IMAGE_BASE}{hex_id}" if hex_id else ""


def _image_sort_key(img) -> tuple[int, int]:
    width = _as_int(getattr(img, "width", 0))
    height = _as_int(getattr(img, "height", 0))
    area = width * height
    size_rank = _SIZE_RANK.get(_as_int(getattr(img, "size", 0)), -1)
    return (area, size_rank)
