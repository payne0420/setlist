# Ground-truth spec: amazon
Source Go files: amazon.go (read in full — ~16KB)

The decryption works and I have the worked example. Now I have everything needed for the spec.

# Amazon Music Provider — Porting Spec (`amazon.go`)

Source of truth: `SpotiFLAC/backend/amazon.go` (read in full, 538 lines). Cross-file dependencies confirmed in `provider_endpoints.go`, `http_headers.go`, `songlink.go`.

This provider downloads Amazon Music CENC-encrypted streams through a proxy, decrypts them with ffmpeg, and **only counts as a successful lossless result if the decrypted codec is `flac`**. The lossy-vs-FLAC verdict is left to ffprobe; the file is renamed `.flac` vs `.m4a` accordingly.

---

## 1. The AES-GCM Debug Key (`X-Debug-Key`)

The proxy requires a static secret `X-Debug-Key`. It is **NOT** stored in plaintext — it is AES-256-GCM-decrypted at runtime from hardcoded ciphertext. The key for the cipher is `sha256` of a seed assembled from 3 byte-slice parts (this is the **qobuz-style seed-parts** approach, NOT a single string literal hash, though concatenation yields exactly one string).

### 1a. Verbatim constants (copy as-is)

```python
# Seed parts — concatenated IN ORDER, then SHA-256'd to form the AES key.
# Go: hasher.Write(part) for each part; key = sha256.Sum(nil)
AMAZON_DEBUG_KEY_SEED_PARTS = [
    b"spotif",
    b"lac:am",
    b"azon:spotbye:api:v1",
]
# Concatenation == b"spotiflac:amazon:spotbye:api:v1"
# CONFIRMED: key = sha256(b"spotiflac:amazon:spotbye:api:v1")
#   sha256 hex = 9fce9ffe4f1206ad6649956fdcc395e30251a23120d961a63f84ad9cab040372

# AAD (additional authenticated data) — ASCII "amazon|spotbye|debug|v1"
AMAZON_DEBUG_KEY_AAD = bytes([
    0x61, 0x6d, 0x61, 0x7a, 0x6f, 0x6e, 0x7c, 0x73, 0x70, 0x6f, 0x74, 0x62,
    0x79, 0x65, 0x7c, 0x64, 0x65, 0x62, 0x75, 0x67, 0x7c, 0x76, 0x31,
])

# GCM nonce (12 bytes)
AMAZON_DEBUG_KEY_NONCE = bytes([
    0x52, 0x1f, 0xa4, 0x9c, 0x13, 0x77, 0x5b, 0xe2, 0x81, 0x44, 0x90, 0x6d,
])

# Ciphertext (24 bytes)
AMAZON_DEBUG_KEY_CIPHERTEXT = bytes([
    0x5b, 0xf9, 0xc1, 0x2e, 0x58, 0xf8, 0x5b, 0xc0, 0x04, 0x68, 0x7e, 0xff,
    0x3d, 0xd6, 0x8b, 0xe3, 0x86, 0x49, 0x6c, 0xfd, 0xc1, 0x49, 0x0b, 0xfb,
])

# GCM tag (16 bytes)
AMAZON_DEBUG_KEY_TAG = bytes([
    0x6c, 0x21, 0x98, 0x51, 0xf2, 0x38, 0x4b, 0x4a, 0x23, 0xe1, 0xc6, 0xd7,
    0x65, 0x7f, 0xfb, 0xa1,
])
```

### 1b. Decryption formula

```
key   = sha256( b"".join(SEED_PARTS) )            # 32 bytes
sealed = CIPHERTEXT + TAG                          # Go AES-GCM appends tag to ciphertext
plaintext = AES_GCM(key).decrypt(NONCE, sealed, AAD)   # AAD is authenticated, not encrypted
X_DEBUG_KEY = plaintext.decode("utf-8")            # Go: string(plaintext)
```

Go uses `crypto/cipher` GCM: `gcm.Open(nil, nonce, ciphertext||tag, aad)`. Python `cryptography`'s `AESGCM.decrypt(nonce, ciphertext+tag, aad)` matches exactly (also appends tag at end). Default GCM tag size = 16 bytes (Go default; matches our tag length).

### 1c. WORKED EXAMPLE (verified by running Python)

```
seed concat            = b'spotiflac:amazon:spotbye:api:v1'
sha256(seed) hex       = 9fce9ffe4f1206ad6649956fdcc395e30251a23120d961a63f84ad9cab040372
AAD as ASCII           = "amazon|spotbye|debug|v1"
DECRYPTED X-Debug-Key  = "spotbyeqzziokofiafkarxyz"   (24 chars, ASCII)
```

