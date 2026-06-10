# Ground-truth spec: qobuz-sign-frontends
Source Go files: qobuz.go (read in full — it is ~33KB; paginate)

I have all worked examples and constants verified. Let me confirm one subtle Go detail: in `newQobuzSignedRequestWithCredentials`, the signature is computed from the **original** `params` (not the cloned one that has app_id/request_ts/request_sig added). I'll verify that reading and also confirm `url.Values.Encode()` URL-encodes the final query string (sorted by key). Everything is confirmed. Now writing the spec.

# Qobuz Signed API + 3 CDN Frontends — Byte-for-Byte Porting Spec

Source files: `qobuz.go`, `qobuz_api.go`, `provider_endpoints.go`, `qobuz_providers.go`, `provider_priority.go`, `http_headers.go` (all under `SpotiFLAC/backend/`).

This spec covers the signed metadata API (signature + credentials), the three download frontends (WJHE, GDStudio, MusicDL), the shared stream-URL extractor, candidate scoring, and provider ordering/fallback.

---

## 0. Verbatim constants

```python
# --- Signed metadata API (qobuz_api.go) ---
QOBUZ_API_BASE_URL                  = "https://www.qobuz.com/api.json/0.2"
QOBUZ_DEFAULT_API_APP_ID            = "712109809"
QOBUZ_DEFAULT_API_APP_SECRET        = "589be88e4538daea11f509d29e4a23b1"
QOBUZ_CREDENTIALS_CACHE_FILE        = "qobuz-api-credentials.json"
QOBUZ_CREDENTIALS_CACHE_TTL_SECONDS = 24 * 3600          # 24h
QOBUZ_CREDENTIALS_PROBE_TRACK_ISRC  = "USUM71703861"
QOBUZ_OPEN_TRACK_PROBE_URL          = "https://open.qobuz.com/track/1"

# UA used for signed API + credential scraping == DefaultDownloaderUserAgent
DEFAULT_DOWNLOADER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

# --- Frontend endpoints (provider_endpoints.go) ---
QOBUZ_WJHE_BASE_URL            = "https://music.wjhe.top"
QOBUZ_WJHE_SEARCH_API_URL      = QOBUZ_WJHE_BASE_URL + "/api/music/qobuz/search"   # not used in download path
QOBUZ_WJHE_STREAM_API_URL      = QOBUZ_WJHE_BASE_URL + "/api/music/qobuz/url"
QOBUZ_MUSICDL_DOWNLOAD_API_URL = "https://www.musicdl.me/api/qobuz/download"
QOBUZ_GDSTUDIO_API_URL_XYZ     = "https://music.gdstudio.xyz/api.php"   # primary
QOBUZ_GDSTUDIO_API_URL_ORG     = "https://music.gdstudio.org/api.php"   # fallback
QOBUZ_GDSTUDIO_VERSION         = "2026.5.10"

# Default provider URL order (provider_endpoints.go) — used only to size a slice; real order is computed
QOBUZ_DEFAULT_DOWNLOAD_PROVIDER_URLS = [
    QOBUZ_WJHE_STREAM_API_URL,
    QOBUZ_GDSTUDIO_API_URL_XYZ,
    QOBUZ_GDSTUDIO_API_URL_ORG,
    QOBUZ_MUSICDL_DOWNLOAD_API_URL,
]

# Probe track id used by health checks (qobuz.go)
QOBUZ_PROBE_TRACK_ID = 341032040

# Default request headers applied by NewRequestWithDefaultHeaders (http_headers.go):
#   User-Agent: <DEFAULT_DOWNLOADER_USER_AGENT>
#   Accept: application/json, text/plain, */*
```

`open.qobuz.com` track URL builder (`qobuz.go`): `buildQobuzOpenTrackURL(id)` → `"https://open.qobuz.com/track/%d" % id` (Go `%d` = plain base-10, no thousands sep).

---

## 1. The MD5 request signature (signed metadata API)

Functions: `qobuzNormalizedPath`, `qobuzSignaturePayload`, `qobuzRequestSignature`, `newQobuzSignedRequestWithCredentials` (qobuz_api.go).

### 1.1 normalizedPath
```python
def qobuz_normalized_path(path: str) -> str:
    # Go: strings.Trim(strings.TrimSpace(path), "/")
    return path.strip().strip("/")    # trims whitespace, then leading/trailing "/"
```
For the **signature payload** ONLY, a second transform removes ALL slashes:
```python
normalized_path_for_sig = qobuz_normalized_path(path).replace("/", "")
# "track/search" -> "tracksearch" ; "track/get" -> "trackget"
```
Note: the URL path uses `qobuz_normalized_path(path)` (slashes KEPT: `track/search`); the signature uses the all-slashes-removed form (`tracksearch`). Do not confuse the two.

