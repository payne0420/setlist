"""Turn a metadata source's track JSON into the small tag dict the writer consumes.

The download seam builds ``song_meta`` from the spotifydown EMBED (``TrackInfo``),
which has **no album**, **no album track/disc number** (its ``trackNumber`` is the
1-based PLAYLIST position), and joins artist names with a non-breaking space
(``\\xa0``). Rich, accurate tags come from one of four sources that a Real-FLAC
download already touches:

* **Spotify spclient** ``/metadata/4/track/<gid>`` — the universal, most-faithful
  set (it is the exact album/number/cover for the Spotify track the user asked for).
* **Qobuz** search dict, **Deezer** track JSON, **Tidal** instance ``/info/`` — the
  provider that actually served the FLAC, as one *self-consistent* release.

Each extractor returns the same small dict shape (only the fields it actually
carries) so the seam can merge selectively:
``{title, artists, albumArtist, album, releaseDate, trackNumber, discNumber, cover}``.

ACCURACY CAVEAT: providers resolve the recording **by ISRC**, so they can land on a
*different album release* than the Spotify track (e.g. a compilation). Never mix a
field from one source with a field from another — treat each extractor's output as
one self-consistent set, and let the seam pick a single winning source per its
priority. Everything here is best-effort and never raises: a malformed/partial
payload degrades to whatever fields ARE present.
"""

from __future__ import annotations

# Spotify CDN base for album art addressed by hex file_id.
SCDN_IMAGE_BASE = "https://i.scdn.co/image/"
# Tidal album art: the cover uuid's dashes become path slashes.
TIDAL_IMAGE_TMPL = "https://resources.tidal.com/images/{path}/1280x1280.jpg"

# Spotify image size names, smallest -> largest (the JSON ships strings, not the
# protobuf's int enum). Higher rank = higher resolution.
_SPOTIFY_SIZE_RANK = {"SMALL": 0, "DEFAULT": 1, "LARGE": 2, "XLARGE": 3}


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


def _join_names(items, key: str = "name") -> str:
    """Join ``[{name: ...}, ...]`` (or list of strings) with ', ' (clean, no nbsp)."""
    names = []
    for it in items or ():
        name = _clean(it.get(key, "") if isinstance(it, dict) else it)
        if name:
            names.append(name)
    return ", ".join(names)


def _date_part(value) -> str:
    """Take the ``YYYY-MM-DD`` (or ``YYYY``) prefix of a date/ISO-timestamp string."""
    s = _clean(value)
    if not s:
        return ""
    # "2023-03-24T00:00:00.000+0000" -> "2023-03-24"; "2023" stays "2023".
    return s.split("T", 1)[0].strip()


def _put(meta: dict, key: str, value) -> None:
    """Set ``meta[key]`` only when *value* is truthy (keep the dict gap-free)."""
    if value:
        meta[key] = value


# --------------------------------------------------------------------------- #
# Spotify spclient /metadata/4/track/<gid>  (the universal, most-faithful set)
# --------------------------------------------------------------------------- #
def extract_spotify_metadata(data: dict) -> dict | None:
    """spclient track JSON -> tag dict (or ``None`` if unusable).

    Shape (verified live): ``name``, ``number``, ``disc_number``,
    ``artist[].name``, ``album{name, artist[].name, date{year,month,day},
    cover_group.image[]{size, file_id}}``. ``size`` is a STRING ("LARGE") and
    ``file_id`` is a hex STRING here (not protobuf bytes)."""
    if not isinstance(data, dict):
        return None
    meta: dict = {}
    _put(meta, "title", _clean(data.get("name")))
    _put(meta, "artists", _join_names(data.get("artist")))
    num = _as_int(data.get("number"))
    if num > 0:
        meta["trackNumber"] = num
    disc = _as_int(data.get("disc_number"))
    if disc > 0:
        meta["discNumber"] = disc
    album = data.get("album")
    if isinstance(album, dict):
        _put(meta, "album", _clean(album.get("name")))
        _put(meta, "albumArtist", _join_names(album.get("artist")))
        _put(meta, "releaseDate", _spotify_date(album.get("date")))
        _put(meta, "cover", _spotify_cover(album))
    return meta or None


def _spotify_date(date) -> str:
    if not isinstance(date, dict):
        return ""
    year = _as_int(date.get("year"))
    if year <= 0:
        return ""
    month, day = _as_int(date.get("month")), _as_int(date.get("day"))
    if month > 0 and day > 0:
        return f"{year:04d}-{month:02d}-{day:02d}"
    if month > 0:
        return f"{year:04d}-{month:02d}"
    return f"{year:04d}"


