"""Point-in-time snapshot constants + service endpoints, isolated in one module.

Per goal §13: the hardcoded Spotify TOTP secret/version, the Qobuz app
credentials, the GDStudio version string, and the embedded AES-GCM key material
are all server-side snapshots that **will** eventually break. Keeping them here —
clearly labelled, in one file — makes a server-side change a one-file edit rather
than a hunt across the codebase. Every value is copied verbatim from
``SpotiFLAC/backend/*.go`` (the source-of-truth); the matching ``.go`` file is
named next to each block.

If the lossless lookups suddenly start failing in the wild, the values most
likely to have rotated are (in rough order): the Qobuz ``app_secret``, the
GDStudio ``VERSION``, the Spotify ``TOTP_SECRET``, and the resolver host names.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Shared User-Agents (http_headers.go / songlink.go / soundplate.go)
# --------------------------------------------------------------------------- #
# The downloader UA (Chrome 146) used by the signed Qobuz API + every Qobuz/
# Amazon/Tidal request. NewRequestWithDefaultHeaders sets UA + this Accept.
DEFAULT_DOWNLOADER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT = "application/json, text/plain, */*"

# Songlink/Deezer/Songstats use a *different* Chrome version (145). Kept distinct
# from the downloader UA on purpose (bridge spec divergence flag #2).
SONGLINK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
# Soundplate uses Chrome 146 but with a full browser header set (see spotify_isrc).
SOUNDPLATE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)

# --------------------------------------------------------------------------- #
# Spotify ISRC flow (spotify_totp.go / isrc_finder.go / soundplate.go)
# --------------------------------------------------------------------------- #
# RFC-6238 TOTP: the secret is a plain Base32 key fed straight to TOTP-SHA1-6-30.
# NO XOR/byte-mapping transform (isrc spec divergence flag #1).
SPOTIFY_TOTP_SECRET = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
SPOTIFY_TOTP_VERSION = 61  # sent as totpVer="61"; NOT part of the OTP math

SPOTIFY_SESSION_TOKEN_URL = "https://open.spotify.com/api/token"
# %s #1 = entity type ("track"); %s #2 = the 32-char hex GID. market is baked in.
SPOTIFY_GID_METADATA_URL = (
    "https://spclient.wg.spotify.com/metadata/4/{etype}/{gid}?market=from_token"
)
# digits -> lowercase -> uppercase (isrc spec divergence flag #3). Case-sensitive.
SPOTIFY_BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

SOUNDPLATE_SPOTIFY_API_URL = "https://phpstack-822472-6184058.cloudwaysapps.com/api/spotify.php"
SOUNDPLATE_REFERER_URL = "https://phpstack-822472-6184058.cloudwaysapps.com/?"

# --------------------------------------------------------------------------- #
# Songlink bridge (songlink.go)
# --------------------------------------------------------------------------- #
DEEZER_ISRC_URL = "https://api.deezer.com/track/isrc:{isrc}"
SONGLINK_LINKS_URL = "https://api.song.link/v1-alpha.1/links"

# --------------------------------------------------------------------------- #
# Qobuz signed metadata API (qobuz_api.go)
# --------------------------------------------------------------------------- #
QOBUZ_API_BASE_URL = "https://www.qobuz.com/api.json/0.2"
QOBUZ_DEFAULT_APP_ID = "712109809"
QOBUZ_DEFAULT_APP_SECRET = "589be88e4538daea11f509d29e4a23b1"
QOBUZ_OPEN_TRACK_PROBE_URL = "https://open.qobuz.com/track/1"
QOBUZ_CREDENTIALS_PROBE_ISRC = "USUM71703861"
QOBUZ_CREDENTIALS_CACHE_FILE = "qobuz-api-credentials.json"
QOBUZ_CREDENTIALS_CACHE_TTL_S = 24 * 3600  # 24h

# --------------------------------------------------------------------------- #
# Qobuz CDN frontends (provider_endpoints.go / qobuz.go)
# --------------------------------------------------------------------------- #
QOBUZ_WJHE_STREAM_API_URL = "https://music.wjhe.top/api/music/qobuz/url"
QOBUZ_GDSTUDIO_API_URL_XYZ = "https://music.gdstudio.xyz/api.php"
QOBUZ_GDSTUDIO_API_URL_ORG = "https://music.gdstudio.org/api.php"
QOBUZ_MUSICDL_DOWNLOAD_API_URL = "https://www.musicdl.me/api/qobuz/download"
QOBUZ_GDSTUDIO_VERSION = "2026.5.10"  # zero-padded per dotted part -> "20260510"
# A real Qobuz track id used by health probes (qobuz.go).
QOBUZ_PROBE_TRACK_ID = 341032040

# MusicDL X-Debug-Key: SHA-256(seed parts) -> AES-256-GCM decrypt (ciphertext||tag).
# Byte arrays copied VERBATIM from qobuz.go. seed concat == b"spotiflac:qobuz:musicdl:v1",
# AAD == b"qobuz|musicdl|debug|v1"; plaintext == "ryzmicisgoatedandnothingcomesevenclose".
QOBUZ_MUSICDL_DEBUG_KEY_SEED_PARTS = (
    bytes([0x73, 0x70, 0x6F, 0x74, 0x69, 0x66]),
    bytes([0x6C, 0x61, 0x63, 0x3A, 0x71, 0x6F]),
    bytes([0x62, 0x75, 0x7A, 0x3A, 0x6D, 0x75, 0x73, 0x69, 0x63, 0x64, 0x6C, 0x3A, 0x76, 0x31]),
)
QOBUZ_MUSICDL_DEBUG_KEY_AAD = bytes(
    [
        0x71,
        0x6F,
        0x62,
        0x75,
        0x7A,
        0x7C,
        0x6D,
        0x75,
        0x73,
        0x69,
        0x63,
        0x64,
        0x6C,
        0x7C,
        0x64,
        0x65,
        0x62,
        0x75,
        0x67,
        0x7C,
        0x76,
        0x31,
    ]
)
QOBUZ_MUSICDL_DEBUG_KEY_NONCE = bytes(
    [0x91, 0x2A, 0x5C, 0x77, 0x0F, 0x33, 0xA8, 0x14, 0x62, 0x9D, 0xCE, 0x41]
)
QOBUZ_MUSICDL_DEBUG_KEY_CIPHERTEXT = bytes(
    [
        0xF3,
        0x4A,
        0x83,
        0x45,
        0x24,
        0xB6,
        0x22,
        0xAF,
        0xD6,
        0xC3,
        0x6E,
        0x2D,
        0x56,
        0xD1,
        0xBB,
        0x0B,
        0xE9,
        0x1B,
        0x4F,
        0x1C,
        0x5F,
        0x41,
        0x55,
        0xC2,
        0xC6,
        0xDF,
        0xAD,
        0x21,
        0x58,
        0xFE,
        0xD5,
        0xB8,
        0x2D,
        0x29,
        0xF9,
        0x9E,
        0x6F,
        0xD6,
    ]
)
QOBUZ_MUSICDL_DEBUG_KEY_TAG = bytes(
    [0x69, 0x0C, 0x42, 0x70, 0x14, 0x83, 0xFF, 0x14, 0xC8, 0xBE, 0x17, 0x00, 0x69, 0xB1, 0xFE, 0xBB]
)

# --------------------------------------------------------------------------- #
# Amazon proxy + DRM (amazon.go / provider_endpoints.go)
# --------------------------------------------------------------------------- #
AMAZON_API_BASE = "https://amazon.spotbye.qzz.io"
# X-Debug-Key: SHA-256(seed parts) -> AES-256-GCM decrypt. seed concat ==
# b"spotiflac:amazon:spotbye:api:v1", AAD == b"amazon|spotbye|debug|v1";
# plaintext == "spotbyeqzziokofiafkarxyz".
AMAZON_DEBUG_KEY_SEED_PARTS = (b"spotif", b"lac:am", b"azon:spotbye:api:v1")
AMAZON_DEBUG_KEY_AAD = bytes(
    [
        0x61,
        0x6D,
        0x61,
        0x7A,
        0x6F,
        0x6E,
        0x7C,
        0x73,
        0x70,
        0x6F,
        0x74,
        0x62,
        0x79,
        0x65,
        0x7C,
        0x64,
        0x65,
        0x62,
        0x75,
        0x67,
        0x7C,
        0x76,
        0x31,
    ]
)
AMAZON_DEBUG_KEY_NONCE = bytes(
    [0x52, 0x1F, 0xA4, 0x9C, 0x13, 0x77, 0x5B, 0xE2, 0x81, 0x44, 0x90, 0x6D]
)
AMAZON_DEBUG_KEY_CIPHERTEXT = bytes(
    [
        0x5B,
        0xF9,
        0xC1,
        0x2E,
        0x58,
        0xF8,
        0x5B,
        0xC0,
        0x04,
        0x68,
        0x7E,
        0xFF,
        0x3D,
        0xD6,
        0x8B,
        0xE3,
        0x86,
        0x49,
        0x6C,
        0xFD,
        0xC1,
        0x49,
        0x0B,
        0xFB,
    ]
)
AMAZON_DEBUG_KEY_TAG = bytes(
    [0x6C, 0x21, 0x98, 0x51, 0xF2, 0x38, 0x4B, 0x4A, 0x23, 0xE1, 0xC6, 0xD7, 0x65, 0x7F, 0xFB, 0xA1]
)