### 1.2 Payload construction (qobuzSignaturePayload)
Built from the **original** `params` (the caller's params, NOT including `app_id`/`request_ts`/`request_sig` — those are added to a separate cloned map after signing):

```python
def qobuz_signature_payload(path, params: dict[str, list[str]], timestamp: str, secret: str) -> str:
    normalized = qobuz_normalized_path(path).replace("/", "")
    # exclude these three keys, then sort remaining keys ascending (Go sort.Strings = byte/lexicographic on UTF-8)
    keys = sorted(k for k in params if k not in ("app_id", "request_ts", "request_sig"))
    s = normalized
    for k in keys:
        vals = params[k]
        if len(vals) == 0:
            s += k                 # key with no values -> append key only
        else:
            for v in vals:         # multi-value: repeat "key"+"value" for each value in slice order
                s += k + v
    s += timestamp                 # request_ts (unix seconds, base-10)
    s += secret                    # app_secret
    return s
```
Important quirks:
- Keys sorted ascending; values concatenated **without separators** as `keyvalue`.
- A key may have multiple values — emit `key+value` for EACH value (insertion order within that key's slice).
- `timestamp` = `str(int(time.time()))` (unix **seconds**), the SAME value also set as the `request_ts` query param.
- `app_secret` appended raw at the end.

### 1.3 request_sig
```python
import hashlib
request_sig = hashlib.md5(qobuz_signature_payload(...).encode()).hexdigest()  # lowercase hex
```

### 1.4 Worked examples (verified)
- `track/search` with `query=USUM71703861`, `limit=1`, `ts=1700000000`, secret=`589be88e4538daea11f509d29e4a23b1`:
  - payload = `tracksearchlimit1queryUSUM717038611700000000589be88e4538daea11f509d29e4a23b1`
    (note key sort: `limit` < `query`)
  - request_sig = `80f86ad720695063422aaf47c563934c`
- `track/get` with `track_id=123456`, `ts=1700000000`:
  - payload = `trackgettrack_id1234561700000000589be88e4538daea11f509d29e4a23b1`
  - request_sig = `b28ecba4d3d542c33ba2c1549b42a3e2`

---

## 2. The signed request (URL + headers)

`newQobuzSignedRequestWithCredentials` (qobuz_api.go):

1. `normalized = qobuz_normalized_path(path)` (slashes kept). Empty → error `"qobuz request path is empty"`.
2. Require `creds.AppID` and `creds.AppSecret` non-empty (after `TrimSpace`) else error `"qobuz credentials are incomplete"`.
3. Clone params (deep copy of multi-valued map).
4. `timestamp = str(int(time.time()))`.
5. On the **clone** set: `app_id = creds.AppID`, `request_ts = timestamp`, `request_sig = qobuz_request_signature(normalized, ORIGINAL_params, timestamp, creds.AppSecret)`.
   - CRITICAL: signature is computed from the ORIGINAL params (without app_id/request_ts/request_sig); the clone is only what gets encoded into the URL.
6. URL: `f"{QOBUZ_API_BASE_URL}/{normalized}?{urlencode(clone)}"`.
   - Go `url.Values.Encode()`: keys sorted ascending, each `key=value` URL-escaped (`QueryEscape`: space→`+`, etc.), joined by `&`. Reproduce with `urllib.parse.urlencode(sorted_items, doseq=True)` ensuring keys are sorted (`urlencode` does not sort by default — sort keys yourself, and for multi-value preserve slice order). Practically all signed calls here use single-value params.

Headers (exact casing):
```
User-Agent: <DEFAULT_DOWNLOADER_USER_AGENT>
Accept: application/json
X-App-Id: <creds.AppID>
```
Method: caller-supplied (`GET` for both `track/get` and `track/search`). Body: none.

### 2.1 Transport / timeouts / refresh
- `doQobuzSignedRequest(method, path, params, client)`: if `client is None`, use timeout 20s. Make the call with `getQobuzAPICredentials(forceRefresh=False)`. If response status is **400 or 401** (`qobuzShouldRefreshCredentials`), close body and retry ONCE with `forceRefresh=True`. Return whatever the second call yields.
- `doQobuzSignedJSONRequest(path, params, target)`: always `GET`, fresh `http.Client{Timeout:20s}`. Non-200 → error `"qobuz request failed: HTTP %d: %s"` with body limited to 2048 bytes (trimmed). Else JSON-decode body into target.

### 2.2 Credentials acquisition (`getQobuzAPICredentials`) — full algorithm
Mutex-guarded. Order:
1. If `not forceRefresh` and in-memory cached creds are **fresh** (TTL 24h, `FetchedAtUnix != 0`, app_id+secret non-empty) → return them.
2. Load disk cache from `<FFmpegDir>/qobuz-api-credentials.json` (JSON `{app_id, app_secret, source?, fetched_at_unix}`). If `not forceRefresh` and disk cache fresh → cache in memory + return.
3. Scrape live creds (`scrapeQobuzOpenCredentials`, 30s client):
   - GET `https://open.qobuz.com/track/1` with `User-Agent: <DEFAULT_DOWNLOADER_USER_AGENT>`. Non-200 → error.
   - In HTML, find bundle script via regex (verbatim): `<script[^>]+src="([^"]+/js/main\.js|/resources/[^"]+/js/main\.js)"`. If match starts with `/`, prefix `https://open.qobuz.com`.
   - GET bundle (same UA). In bundle JS, find creds via regex (verbatim): `app_id:"(?P<app_id>\d{9})",app_secret:"(?P<app_secret>[a-f0-9]{32})"`. app_id = exactly 9 digits, app_secret = 32 lowercase-hex chars.
   - Validate scraped creds via `track/search?query=USUM71703861&limit=1` signed request; require HTTP 200 AND `tracks.total > 0`.
   - On success: cache in memory, write disk cache (`MarshalIndent` 2-space, mode 0644, dir 0755), return.
4. On scrape/validation failure, fall back in order: disk cache (even if stale) → in-memory (even if stale) → embedded default (`app_id=712109809`, `app_secret=589be88e4538daea11f509d29e4a23b1`, source `"embedded-default"`).

A Python port MAY skip scraping and just use the embedded default app_id/app_secret (that is the guaranteed fallback). But if you replicate the cache file, use the same shape and TTL.

---

## 3. searchByISRC + scoring

`searchByISRC(isrc, spotifyTrackName, spotifyArtistName, spotifyAlbumName)` (qobuz.go):

### 3.1 Direct-by-ID shortcut
If `isrc` starts with literal prefix `"qobuz_"`:
- `trackID = isrc[len("qobuz_"):].strip()`.
- `doQobuzSignedRequest("GET", "track/get", {"track_id":[trackID]}, q.client)`. (Uses `q.client`, 60s default — see §9.)
- Non-200 → error `"Qobuz public API track/get returned status %d: %s"` (body ≤512 read, previewed to 256).
- Else JSON-decode body into one `QobuzTrack`; return it.

### 3.2 ISRC + secondary query search
- Build `queries = [isrc.strip()]`.
- Secondary fallback query = `strings.Join([spotifyTrackName, spotifyArtistName], " ").strip()` i.e. `f"{trackName} {artistName}".strip()` — appended only if non-empty.
- For each non-blank query (in order): `doQobuzSignedJSONRequest("track/search", {"query":[query.strip()], "limit":["10"]}, &searchResp)`.
  - On error → record `lastErr`, continue.
  - If `tracks.total == 0` OR `len(items) == 0` → `lastErr = "track not found for query: %s"`, continue.
  - Else pick best by `scoreQobuzSearchCandidate`. Tie/selection rule: iterate items; `bestIndex=0`; for each idx, if `idx==0 or score > bestScore` → set best. (So index 0 always seeds; later items only win on strictly-greater score — first max wins.) Return the selected item immediately (does NOT try the secondary query if the first query yielded any items).
- If all queries exhausted: return `lastErr` or `"track not found for ISRC: %s"`.

### 3.3 normalizeQobuzSearchValue (verbatim replacer)
```python
def normalize_qobuz_search_value(value: str) -> str:
    # Go: lower+trim FIRST, then a single-pass strings.NewReplacer over these pairs (longest-match,
    # non-overlapping, left-to-right), then collapse whitespace via strings.Fields join.
    normalized = value.strip().lower()
    # strings.NewReplacer replacements (order as given; Replacer is single-pass, non-overlapping):
    #   "&"     -> " and "
    #   "feat." -> " "
    #   "ft."   -> " "
    #   "/"     -> " "
    #   "-"     -> " "
    #   "_"     -> " "
    # Note: lowercasing happens BEFORE the replacer, so "Feat."/"FT." already lowercased to "feat."/"ft.".
    normalized = _go_replacer(normalized, [("&"," and "),("feat."," "),("ft."," "),("/"," "),("-"," "),("_"," ")])
    return " ".join(normalized.split())   # strings.Fields: split on any Unicode whitespace, drop empties
```
IMPORTANT — Go `strings.NewReplacer` semantics (not naive sequential `str.replace`): it scans once; at each position it picks the FIRST pattern in the given list that matches (at that position), replaces it, and resumes AFTER the replacement (no re-scanning of inserted text). For these disjoint single-byte/short patterns this is equivalent to a single left-to-right longest-among-listed match. Implement a position-scanning replacer, NOT chained `.replace()` (chaining can double-apply, e.g. an inserted `" and "` is safe here but get the semantics right for correctness).

### 3.4 Scoring (`scoreQobuzSearchCandidate`)
```python
score = 0
# TITLE: spotifyTrackName vs track.Title
tN = norm(spotifyTrackName); tH = norm(track.Title)
if tN != "" and tH == tN:                       score += 1000
elif tN != "" and (tN in tH or tH in tN):       score += 500
# ARTIST: spotifyArtistName vs display artist (= performer.name OR album.artist.name, first non-empty trimmed)
aN = norm(spotifyArtistName); aH = norm(qobuz_display_artist(track))
if aN != "" and aH == aN:                                  score += 300
elif aN != "" and aH != "" and (aN in aH or aH in aN):     score += 180
# ALBUM: spotifyAlbumName vs track.Album.Title
bN = norm(spotifyAlbumName); bH = norm(track.Album.Title)
if bN != "" and bH == bN:                                  score += 150
elif bN != "" and bH != "" and (bN in bH or bH in bN):     score += 90
# HI-RES bonus
if qobuz_supports_hires(track):     score += 40
elif track.MaximumBitDepth >= 16:   score += 20
```
Where:
- `qobuz_display_artist(track)` = first non-empty of (`track.Performer.Name`, `track.Album.Artist.Name`) after `TrimSpace` (`firstNonEmptyQobuzValue`).
- `qobuz_supports_hires(track)`: `True` if `track.Hires or track.HiresStreamable` else `track.MaximumBitDepth >= 24 or track.MaximumSamplingRate > 48`.
- `Contains` checks: title uses raw `in` both directions; artist & album additionally require the haystack be non-empty (`aH != ""`/`bH != ""`) before the `in` test.

`QobuzTrack` JSON keys you must read for scoring/metadata: `id` (int64), `title`, `version`, `duration`, `track_number`, `media_number`, `isrc`, `copyright`, `maximum_bit_depth` (int), `maximum_sampling_rate` (float), `hires` (bool), `hires_streamable` (bool), `release_date_original`, `performer.{name,id}`, `album.{title,id,image.{small,thumbnail,large},artist.{name,id},label.name}`.

`track/search` response shape (`qobuzPublicSearchResponse`): `{"tracks":{"total":int,"items":[QobuzTrack,...]}}`.

---

## 4. Quality codes + 27→7→6 fallback

`GetDownloadURL(trackID, quality, allowFallback)` (qobuz.go):
- Normalize requested quality: `qualityCode = quality`; if `qualityCode in ("", "5")` → `qualityCode = "6"`.
- `downloadFunc(qual)` runs all frontends in priority order (see §8) and returns first success.
- Run `downloadFunc(qualityCode)`. If success → return.
- Fallback chain (only if `allowFallback` true), each step a fresh full run of `downloadFunc`:
  - If `currentQuality == "27"`: try `downloadFunc("7")`; on success return; else `currentQuality = "7"`.
  - If `currentQuality == "7"`: try `downloadFunc("6")`; on success return.
- If everything fails: error `"all APIs and fallbacks failed. Last error: %v"`.
- Quality semantics: `27` = Hi-Res ≥24-bit up to 192kHz; `7` = 24-bit standard; `6` = 16-bit lossless. The fallback ladder is strictly 27→7→6 (a requested `6` never escalates; a requested `7` only falls to `6`).

---

## 5. WJHE frontend (`DownloadFromWJHE`)

### 5.1 URL + params (`buildQobuzWJHEDownloadURL`)
```python
def map_wjhe_quality(quality: str):           # mapQobuzWJHEQuality
    q = quality.strip()
    if q in ("27", "7"):  return (2000, "flac")
    if q in ("", "6"):    return (1000, "flac")
    return (320, "mp3")

wjhe_quality, wjhe_format = map_wjhe_quality(quality)
params = {"ID": str(track_id), "quality": str(wjhe_quality), "format": wjhe_format}
url = QOBUZ_WJHE_STREAM_API_URL + "?" + urlencode_sorted(params)
# Go url.Values.Encode() sorts keys -> "ID", "format", "quality" (uppercase 'I'(0x49) < 'f','q')
# => .../api/music/qobuz/url?ID=<id>&format=<fmt>&quality=<n>
```
Param casing is exact: `ID` (uppercase), `quality`, `format` (lowercase). Encode with sorted keys to match Go.

### 5.2 Request flow (no-redirect client)
- Client: `newQobuzNoRedirectClient(q.client)` — clones base client, forces timeout 20s if unset, and `CheckRedirect → http.ErrUseLastResponse` (i.e. DO NOT follow redirects; capture the 3xx response and its `Location` header). In Python: `requests` with `allow_redirects=False`, or httpx `follow_redirects=False`, timeout 20s.
- Send `HEAD` to `url` with default headers (UA + `Accept: application/json, text/plain, */*`).
- If status is **405 (Method Not Allowed)** or **501 (Not Implemented)** → close, re-send as `GET` (same URL, same default headers).
- Stream-URL extraction order:
  1. `Location` header, trimmed; if `qobuz_url_looks_streamable(location)` → return it.
  2. Read body (limit 128*1024 bytes = 131072) and `extractQobuzStreamingURL(body)` (see §7); if non-empty → return.
  3. If `resp.Request.URL` differs from `apiURL` and is streamable → return final URL string (handles redirect-followed cases; here redirects aren't followed, but final URL after no-redirect equals request URL — still checked).
  4. If status < 200 or ≥ 400 → error `"WJHE returned status %d: %s"` (body preview 256).
  5. Else error `"WJHE response did not include a stream URL"`.

`qobuz_url_looks_streamable(raw)`: trim; parse URL; True iff scheme in (`http`,`https`) AND host non-empty.

---

## 6. GDStudio frontend (`DownloadFromGDStudio`)

### 6.1 Endpoint selection
- `apiURL` arg trimmed; if empty → `GetQobuzGDStudioPrimaryAPIURL()` = `https://music.gdstudio.xyz/api.php`. The `.org` URL is tried as a separate provider attempt (§8), not an in-function fallback.
- `signatureHost = host(apiURL)` via `url.Parse` (e.g. `music.gdstudio.xyz`). Empty host → error `"GDStudio API URL is invalid: %s"`.

### 6.2 ts9 (`getQobuzGDStudioTS9`)
```python
def get_gdstudio_ts9(api_url, client):
    fallback = str(int(time.time() * 1000))     # unix MILLIS, base-10
    if len(fallback) >= 9: fallback = fallback[:9]
    host = host_of(api_url)
    if not host: return fallback
    # GET https://<host>/time  with default headers, client (60s default, or 10s if client None)
    try:
        resp = client.get(f"https://{host}/time", headers=default_headers, timeout=...)
        body = resp.content[:64]                 # io.LimitReader 64 bytes
        ts = body.decode().strip()
        if len(ts) >= 9: return ts[:9]
        return fallback
    except Exception:
        return fallback
```
ts9 = first 9 chars of the `/time` response (trimmed); on any failure (request error, short body, read error) use first 9 chars of local unix-millis.

### 6.3 paddedVersion + escaped value + signature
```python
def gdstudio_padded_version():                   # qobuzGDStudioPaddedVersion
    parts = QOBUZ_GDSTUDIO_VERSION.split(".")    # "2026.5.10" -> ["2026","5","10"]
    parts = [(("0"+p.strip()) if len(p.strip())==1 else p.strip()) for p in parts]
    return "".join(parts)                        # -> "20260510"

def gdstudio_escaped_value(value):               # qobuzGDStudioEscapedValue
    # Go: url.QueryEscape(trim(value)) then ReplaceAll("+","%20")
    return urllib.parse.quote_plus(value.strip()).replace("+", "%20")
    # numeric id -> unchanged ("341032040"); space -> "%20"

def build_gdstudio_signature(api_url, value, ts9):
    host = host_of(api_url)
    base = f"{host}|{gdstudio_padded_version()}|{ts9}|{gdstudio_escaped_value(value)}"
    digest = hashlib.md5(base.encode()).hexdigest()   # lowercase hex
    return digest[-8:].upper()                         # UPPER of last 8 hex chars
```
Worked example (verified): host=`music.gdstudio.xyz`, paddedVersion=`20260510`, ts9=`172000000`, value(id)=`341032040`
→ base = `music.gdstudio.xyz|20260510|172000000|341032040`
→ md5 = `acb528f620cd6e28e7751d47810aeda3` → `s` = `810AEDA3`.

### 6.4 POST request
```python
track_id_str = str(track_id)
ts9 = get_gdstudio_ts9(api_url, q.client)
def map_gdstudio_bitrate(quality):               # mapQobuzGDStudioBitrate
    q = quality.strip()
    if q in ("27","7"): return "999"
    if q in ("","6"):   return "740"
    return "320"
form = {
    "types":  "url",
    "id":     track_id_str,
    "source": "qobuz",
    "br":     map_gdstudio_bitrate(quality),
    "s":      build_gdstudio_signature(api_url, track_id_str, ts9),
}
body = urlencode_sorted(form)   # Go url.Values.Encode(): keys sorted -> br,id,s,source,types
```
Headers (default headers PLUS these, exact casing/values):
```
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Origin: https://<signatureHost>
Referer: https://<signatureHost>/        # trailing slash
User-Agent: <DEFAULT_DOWNLOADER_USER_AGENT>
Accept: application/json, text/plain, */*
```
- POST to `apiURL`, using `q.client` (60s default; follows redirects — NOT the no-redirect client). Body read limit 256*1024 = 262144.
- Non-200 → error `"GDStudio returned status %d: %s"` (preview 256).
- `extractQobuzStreamingURL(body)` (§7); empty → error `"GDStudio response did not include a stream URL: %s"`.

---

## 7. Shared stream-URL extraction (`extractQobuzStreamingURL`)

Regex (verbatim): `qobuzStreamingURLPattern = regexp.MustCompile(`https?://[^\s"'<>\\)]+`)`
In Python: `re.compile(r'''https?://[^\s"'<>\\)]+''')` (char class excludes whitespace, `"`, `'`, `<`, `>`, backslash, `)`).

Algorithm (order matters):
1. `trimmed = body.decode().strip()`; if empty → `""`.
2. **Typed parse** (`directResp`): JSON-unmarshal into `{url, download_url, data:{url, download_url}}`. If decode succeeds, test these in EXACT order and return first streamable:
   `directResp.download_url`, `directResp.url`, `directResp.data.download_url`, `directResp.data.url`.
   (Use `qobuz_url_looks_streamable`.)
3. **Generic recursive walk** (`findQobuzStreamingURLInPayload`) over `json.loads(body)`:
   - string: `candidate = trimmed.replace(r"\/", "/")`; if streamable → return.
   - list: recurse items in order; first hit wins.
   - dict: FIRST probe these keys in EXACT order — `["download_url","url","play_url","stream_url","link","file"]` — recursing into each present value; THEN (if none matched) iterate ALL remaining values (Go ranges a map → **non-deterministic order**; for a deterministic port, after the priority keys, iterate remaining keys — order is not guaranteed by Go, so prefer the priority list to disambiguate; if multiple non-priority URL values exist, Go could pick any. Flag: this is a real Go nondeterminism point).
4. **JSONP unwrap**: if `"("` present at `openIdx>=0` and a `")"` at `closeIdx > openIdx+1`, take `trimmed[openIdx+1:closeIdx].strip()` and RECURSE `extractQobuzStreamingURL` on it. (Uses first `(` and LAST `)`.)
5. **Regex fallback**: `findall` the pattern over `trimmed`; for each match `candidate = match.replace(r"\/", "/")`; first streamable → return.
6. Else `""`.

Note the `\/` → `/` un-escaping (literal backslash-slash) is applied to BOTH JSON string candidates and regex matches.

---

## 8. MusicDL frontend (`DownloadFromMusicDL`)

### 8.1 The AES-GCM debug key (decrypt-on-first-use)
All byte arrays VERBATIM (Python). Key derivation: `SHA256(seed_part0 || seed_part1 || seed_part2)` → 32-byte AES key; AES-256-GCM decrypt `(ciphertext||tag)` with the given 12-byte nonce and AAD.
```python
QOBUZ_MUSICDL_DEBUG_KEY_SEED_PARTS = [
    bytes([0x73,0x70,0x6f,0x74,0x69,0x66]),                                   # "spotif"
    bytes([0x6c,0x61,0x63,0x3a,0x71,0x6f]),                                   # "lac:qo"
    bytes([0x62,0x75,0x7a,0x3a,0x6d,0x75,0x73,0x69,0x63,0x64,0x6c,0x3a,0x76,0x31]),  # "buz:musicdl:v1"
]
QOBUZ_MUSICDL_DEBUG_KEY_AAD = bytes([
    0x71,0x6f,0x62,0x75,0x7a,0x7c,0x6d,0x75,0x73,0x69,0x63,0x64,
    0x6c,0x7c,0x64,0x65,0x62,0x75,0x67,0x7c,0x76,0x31,
])  # "qobuz|musicdl|debug|v1"
QOBUZ_MUSICDL_DEBUG_KEY_NONCE = bytes([
    0x91,0x2a,0x5c,0x77,0x0f,0x33,0xa8,0x14,0x62,0x9d,0xce,0x41,
])
QOBUZ_MUSICDL_DEBUG_KEY_CIPHERTEXT = bytes([
    0xf3,0x4a,0x83,0x45,0x24,0xb6,0x22,0xaf,0xd6,0xc3,0x6e,0x2d,
    0x56,0xd1,0xbb,0x0b,0xe9,0x1b,0x4f,0x1c,0x5f,0x41,0x55,0xc2,
    0xc6,0xdf,0xad,0x21,0x58,0xfe,0xd5,0xb8,0x2d,0x29,0xf9,0x9e,
    0x6f,0xd6,
])
QOBUZ_MUSICDL_DEBUG_KEY_TAG = bytes([
    0x69,0x0c,0x42,0x70,0x14,0x83,0xff,0x14,0xc8,0xbe,0x17,0x00,
    0x69,0xb1,0xfe,0xbb,
])

def get_musicdl_debug_key() -> str:
    import hashlib
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key = hashlib.sha256(b"".join(QOBUZ_MUSICDL_DEBUG_KEY_SEED_PARTS)).digest()
    pt = AESGCM(key).decrypt(
        QOBUZ_MUSICDL_DEBUG_KEY_NONCE,
        QOBUZ_MUSICDL_DEBUG_KEY_CIPHERTEXT + QOBUZ_MUSICDL_DEBUG_KEY_TAG,
        QOBUZ_MUSICDL_DEBUG_KEY_AAD,
    )
    return pt.decode()
```
DECRYPTED VALUE (verified — you may hardcode this if you prefer not to ship the crypto):
- SHA256 key (hex): `849b4cdb4bcec33ab406f7dd4d852ecaa3162cbc37b542a22a1c383f8b597d38`
- Plaintext debug key = **`ryzmicisgoatedandnothingcomesevenclose`** (38 bytes)

Go caches this via `sync.Once`; decrypt once and reuse.

### 8.2 Request
- If `quality.strip() == ""` → `quality = "6"`.
- Body JSON (`qobuzMusicDLRequest`): keys EXACT — `{"url": buildQobuzOpenTrackURL(trackID), "quality": quality.strip()}` i.e. `{"url":"https://open.qobuz.com/track/<id>","quality":"<q>"}`. Go `json.Marshal` field order = struct order: `url` then `quality`.
- POST to `https://www.musicdl.me/api/qobuz/download` with default headers PLUS:
  ```
  Content-Type: application/json
  X-Debug-Key: <decrypted debug key>
  User-Agent: <DEFAULT_DOWNLOADER_USER_AGENT>
  Accept: application/json, text/plain, */*
  ```
  Client: `q.client` (60s default). Body read fully (no limit).
- Non-200 → error `"MusicDL returned status %d: %s"` (preview 256).
- Parse JSON into `qobuzMusicDLResponse` keys: `success` (bool), `type`, `url_type`, `track_id`, `quality_label`, `download_url`, `message`, `error`. Decode failure → error `"failed to decode MusicDL response: %w (%s)"`.
- Require `success == true`; if false, error message = first non-empty of `error`, `message`, else literal `"MusicDL reported failure"`.
- `download_url = response.download_url.strip()`; empty → error `"MusicDL response did not include a download_url"`. Else return it.
- NOTE: MusicDL does NOT use `extractQobuzStreamingURL`; it reads `download_url` directly from the typed response.

---

## 9. Provider ordering, attempts, and the per-call client

### 9.1 Attempt set (`getQobuzDownloadProviders` → `Attempts`)
Provider list order (qobuz_providers.go): **WJHE, GDStudio, MusicDL**. Each provider yields attempts with `ID` = its endpoint URL:
- WJHE → 1 attempt, ID = `QOBUZ_WJHE_STREAM_API_URL`.
- GDStudio → 2 attempts (one per URL from `GetQobuzGDStudioAPIURLs()` = `[xyz, org]`), IDs = the two `api.php` URLs. Each calls `DownloadFromGDStudio(trackID, qual, thatURL)`.
- MusicDL → 1 attempt, ID = `QOBUZ_MUSICDL_DOWNLOAD_API_URL`.

So the raw attemptIDs list = `[wjhe, gdstudio.xyz, gdstudio.org, musicdl]` (a map dedups by ID; here all 4 IDs are distinct).

### 9.2 Ordering
1. `orderedProviderIDs = prioritizeProviders("qobuz", attemptIDs)` — reorders by a bbolt-backed history DB: sort STABLE by (a) last-outcome rank desc (success=2 > unknown/empty=1 > failure=0), (b) `LastSuccess` desc, (c) `LastAttempt` desc, (d) original index asc. If DB unavailable or <2 IDs → returns input order unchanged. For a fresh/no-DB Python port, the effective order is the original: `[wjhe, gdstudio.xyz, gdstudio.org, musicdl]`.
2. `orderedProviderIDs = moveQobuzAttemptIDsLast(orderedProviderIDs, QOBUZ_MUSICDL_DOWNLOAD_API_URL)` — moves the MusicDL ID to the END (stable; preserves relative order of the rest). So MusicDL is ALWAYS tried last regardless of history.
3. Iterate ordered IDs; for each, look up the attempt, print `"Trying Provider: <Name> (Quality: <qual>)..."`, call `attempt.Download()`. On success: record success, return URL. On failure: print, record failure, set `lastErr`, continue. If all fail → return `lastErr`.

The history DB is an optimization; a Python port may omit it and use the fixed order `WJHE → GDStudio(.xyz) → GDStudio(.org) → MusicDL`, which is exactly what an empty DB produces.

### 9.3 The downloader HTTP client
`NewQobuzDownloader().client = http.Client{Timeout: 60s}` (follows redirects by default). This `q.client` is used directly by GDStudio POST, MusicDL POST, GDStudio `/time`, cover download, and the `track/get` shortcut. WJHE wraps it into a no-redirect 20s-min client. Health-check entrypoints (`CheckQobuz*Status*`) build a `QobuzDownloader` with a 4s client and call the respective `DownloadFrom*` with probe id `341032040`, quality `"27"`.

---

## 10. Divergence / quirk flags for the porter

- **Signature input is the ORIGINAL params**, not the params that include `app_id/request_ts/request_sig`. Those three are explicitly excluded in payload building AND are added only to a cloned map used for the URL. Easy to get wrong.
- **normalizedPath differs** between URL (slashes kept: `track/search`) and signature (slashes stripped: `tracksearch`).
- **`request_ts` is unix SECONDS**; **GDStudio ts9 fallback is unix MILLIS** truncated to 9 chars. Don't mix units.
- **GDStudio ts9 from server** is the first 9 chars of `/time` body (server returns a unix value; only 9 chars used) — match exactly.
- **`url.Values.Encode()` sorts keys** — replicate sorted-key encoding for WJHE (`ID,format,quality`) and GDStudio form (`br,id,s,source,types`) and signed API URLs.
- **GDStudio escaped value** uses `QueryEscape`+`+`→`%20` (so spaces become `%20`, not `+`); for the numeric track id it's a no-op but keep the rule for correctness.
- **`strings.NewReplacer` in normalizeQobuzSearchValue is single-pass non-overlapping**, not chained replaces; and lowercasing happens BEFORE replacement.
- **Generic URL walk dict iteration** after the 6 priority keys ranges a Go map (nondeterministic). Priority keys (`download_url,url,play_url,stream_url,link,file`) make this deterministic in practice; flag if a payload has stream URLs only under non-priority keys.
- **WJHE uses HEAD-then-GET-on-405/501 with a no-redirect client** and reads `Location` first; GDStudio/MusicDL are plain POSTs on the redirect-following 60s client.
- **MusicDL reads `download_url` directly** (typed), bypassing `extractQobuzStreamingURL`; the other two use the shared extractor.
- **Fallback ladder is one-directional 27→7→6** and each rung re-runs ALL providers; only triggered when `allowFallback` is true and only from the matching current quality.
- Body read LIMITS differ: WJHE 131072, GDStudio 262144, MusicDL unlimited, `/time` 64, signed-JSON error snippet 2048, track/get error 512, previews truncate to 256.