def _spotify_cover(album: dict) -> str:
    cover_group = album.get("cover_group") or {}
    images = cover_group.get("image") if isinstance(cover_group, dict) else None
    images = images or album.get("cover") or []
    best, best_key = None, None
    for img in images:
        if not isinstance(img, dict):
            continue
        file_id = img.get("file_id")
        if not file_id:
            continue
        area = _as_int(img.get("width")) * _as_int(img.get("height"))
        rank = _SPOTIFY_SIZE_RANK.get(str(img.get("size", "")).upper(), -1)
        key = (area, rank)
        if best is None or key > best_key:
            best, best_key = img, key
    if best is None:
        return ""
    fid = best["file_id"]
    hex_id = fid.hex() if isinstance(fid, (bytes, bytearray)) else str(fid)
    return f"{SCDN_IMAGE_BASE}{hex_id}" if hex_id else ""


# --------------------------------------------------------------------------- #
# Qobuz search dict  (.ground-truth/qobuz-sign-frontends.md §3.4)
# --------------------------------------------------------------------------- #
def extract_qobuz_metadata(track: dict) -> dict | None:
    """Qobuz ``QobuzTrack`` dict -> tag dict. Fields: title(+version),
    performer.name, album.title, album.artist.name, track_number,
    media_number (disc), release_date_original, album.image.{large,...}."""
    if not isinstance(track, dict):
        return None
    meta: dict = {}
    title = _clean(track.get("title"))
    version = _clean(track.get("version"))
    if title and version and version.lower() not in title.lower():
        title = f"{title} ({version})"
    _put(meta, "title", title)
    performer = track.get("performer")
    if isinstance(performer, dict):
        _put(meta, "artists", _clean(performer.get("name")))
    num = _as_int(track.get("track_number"))
    if num > 0:
        meta["trackNumber"] = num
    disc = _as_int(track.get("media_number"))
    if disc > 0:
        meta["discNumber"] = disc
    _put(meta, "releaseDate", _date_part(track.get("release_date_original")))
    album = track.get("album")
    if isinstance(album, dict):
        _put(meta, "album", _clean(album.get("title")))
        alb_artist = album.get("artist")
        if isinstance(alb_artist, dict):
            _put(meta, "albumArtist", _clean(alb_artist.get("name")))
        image = album.get("image")
        if isinstance(image, dict):
            _put(meta, "cover", _clean(image.get("large") or image.get("small")))
    return meta or None


# --------------------------------------------------------------------------- #
# Deezer track JSON  (api.deezer.com/track/isrc:<ISRC>) — the Amazon path's set
# --------------------------------------------------------------------------- #
def extract_deezer_metadata(data: dict) -> dict | None:
    """Deezer track JSON -> tag dict. Fields: title, contributors[].name (else
    artist.name), album.title, track_position, disk_number, release_date,
    album.cover_xl."""
    if not isinstance(data, dict):
        return None
    meta: dict = {}
    _put(meta, "title", _clean(data.get("title")))
    artists = _join_names(data.get("contributors"))
    if not artists:
        artist = data.get("artist")
        artists = _clean(artist.get("name")) if isinstance(artist, dict) else ""
    _put(meta, "artists", artists)
    num = _as_int(data.get("track_position"))
    if num > 0:
        meta["trackNumber"] = num
    disc = _as_int(data.get("disk_number"))
    if disc > 0:
        meta["discNumber"] = disc
    _put(meta, "releaseDate", _date_part(data.get("release_date")))
    album = data.get("album")
    if isinstance(album, dict):
        _put(meta, "album", _clean(album.get("title")))
        _put(meta, "cover", _clean(album.get("cover_xl") or album.get("cover_big")))
    return meta or None


# --------------------------------------------------------------------------- #
# Tidal instance /info/?id=<id>  (.ground-truth/tidal.md)
# --------------------------------------------------------------------------- #
def extract_tidal_metadata(data: dict) -> dict | None:
    """Tidal ``/info/`` response -> tag dict. Unwraps the ``{version, data}``
    envelope. Fields: title, artists[].name, album.title, trackNumber,
    volumeNumber (disc), streamStartDate / album.releaseDate, album.cover (uuid)."""
    if not isinstance(data, dict):
        return None
    # Unwrap the instance's {"version", "data": <tidal track>} envelope.
    if "data" in data and isinstance(data.get("data"), dict):
        data = data["data"]
    meta: dict = {}
    _put(meta, "title", _clean(data.get("title")))
    _put(meta, "artists", _join_names(data.get("artists")))
    num = _as_int(data.get("trackNumber"))
    if num > 0:
        meta["trackNumber"] = num
    disc = _as_int(data.get("volumeNumber"))
    if disc > 0:
        meta["discNumber"] = disc
    album = data.get("album") if isinstance(data.get("album"), dict) else {}
    release = _date_part(data.get("streamStartDate")) or _date_part(album.get("releaseDate"))
    _put(meta, "releaseDate", release)
    _put(meta, "album", _clean(album.get("title")))
    _put(meta, "cover", _tidal_cover(album.get("cover")))
    return meta or None


def _tidal_cover(uuid) -> str:
    uuid = _clean(uuid)
    if not uuid:
        return ""
    return TIDAL_IMAGE_TMPL.format(path=uuid.replace("-", "/"))
