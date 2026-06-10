# Ground-truth spec: isrc
Source Go files: spotify_totp.go, isrc_finder.go, isrc_helper.go, isrc_cache.go

I have confirmed: `totp.GenerateCode(secret, t)` uses defaults Period=30, Digits=6, Algorithm=SHA1 (RFC 6238), and the secret passed to it is base32-encoded (which `key.Secret()` returns — the raw base32 string from the otpauth URL). I now have everything needed for a complete, byte-for-byte spec.

---

# SpotiFLAC ISRC Acquisition — Python Porting Spec

Source files: `spotify_totp.go`, `isrc_finder.go`, `isrc_helper.go`, `isrc_cache.go` (plus cross-referenced `soundplate.go`, `songlink.go`, `ffmpeg.go`). All constants reproduced verbatim. Target: byte-for-byte reproduction in Python without reading the Go.

## 0. On-disk app directory (shared by all caches)

```python
import os
def get_app_dir() -> str:
    # Go: filepath.Join(os.UserHomeDir(), ".spotiflac")
    return os.path.join(os.path.expanduser("~"), ".spotiflac")
# EnsureAppDir() = get_app_dir() + os.makedirs(dir, exist_ok=True)  (Go mode 0o755)
```
All cache files live under `~/.spotiflac/`. Go's `os.UserHomeDir()` == `$HOME` on Unix, `%USERPROFILE%` on Windows. `EnsureAppDir` is what creates the dir before any cache file is written.

---

## 1. TOTP secret + version constants and TOTP computation

### Constants (verbatim, from `spotify_totp.go`)
```python
SPOTIFY_TOTP_SECRET = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
SPOTIFY_TOTP_VERSION = 61   # int, sent as the string "61"
```

### How the TOTP is computed

The Go code does:
```go
key, _ := otp.NewKeyFromURL(fmt.Sprintf("otpauth://totp/secret?secret=%s", spotifyTOTPSecret))
code, _ := totp.GenerateCode(key.Secret(), now)
```

Critical facts about the `github.com/pquerna/otp` library behavior to reproduce:
- `NewKeyFromURL` parses the otpauth URL; `key.Secret()` returns the **raw `secret` query value unchanged** — i.e. the string `SPOTIFY_TOTP_SECRET` itself. **No transformation is applied to the secret string here.**
- `totp.GenerateCode(secret, t)` interprets `secret` as a **Base32-encoded** byte string (RFC 4648 Base32, standard alphabet, **uppercase**, padding optional — the library strips/ignores padding and uppercases). It then runs standard RFC 6238 TOTP with **default parameters**:
  - **Algorithm: HMAC-SHA1**
  - **Digits: 6** (zero-padded decimal, modulo 10^6)
  - **Period: 30 seconds**
  - Counter `T = floor(unixSeconds / 30)`, packed as an **8-byte big-endian** integer.
  - Dynamic truncation per RFC 4226 (offset = last nibble of HMAC, take 4 bytes, mask top bit `& 0x7FFFFFFF`).

> DIVERGENCE FLAG: The TOTP secret here is **the literal Base32 string itself decoded as Base32 bytes** — do NOT first re-encode it. Many Spotify TOTP ports instead use a hex/byte-array secret transformed via a cyclic XOR ("magic" mapping) scheme. **This codebase does NOT do that.** It feeds `SPOTIFY_TOTP_SECRET` directly into a standard RFC-6238 TOTP-SHA1-6-30. Reproduce exactly that. The Base32 secret decodes to 60 bytes of key material.