So the header sent is literally `X-Debug-Key: spotbyeqzziokofiafkarxyz`. (You may hardcode this string in Python, but reproducing the decrypt keeps parity and is the "source of truth" behavior.)

### 1d. Go quirks for the key

- `sync.Once` — computed lazily once, memoized (`amazonMusicDebugKey`, `amazonMusicDebugKeyErr`). Port as a module-level cached value / `functools.lru_cache`.
- If GCM open fails, the whole download aborts with `"failed to decrypt Amazon debug key: <err>"`. With the constants above it always succeeds.

---

## 2. Provider proxy: track-info GET

### Base URL (from `provider_endpoints.go`)
```python
AMAZON_API_BASE = "https://amazon.spotbye.qzz.io"
```

### ASIN extraction (from the resolved Amazon URL)
```python
import re
ASIN_REGEX = re.compile(r"(B[0-9A-Z]{9})")   # Go: regexp.MustCompile(`(B[0-9A-Z]{9})`)
asin = ASIN_REGEX.search(amazon_url)          # Go FindString = first match
# if no match -> error: f"failed to extract ASIN from URL: {amazon_url}"
```
Note: ASIN is a `B` followed by exactly 9 chars from `[0-9A-Z]` (10 chars total). `FindString` returns the **first** match anywhere in the string.