Python implementation (no deps beyond stdlib):
```python
import base64, hashlib, hmac, struct, time

def _base32_decode(secret: str) -> bytes:
    s = secret.strip().upper().replace(" ", "")
    pad = (-len(s)) % 8
    return base64.b32decode(s + ("=" * pad))

def generate_spotify_totp(now_unix: float | None = None) -> tuple[str, int]:
    if now_unix is None:
        now_unix = time.time()
    key = _base32_decode(SPOTIFY_TOTP_SECRET)
    counter = int(now_unix) // 30                      # period = 30
    msg = struct.pack(">Q", counter)                   # 8-byte big-endian
    h = hmac.new(key, msg, hashlib.sha1).digest()      # SHA1
    offset = h[-1] & 0x0F
    code_int = (struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code_int:06d}", SPOTIFY_TOTP_VERSION      # 6 digits, zero-padded
```

### How `totp` / `totpServer` / `totpVer` are sent
See Section 2 — they are query params on the token request. Note **`totp` and `totpServer` carry the *same* generated code value**, and `totpVer` is the version as a decimal string (`"61"`).

---

## 2. Anonymous token request + on-disk caching + refresh logic

### Endpoint constant (verbatim)
```python
SPOTIFY_SESSION_TOKEN_URL = "https://open.spotify.com/api/token"
SPOTIFY_TOKEN_CACHE_FILE  = ".isrc-finder-token.json"   # under ~/.spotiflac/
```

### Cache file path
`~/.spotiflac/.isrc-finder-token.json`. Written with `json.MarshalIndent(token, "", "  ")` → **2-space-indented JSON**, file mode `0o644`, directory created with `0o755`.

### Cached token JSON shape (exact keys)
```json
{
  "accessToken": "<string>",
  "accessTokenExpirationTimestampMs": 1700000000000
}
```
Go struct tags: `accessToken` (string), `accessTokenExpirationTimestampMs` (int64, epoch **milliseconds**). The server response uses these same key names and is unmarshaled into the same struct, then re-serialized to disk verbatim (only these two fields are persisted; any extra response fields are dropped).

### Validity / refresh-before-expiry rule (exact)
```go
func spotifyTokenIsValid(token) bool {
    if token == nil || token.AccessToken == "" || token.AccessTokenExpirationTimestampMs == 0 { return false }
    return time.Now().UnixMilli() < token.AccessTokenExpirationTimestampMs - 30_000
}
```
Python:
```python
def spotify_token_is_valid(tok: dict) -> bool:
    if not tok: return False
    if not tok.get("accessToken"): return False
    exp = tok.get("accessTokenExpirationTimestampMs", 0)
    if exp == 0: return False
    now_ms = int(time.time() * 1000)
    return now_ms < exp - 30_000     # refresh 30s before actual expiry
```
- TTL is server-driven (the `accessTokenExpirationTimestampMs` field). The **30,000 ms (30 s) early-refresh skew** is the only client-side TTL logic.
- The whole acquire path is guarded by a process-wide mutex (`spotifyAnonymousTokenMu`). In Python, wrap with a `threading.Lock` if concurrent.

### Acquisition flow (`requestSpotifyAnonymousAccessToken`)
1. Lock mutex.
2. Load cached token from `~/.spotiflac/.isrc-finder-token.json`. If file missing → treat as `None` (NOT an error). If file exists but unparseable JSON → **return error** (do not silently ignore).
3. If `spotify_token_is_valid(cached)` → return `cached["accessToken"]`. **No HTTP call.**
4. Else generate TOTP: `(code, version) = generate_spotify_totp(time.now())`.
5. Build query params and GET the token URL.
6. Unmarshal JSON into token struct, **save to disk** (overwrite), return `accessToken`.

### The HTTP call (exact)
- **Method:** `GET`
- **URL:** `https://open.spotify.com/api/token?` + `query.Encode()`
- **Query params** (Go `url.Values`):
  | param | value |
  |---|---|
  | `reason` | `init` |
  | `productType` | `web-player` |
  | `totp` | the generated 6-digit code |
  | `totpServer` | **the same** generated 6-digit code |
  | `totpVer` | `"61"` (`strconv.Itoa(version)`) |

  > Go's `url.Values.Encode()` sorts keys **alphabetically** and URL-escapes via `QueryEscape` (spaces→`+`). Resulting query string order: `productType=web-player&reason=init&totp=NNNNNN&totpServer=NNNNNN&totpVer=61`. The exact ordering is cosmetic for this server, but reproduce it if matching byte-for-byte: in Python use `urllib.parse.urlencode(sorted(params.items()))` or pass an ordered dict pre-sorted by key.