### The HTTP request
- **Method:** `GET`
- **URL template:** `f"{AMAZON_API_BASE}/api/track/{asin}"` → e.g. `https://amazon.spotbye.qzz.io/api/track/B0ABCDEFGH`
  - Go: `fmt.Sprintf("%s/api/track/%s", amazonMusicAPIBaseURL, asin)`. ASIN is **not** URL-escaped (it's `[B0-9A-Z]` so escaping is a no-op anyway).
- **Query params:** none.
- **Headers** (exact casing/values):
  | Header | Value |
  |---|---|
  | `User-Agent` | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36` |
  | `Accept` | `application/json, text/plain, */*` |
  | `X-Debug-Key` | `spotbyeqzziokofiafkarxyz` (decrypted; see §1) |

  The `User-Agent` + `Accept` come from `NewRequestWithDefaultHeaders` (`http_headers.go`). `X-Debug-Key` is then set via `req.Header.Set` (so default-headers first, then debug key added).
- **Request body:** none.
- **Timeout:** **120 seconds** (`http.Client{Timeout: 120 * time.Second}` set in `NewAmazonDownloader`). This single client timeout covers connect + full body read; in Python apply `timeout=120` to the whole request.

### Response handling
- **Status check:** if `status != 200` → error `f"Amazon API returned status {code}"`. (Only exactly 200 is accepted; not `<400`.)
- Read full body, then `json.Unmarshal` into:
  ```python
  # struct AmazonStreamResponse
  {
    "streamUrl":     str,   # -> StreamURL
    "decryptionKey": str,   # -> DecryptionKey  (hex string)
  }
  ```
  JSON keys probed: **`streamUrl`** and **`decryptionKey`** (exact camelCase). On JSON parse error → `f"failed to decode response: {err}"`.
- **Required:** if `streamUrl == ""` → error `"no stream URL found in response"`.
- `decryptionKey` may be empty — if so, the decrypt/ffmpeg step is **skipped entirely** and the raw downloaded `.m4a` is returned as-is (see §3/§4 — note this means no FLAC detection happens, file stays `.m4a`).

---

## 3. Download the stream → `<ASIN>.m4a`

- `downloadURL = apiResp.streamUrl` (used verbatim, no modification).
- Output filename: `f"{asin}.m4a"`; path = `os.path.join(output_dir, filename)`.
- Create the file, then issue a **second GET** to `downloadURL` using `NewRequestWithDefaultHeaders` again → headers `User-Agent` + `Accept` **only** (NO `X-Debug-Key` on the stream download). Same 120s client.
- Stream the body to disk (`io.Copy` through a `ProgressWriter`). On copy error: close + `os.Remove(filePath)` + return error.
- Go follows redirects by default (`http.Client` default policy, up to 10 hops) — preserve auto-redirect on the stream URL.

---

## 4. Codec detection (ffprobe) — only runs when `decryptionKey != ""`

```python
# ffprobe <path>
[
  ffprobe_path,
  "-v", "quiet",
  "-select_streams", "a:0",
  "-show_entries", "stream=codec_name",
  "-of", "default=noprint_wrappers=1:nokey=1",
  file_path,            # the downloaded <ASIN>.m4a
]
codec = stdout.decode().strip()   # Go: strings.TrimSpace(string(codecOutput))
```
- Run with hidden console window (Windows). Output captured via `.Output()` (stdout only). **Errors are swallowed** (`codecOutput, _ := ...`) — if ffprobe is missing or fails, `codec` stays `""`.
- If `GetFFprobePath()` errors, the probe is skipped and `codec` is left `""`.

### Extension decision rule (CRITICAL — the lossless gate)
```python
target_ext = ".m4a"
if codec == "flac":
    target_ext = ".flac"
```
- `.flac` is chosen **only** when the decrypted/probed codec is exactly the string `"flac"`. Anything else (e.g. `aac`, `alac`, empty) → `.m4a` = lossy. For the real-FLAC backend, a `.m4a` outcome means **"no lossless here → try next service"**.

> NOTE / possible divergence flag: ffprobe runs on the **still-encrypted** `<ASIN>.m4a` *before* ffmpeg decryption. CENC encryption only encrypts sample payloads, not the codec metadata, so `codec_name` is readable pre-decrypt. The goal-file phrasing "detect codec of decrypted file" is functionally right but the probe is literally on the pre-decrypt container.

---

## 5. ffmpeg decryption (CENC, stream-copy, no re-encode)

### Decrypted temp filename construction (reproduce exactly)
```python
file_name = f"{asin}.m4a"
decrypted_filename = "dec_" + file_name + target_ext       # base case (e.g. "dec_B0X.m4a.m4a")
if target_ext == ".flac" and file_name.endswith(".m4a"):
    decrypted_filename = "dec_" + file_name[:-4] + ".flac"  # strips ".m4a" -> "dec_B0X.flac"
decrypted_path = os.path.join(output_dir, decrypted_filename)
```
> Quirk: in the **non-FLAC** path the temp name is the doubled-extension `dec_<ASIN>.m4a.m4a` (Go: `"dec_" + fileName + targetExt`). This is intentional in the Go and you must reproduce it (it only ever exists transiently before the rename in §5c).

### 5a. ffmpeg preflight
- `GetFFmpegPath()` — if error → `f"ffmpeg not found for decryption: {err}"`.
- `ValidateExecutable(ffmpegPath)` — if error → `f"invalid ffmpeg executable: {err}"`.

### 5b. ffmpeg command (EXACT arg order)
```python
key = apiResp.decryptionKey.strip()    # Go: strings.TrimSpace
cmd = [
    ffmpeg_path,
    "-decryption_key", key,            # hex string from response, trimmed
    "-i", file_path,                   # the downloaded <ASIN>.m4a
    "-c", "copy",                      # stream-copy: NO re-encode
    "-y",                              # overwrite output
    decrypted_path,
]
# run with hidden window; capture CombinedOutput (stdout+stderr merged)
```
- On non-zero exit: take the **last 500 bytes** of combined output (`outStr[len-500:]` if longer) → error `f"ffmpeg decryption failed: {err}\nTail Output: {tail}"`.
- `-decryption_key` is the AES-128 CENC content key (hex). It is the per-track key from the proxy (`decryptionKey`), NOT the debug key from §1.

### 5c. Post-decrypt validation + finalization
1. `os.Stat(decrypted_path)` — if missing OR size == 0 → error `"decrypted file missing or empty"`.
2. `os.Remove(file_path)` (delete the encrypted `<ASIN>.m4a`). Failure is non-fatal (warning printed).
3. Final path = `os.path.join(output_dir, decrypted_filename.removeprefix("dec_"))` — i.e. strip the leading `"dec_"` (Go `strings.TrimPrefix`). So:
   - FLAC: `dec_B0X.flac` → `B0X.flac`
   - non-FLAC: `dec_B0X.m4a.m4a` → `B0X.m4a.m4a`  ← (double ext persists into final; flag this oddity, but it gets renamed later in `DownloadByURL` §6 to the metadata filename so end-users rarely see it).
4. `os.Rename(decrypted_path, final_path)` — failure → `f"failed to rename decrypted file: {err}"`.
5. `filePath = final_path`.

If `decryptionKey == ""`, NONE of §4/§5 runs — the function returns the raw `<ASIN>.m4a` directly.

`DownloadFromService` is a thin alias → calls `DownloadFromAfkarXYZ`.

---

## 6. Outer flow (`DownloadByURL`) — relevant bits for the lossless gate & file naming

This wraps §2–5. Key points the porter needs:

- **Output dir:** `os.MkdirAll(outputDir, 0755)` unless `outputDir == "."`.
- **Pre-existence skip:** if `spotifyTrackName` & `spotifyArtistName` set and redownload-with-suffix is OFF, builds `BuildExpectedFilename(...)` and if that file exists with size>0 → returns `"EXISTS:"+expectedPath` (sentinel, no download).
- **MusicBrainz genre** fetched concurrently (goroutine + channel) only if `embedGenre && spotifyURL != ""`; uses ISRC from SongLink `GetISRC`. Non-fatal on failure.
- Calls `DownloadFromService` (→ §2–5) to get `filePath`.
- **Rename to metadata filename:** applies `filenameFormat`. If format contains `{`, does token replacement (`{title}`,`{artist}`,`{album}`,`{album_artist}`,`{year}`,`{date}`,`{isrc}`,`{disc}`,`{track}`); else switch on `"artist-title"`/`"title"`/default `"title - artist"`, optional `"%02d. "` track prefix.
  - **Extension preserved from `filePath`**: `ext = filepath.Ext(filePath)`; if empty → `.flac`. So a FLAC result keeps `.flac`, an m4a result keeps `.m4a` (or `.m4a.m4a` → `filepath.Ext` returns `.m4a`, so final is `Name.m4a`). The new path = `outputDir/<newFilename><ext>`.
- **Metadata embed:** `EmbedMetadataToConvertedFile(filePath, metadata, coverPath)`. Cover downloaded from `spotifyCoverURL` to `filePath+".cover.jpg"` first (non-fatal).
  - `metadata.Description = "https://github.com/spotbye/SpotiFLAC"` (constant).
  - `metadata.Comment = metadata.URL = spotifyURL`.
- **M4A cleanup when final is FLAC:** if `filePath` ends `.flac` (case-insensitive), it tries to remove `originalFileDir/originalFileBase + ".m4a"` (the pre-rename encrypted leftover). Non-fatal.
- Returns final `filePath`. Prints `"✓ Downloaded successfully from Amazon Music"`.

`DownloadBySpotifyID` = `GetAmazonURLFromSpotify(id)` then `DownloadByURL(...)`.

---

## 7. How the Amazon URL is obtained (`GetAmazonURLFromSpotify`)

- `NewSongLinkClient().GetAllURLsFromSpotify(spotifyTrackID, "")` (odesli/song.link resolver — separate module `songlink.go`).
- `normalizeAmazonMusicURL(urls.AmazonURL)` (from `songlink.go`):
  ```python
  def normalize_amazon_music_url(raw):
      u = raw.strip()
      if not u: return ""
      if "trackAsin=" in u:
          asin = u.split("trackAsin=")[1].split("&")[0]   # Go: Split[1] then Split("&")[0]
          if asin:
              return f"https://music.amazon.com/tracks/{asin}?musicTerritory=US"
      # /albums/<10x[A-Z0-9]>/(B........) capture group 1
      m = re.search(r"/albums/[A-Z0-9]{10}/(B[0-9A-Z]{9})", u)
      if m: return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"
      m = re.search(r"/tracks/(B[0-9A-Z]{9})", u)
      if m: return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"
      return ""
  ```
  Regexes verbatim: `/albums/[A-Z0-9]{10}/(B[0-9A-Z]{9})` and `/tracks/(B[0-9A-Z]{9})`.
- If normalized URL is `""` → error `"amazon Music link not found"`.

---

## 8. Error / fallback summary (for the multi-service orchestrator)

| Condition | Behavior |
|---|---|
| Amazon link not found via SongLink | error → caller tries next service |
| ASIN not extractable | error `failed to extract ASIN...` |
| Debug-key decrypt fails | error (won't happen with given constants) |
| API status ≠ 200 | error `Amazon API returned status N` |
| `streamUrl` empty | error `no stream URL found in response` |
| `decryptionKey` empty | **skips decrypt**, returns raw `.m4a` (lossy) |
| Decrypted codec ≠ `flac` | file ends `.m4a` → **lossy; FLAC-only backend treats as "no lossless, try next"** |
| Decrypted codec == `flac` | `.flac` result → success |
| ffmpeg fails / output empty | error |

There is **no internal retry loop** in `amazon.go` (the `regions` field `["us","eu"]` is set in `NewAmazonDownloader` but **never used** anywhere in this file — flag as dead/unused; do not port behavior for it). No HTTP 400/401 refresh logic, no cache TTL. Single attempt per call.

---

## 9. Unused / divergence flags
- `AmazonDownloader.regions = ["us","eu"]` — **dead field**, never read. Don't implement region cycling.
- Non-FLAC decrypt temp file is the double-extension `dec_<ASIN>.m4a.m4a`, finalized to `<ASIN>.m4a.m4a` inside `DownloadFromAfkarXYZ`, but the outer `DownloadByURL` renames using `filepath.Ext` (`.m4a`) so the user-facing file collapses to a single `.m4a`. Reproduce the intermediate names if you want byte-for-byte temp-file parity; the final user file does not carry the double extension when metadata renaming is active.
- ffprobe runs on the encrypted container (pre-decrypt), relying on CENC leaving `codec_name` readable — matches goal-file intent.
- Debug key uses **seed-parts → sha256** (qobuz-style), and that sha256 input equals the single string `b"spotiflac:amazon:spotbye:api:v1"` — both framings are equivalent and confirmed.