- **Headers:** **NONE.** The Go code passes `headers = nil` to `requestSpotifyJSON`. No User-Agent, no Accept, no Authorization is set by the application. (Go's stdlib will still send its default `User-Agent: Go-http-client/1.1`, `Accept-Encoding: gzip`, and `Host`. A Python port using `requests`/`urllib` will send its own defaults; this has not caused issues since the endpoint does not gate on UA for the anonymous token. Do **not** add a browser UA here unless needed — the Go original sends none.)
- **Request body:** none.
- **Timeout:** the `http.Client` used is the caller's; for the ISRC path it is `&http.Client{Timeout: 30 * time.Second}` (see `GetSpotifyTrackIdentifiersDirect`). So **30 seconds total**.
- **Redirects:** Go default client follows up to 10 redirects automatically. Match with `allow_redirects=True`.

### Response handling
- HTTP status check (`requestSpotifyBytes`): success iff `200 <= status < 300`. Otherwise error with the trimmed response body as the message (or the status line if body empty).
- Body is parsed as JSON into the 2-field token struct. Unknown fields ignored.
- On success, the token (only `accessToken` + `accessTokenExpirationTimestampMs`) is written to disk.

---

## 3. Base62 decode of the 22-char Spotify track ID → 32-char hex GID

### Alphabet (verbatim)
```python
SPOTIFY_BASE62_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
# index 0-9   = '0'..'9'
# index 10-35 = 'a'..'z'  (lowercase BEFORE uppercase)
# index 36-61 = 'A'..'Z'
```
> DIVERGENCE FLAG: This alphabet is **digits, then lowercase, then uppercase** — NOT the more common "uppercase-before-lowercase" base62 variant. Spotify IDs are case-sensitive; using the wrong ordering yields a wrong GID. Reproduce this exact 62-char string.

### Decode algorithm (`spotifyEntityIDToGID`) — applies to both track and album IDs
```go
value := big.NewInt(0); base := big.NewInt(62)
for _, char := range entityID {
    index := strings.IndexRune(spotifyBase62Alphabet, char)   // -1 if not found → error
    if index < 0 { return "", error("invalid base62 character") }
    value = value*62 + index
}
hexValue := value.Text(16)            // lowercase hex, NO leading zeros
if len(hexValue) < 32 {
    hexValue = strings.Repeat("0", 32-len(hexValue)) + hexValue   // left-pad to 32
}
return hexValue   // NOTE: if len > 32, it is NOT truncated
```

Python:
```python
def spotify_entity_id_to_gid(entity_id: str) -> str:
    if entity_id == "":
        raise ValueError("entity ID is empty")
    value = 0
    for ch in entity_id:                       # iterate by Unicode code point
        idx = SPOTIFY_BASE62_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError(f"invalid base62 character: {ch!r}")
        value = value * 62 + idx
    hex_value = format(value, "x")             # lowercase, no leading zeros, like big.Int.Text(16)
    if len(hex_value) < 32:
        hex_value = "0" * (32 - len(hex_value)) + hex_value
    return hex_value                           # NOT truncated if >32 (won't happen for valid 22-char IDs)
```
- Output is **lowercase hex, zero-padded on the left to a minimum of 32 chars**. A valid 22-char base62 track ID decodes to a 128-bit number → exactly 32 hex chars.
- `spotifyTrackIDToGID(trackID)` is just `spotifyEntityIDToGID(trackID)` — track and album use the same decoder.

### Track-ID normalization (`extractSpotifyTrackID`) — run BEFORE decoding
Accepts: a raw 22-char ID, a `spotify:track:<id>` URI, or an `http(s)://.../track/<id>` URL.
```python
import urllib.parse
def extract_spotify_track_id(value: str) -> str:
    value = value.strip()
    if value == "":
        raise ValueError("track input is required")
    if value.startswith("spotify:track:"):
        return value[value.rfind(":") + 1:]          # substring after the LAST ':'
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme in ("http", "https"):
        parts = [p for p in parsed.path.strip("/").split("/")]   # Go: strings.Split(Trim(path,"/"),"/")
        if len(parts) >= 2 and parts[0] == "track":
            return parts[1]
        raise ValueError("expected URL like https://open.spotify.com/track/<id>")
    if len(value) == 22:
        return value
    raise ValueError("track must be a Spotify track ID, URL, or URI")
```
> Note Go quirk: `strings.Split(strings.Trim(path,"/"), "/")` of an empty path yields `[""]` (length 1), so a bare `https://x/` fails the `len>=2 && parts[0]=="track"` check and errors. Match `"/".strip("/").split("/")` semantics: in Python `"".split("/")` → `[""]` too, so behavior matches.

---

## 4. spclient metadata GET + ISRC extraction

### Endpoint constant (verbatim)
```python
SPOTIFY_GID_METADATA_URL = "https://spclient.wg.spotify.com/metadata/4/%s/%s?market=from_token"
# %s #1 = entityType ("track" or "album");  %s #2 = the 32-char hex GID
```
Example resolved URL: `https://spclient.wg.spotify.com/metadata/4/track/<32hexGID>?market=from_token`
- **`market` query param is hardcoded to `from_token`** — it is baked into the constant, not computed. The server resolves the market from the bearer token.

### The HTTP call (`fetchSpotifyRawMetadataByGID`)
First obtains an anonymous bearer token via Section 2 (`requestSpotifyAnonymousAccessToken`). Then:
- **Method:** `GET`
- **URL:** the formatted constant above.
- **Headers** (Go `http.Header.Set`; note **lowercase header names** as written — Go canonicalizes to `Authorization`/`Accept`/`User-Agent` on the wire, but the values are what matters):
  | header (wire-canonical) | value |
  |---|---|
  | `Authorization` | `Bearer ` + accessToken (literal `"Bearer "` prefix, one space) |
  | `Accept` | `application/json` |
  | `User-Agent` | `songLinkUserAgent` (see below) |

  `songLinkUserAgent` (verbatim, from `songlink.go`):
  ```python
  SONGLINK_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
  ```
- **Body:** none.
- **Timeout:** 30 s (same client).
- **Status handling:** `requestSpotifyBytes` → success iff `200 <= status < 300`, else error with body text. Returns the **raw bytes** of the body.

> No automatic retry/refresh on 401 here. If the token is expired the cache logic in Section 2 already refreshed it (30 s skew) before this call. A 401/400 from spclient is surfaced as a metadata error and triggers the **Soundplate fallback** (Section 6), not a token refresh.

### Track metadata JSON shape probed (`spotifyTrackRawData`)
```json
{
  "album": { "gid": "<hexGID-string>" },
  "external_id": [ { "type": "isrc", "id": "..." }, ... ]
}
```
Only `album.gid` and `external_id[]` (each `{type, id}`) are read.

### ISRC extraction order (`extractSpotifyTrackIdentifiers`)
1. **Primary:** iterate `external_id[]`. For the first entry where `type` (trimmed, **case-insensitive** via `strings.EqualFold`) equals `"isrc"`, run `firstISRCMatch(entry.id)`; if non-empty, that is the ISRC; **break**.
2. **Whole-body regex fallback:** if step 1 produced no ISRC, run `firstISRCMatch(string(payload))` over the **entire raw response body**.
3. (UPC, separately:) if `album.gid` present and a client is supplied, fetch album metadata (`metadata/4/album/<albumGID>?market=from_token`, same headers) and extract UPC — see Section 5. UPC failures are swallowed (ISRC still returned).

### The ISRC validation regex (`isrcPattern`, verbatim) + `firstISRCMatch`
```python
import re
ISRC_PATTERN = re.compile(r"\b([A-Z]{2}[A-Z0-9]{3}\d{7})\b")

def first_isrc_match(body: str) -> str:
    m = ISRC_PATTERN.search(body.upper())     # NOTE: uppercases input FIRST
    if not m:
        return ""
    return m.group(1).strip()
```
- Pattern: 2 uppercase letters, then 3 alphanumeric (uppercase letter or digit), then 7 digits, on word boundaries. **12 chars total.**
- `firstISRCMatch` **uppercases the entire input before matching** — so lowercase ISRCs in the JSON still match. Returns the first match, trimmed.
- `\b` word boundaries: a Python `re` `\b` matches the same as Go's `regexp` `\b` here (ASCII context). Equivalent.

> DIVERGENCE FLAG: Because `firstISRCMatch` uppercases and regex-validates, the returned ISRC is effectively normalized to uppercase at extraction time. Downstream cache/`ResolveTrackISRC` also force-uppercase. Keep ISRCs uppercase everywhere.

---

## 5. Album UPC extraction (`extractSpotifyAlbumUPC`)

Used for the optional UPC alongside ISRC. Album metadata JSON probed (`spotifyAlbumRawData`):
```json
{ "external_id": [ { "type": "upc", "id": "..." }, ... ] }
```
Logic:
```python
def extract_spotify_album_upc(payload_bytes: bytes) -> str:
    album = json.loads(payload_bytes)
    for ext in album.get("external_id", []):
        if (ext.get("type") or "").strip().lower() == "upc":   # EqualFold → case-insensitive
            upc = (ext.get("id") or "").strip()
            if upc != "":
                return upc
    raise ValueError("UPC not found in Spotify album metadata")
```
- UPC is **NOT** regex-validated (unlike ISRC) — it is just trimmed and returned as-is. First matching `upc` entry wins.
- `lookupSpotifyAlbumUPC(albumID)` is a separate standalone entry point: trims album ID, fetches album metadata by GID, extracts UPC.

---

## 6. Soundplate fallback (`soundplate.go`)

Triggered (in `GetSpotifyTrackIdentifiersDirect`) when ISRC is still empty after the spclient metadata path (either metadata errored, or metadata succeeded but yielded no ISRC).

### Constants (verbatim)
```python
SOUNDPLATE_SPOTIFY_API_URL = "https://phpstack-822472-6184058.cloudwaysapps.com/api/spotify.php"
SOUNDPLATE_REFERER_URL     = "https://phpstack-822472-6184058.cloudwaysapps.com/?"
SOUNDPLATE_USER_AGENT      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
```
(Note: Soundplate UA is Chrome **146**, distinct from songLink's Chrome **145**.)

### The HTTP call (`lookupSpotifyISRCViaSoundplate`)
1. Normalize input via `extract_spotify_track_id` (Section 3).
2. Build `spotifyTrackURL = "https://open.spotify.com/track/<normalizedTrackID>"`.
3. Query: single param `q` = that track URL. `query.Encode()` URL-escapes it: `q=https%3A%2F%2Fopen.spotify.com%2Ftrack%2F<id>`.
- **Method:** `GET`
- **URL:** `SOUNDPLATE_SPOTIFY_API_URL + "?" + urlencode({"q": spotifyTrackURL})`
- **Headers** (exact casing + values):
  | header | value |
  |---|---|
  | `User-Agent` | `SOUNDPLATE_USER_AGENT` |
  | `Accept` | `*/*` |
  | `Referer` | `https://phpstack-822472-6184058.cloudwaysapps.com/?` |
  | `Accept-Language` | `en-US,en;q=0.9,id;q=0.8` |
  | `Sec-CH-UA` | `"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"` |
  | `Sec-CH-UA-Mobile` | `?0` |
  | `Sec-CH-UA-Platform` | `"Windows"` |
  | `Sec-Fetch-Dest` | `empty` |
  | `Sec-Fetch-Mode` | `cors` |
  | `Sec-Fetch-Site` | `same-origin` |
  | `Priority` | `u=1, i` |
- **Body:** none.
- **Timeout:** uses `s.client` = `NewSongLinkClient()` → `http.Client{Timeout: 30 * time.Second}` → **30 s**.
- **Status handling:** success iff status == **200** exactly. On non-200, error `"Soundplate ISRC returned status %d (%s)"` with body preview truncated to **256 chars**.

### Response shape (`soundplateSpotifyResponse`) — JSON field is `isrc`
```json
{
  "name": "...", "artist": "...", "album": "...", "album_type": "...",
  "artwork_url": "...", "isrc": "USRC11234567", "year": "...",
  "spotify_url": "https://open.spotify.com/track/<id>"
}
```
Only `isrc` and `spotify_url` are used.

### ISRC extraction (Soundplate)
```python
payload = json.loads(body)
isrc = first_isrc_match(payload.get("isrc", ""))
if isrc == "":
    isrc = first_isrc_match(body_text)        # whole-body regex fallback over raw response
if isrc == "":
    raise ValueError("ISRC missing in Soundplate response")
resolved_track_id = ""
if payload.get("spotify_url", ""):
    try: resolved_track_id = extract_spotify_track_id(payload["spotify_url"])
    except ValueError: pass
return isrc, resolved_track_id
```
- Same `firstISRCMatch` (uppercase + regex). Primary = `isrc` field, fallback = whole raw body.
- `resolved_track_id` is parsed from `spotify_url` for cache write-through (cached under both the requested and resolved IDs if they differ).

---

## 7. ISRC disk cache (`isrc_cache.go`) — bbolt key/value store

### Constants (verbatim)
```python
ISRC_CACHE_DB_FILE = "isrc_cache.db"          # under ~/.spotiflac/
ISRC_CACHE_BUCKET  = "SpotifyTrackISRC"       # bbolt bucket name
```
Full path: `~/.spotiflac/isrc_cache.db`. This is a **bbolt** (Go BoltDB) embedded key/value file, opened mode `0o600`, with `Timeout: 1 * time.Second` on lock acquisition. Bucket `SpotifyTrackISRC` created if absent.

> PORTING NOTE: bbolt is a Go-specific B+tree file format. A Python port cannot read/write the *same* file unless using a bbolt-compatible reader. Two options: (a) reimplement with any local KV/JSON/sqlite store keyed identically (recommended for a fresh Python app), or (b) use a bbolt-compatible library. The **logical contract** below is what must be preserved; the on-disk binary format need not match unless interop with the Go app is required.

### Entry value format (JSON, exact keys)
Each bucket entry: **key = trackID (string, trimmed), value = JSON bytes** of:
```json
{ "track_id": "<trimmed id>", "isrc": "<UPPERCASE isrc>", "updated_at": 1700000000 }
```
Go struct `isrcCacheEntry`: `track_id` (string), `isrc` (string), `updated_at` (int64 = `time.Now().Unix()`, **epoch seconds**).

### `GetCachedISRC(trackID)`
```python
def get_cached_isrc(track_id: str) -> str:
    t = track_id.strip()
    if t == "": return ""
    raw = bucket.get(t)               # None/empty if absent
    if not raw: return ""
    entry = json.loads(raw)
    return (entry.get("isrc") or "").strip().upper()   # force UPPERCASE on read
```
- Key is the **trimmed** trackID, used verbatim (the raw/normalized track ID string — for the main path this is the 22-char ID).
- Returns uppercase-trimmed ISRC, or `""` if absent.
- **No TTL / expiry check on read.** `updated_at` is stored but **never read for invalidation** — cache entries effectively live forever (no TTL). The only place TTL exists in this subsystem is the token cache's 30 s skew (Section 2), NOT the ISRC cache.

### `PutCachedISRC(trackID, isrc)`
```python
def put_cached_isrc(track_id: str, isrc: str) -> None:
    t = track_id.strip()
    i = isrc.strip().upper()
    if t == "" or i == "": return        # silently no-op on empty
    entry = {"track_id": t, "isrc": i, "updated_at": int(time.time())}
    bucket.put(t, json.dumps(entry).encode())
```
- ISRC is **uppercased + trimmed** before storage. Empty key or empty ISRC → no-op (no error).
- `json.Marshal` in Go produces keys in **struct-declaration order**: `track_id`, `isrc`, `updated_at`, compact (no spaces). Match if byte-identical values matter; for logical correctness, order is irrelevant.

### Write-through (`cacheResolvedSpotifyTrackISRC`)
After a successful lookup, the ISRC is cached under the requested `trackID`; and if a `resolvedTrackID` was obtained (from Soundplate's `spotify_url`) and differs from `trackID`, it is **also** cached under `resolvedTrackID`. Cache-write failures are logged as warnings, never fatal.

---

## 8. Top-level orchestration & fallback ordering

### `GetSpotifyTrackIdentifiersDirect(spotifyTrackID)` — the master flow
1. Normalize ID (`extract_spotify_track_id`). Error → return.
2. **Cache read:** `get_cached_isrc(normalizedID)`. If non-empty, seed `identifiers.ISRC` (but flow continues — it still tries metadata to also get UPC).
3. Create `http.Client{Timeout: 30s}`.
4. **spclient metadata path** (`fetchSpotifyTrackRawData` → token + GID decode + GET):
   - If fetch succeeds: extract ISRC (external_id then whole-body regex) + UPC (album fetch). Merge (`mergeSpotifyTrackIdentifiers` trims both). If ISRC now non-empty → log + **cache it** (`cacheResolvedSpotifyTrackISRC(id, "", isrc)`). If **both** ISRC and UPC non-empty → **return early**.
   - `metadataErr` is set to the extract error (nil on success).
5. If metadata path failed → log warning, fall to Soundplate.
6. **Soundplate fallback** (only if `identifiers.ISRC == ""`):
   - `lookupSpotifyISRCViaSoundplate(normalizedID)`. On success with non-empty ISRC → set ISRC, cache under both requested + resolved IDs, **return**.
   - Error combination handling:
     - if `metadataErr != nil` **and** `soundplateErr != nil` → return combined error: `"spotify metadata lookup failed: %v | soundplate lookup failed: %w"`.
     - if `soundplateErr != nil` **and** `identifiers.UPC == ""` → return `soundplateErr`.
7. If ISRC or UPC non-empty → return identifiers (success).
8. If `metadataErr != nil` → return it.
9. Else → error `"no Spotify identifiers found for track %s"`.

> Fallback order is strictly: **disk cache (seed) → Spotify spclient metadata → Soundplate**. Cache seeding does not short-circuit the metadata call (so UPC can still be fetched), but a fully-populated cache+metadata ISRC+UPC returns early.

### `ResolveTrackISRC(spotifyTrackID)` (from `isrc_helper.go`) — the simple public helper
```python
def resolve_track_isrc(spotify_track_id: str) -> str:
    spotify_track_id = spotify_track_id.strip()
    if spotify_track_id == "":
        return ""
    cached = get_cached_isrc(spotify_track_id)     # cache uses the RAW (un-normalized) input here
    if cached:                                      # err==nil && cached!=""
        return cached.strip().upper()
    try:
        isrc = SongLinkClient().get_isrc_direct(spotify_track_id)   # → lookupSpotifyISRC → GetSpotifyTrackIdentifiersDirect
    except Exception:
        return ""                                   # any error → empty string
    return isrc.strip().upper()
```
- `ResolveTrackISRC` is best-effort: **never raises**, returns `""` on any failure.
- It checks the cache **with the raw, un-normalized input** (does not call `extract_spotify_track_id` first). The deeper `GetSpotifyTrackIdentifiersDirect` does normalize. So cache keys can be either the raw input or the normalized 22-char ID depending on entry path — when porting, store/look up under the **same string the caller passed** at this layer, and additionally under the normalized ID inside the deeper path (which is exactly what the Go does).
- `GetISRCDirect(spotifyID)` == `lookupSpotifyISRC(spotifyID)` == `GetSpotifyTrackIdentifiersDirect(...)` then require `identifiers.ISRC != ""` else error `"no Spotify ISRC found for track %s"` (with trimmed input).

---

## 9. Summary of constants table (copy verbatim)

```python
# spotify_totp.go
SPOTIFY_TOTP_SECRET  = "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4DQOJUGQYTAMRRGA2TCMJSHE3TCMBY"
SPOTIFY_TOTP_VERSION = 61                         # sent as "61"

# isrc_finder.go
SPOTIFY_SESSION_TOKEN_URL = "https://open.spotify.com/api/token"
SPOTIFY_GID_METADATA_URL  = "https://spclient.wg.spotify.com/metadata/4/%s/%s?market=from_token"
SPOTIFY_BASE62_ALPHABET   = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
SPOTIFY_TOKEN_CACHE_FILE  = ".isrc-finder-token.json"   # ~/.spotiflac/.isrc-finder-token.json

# songlink.go
SONGLINK_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
ISRC_PATTERN        = r"\b([A-Z]{2}[A-Z0-9]{3}\d{7})\b"

# soundplate.go
SOUNDPLATE_SPOTIFY_API_URL = "https://phpstack-822472-6184058.cloudwaysapps.com/api/spotify.php"
SOUNDPLATE_REFERER_URL     = "https://phpstack-822472-6184058.cloudwaysapps.com/?"
SOUNDPLATE_USER_AGENT      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

# isrc_cache.go
ISRC_CACHE_DB_FILE = "isrc_cache.db"              # ~/.spotiflac/isrc_cache.db  (bbolt)
ISRC_CACHE_BUCKET  = "SpotifyTrackISRC"

# ffmpeg.go (app dir)
APP_DIR = "~/.spotiflac"                          # filepath.Join(UserHomeDir(), ".spotiflac")
```

## 10. Key divergence / gotcha flags for the porter

1. **TOTP secret is used directly as a standard Base32 RFC-6238 TOTP-SHA1-6-30 key.** No XOR/byte-mapping transform (unlike many Spotify-TOTP implementations). Decode `SPOTIFY_TOTP_SECRET` as Base32 → key bytes; standard HMAC-SHA1, 6 digits, 30 s period.
2. `totp` and `totpServer` query params carry the **same** code.
3. Base62 alphabet is **digits → lowercase → uppercase**; case-sensitive; do not swap.
4. GID is **lowercase hex, left-zero-padded to 32**, NOT truncated if longer.
5. `market=from_token` is hardcoded into the metadata URL constant.
6. Token cache early-refresh skew = **30,000 ms**; ISRC cache has **no TTL** (`updated_at` is stored but never read for expiry).
7. ISRC is always normalized **uppercase** (regex requires uppercase; `firstISRCMatch` uppercases input; cache read/write uppercases).
8. Anonymous token request sends **no application headers** (Go default UA only). spclient metadata sends `Authorization`/`Accept`/`User-Agent` (Chrome 145). Soundplate sends a full browser header set (Chrome 146).
9. Whole-body regex fallback (`firstISRCMatch(string(payload))`) is applied for **both** the spclient and Soundplate responses when the structured field yields nothing.
10. Fallback order: disk cache (seed only) → Spotify spclient metadata → Soundplate. Only `ISRC==""` gates the Soundplate call; UPC presence affects the early-return and error-propagation conditions.
11. All HTTP clients here use a **30 s total timeout** and follow redirects (Go default ≤10).