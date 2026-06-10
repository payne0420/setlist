# Ground-truth spec: tidal
Source Go files: tidal.go (read in full — ~30KB; paginate)

No Tidal search/catalog endpoint exists in this file. I have all the information needed for the complete spec.

# Tidal Downloader — Porting Spec (`tidal.go`)

Source: `SpotiFLAC/backend/tidal.go` (read in full). Helpers from `http_headers.go` and `config.go`.

## 0. Constants (verbatim)

```python
# from http_headers.go
DEFAULT_DOWNLOADER_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

# Default request headers applied to EVERY HTTP call in this module
# (NewRequestWithDefaultHeaders — exact casing):
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_DOWNLOADER_USER_AGENT,
    "Accept": "application/json, text/plain, */*",
}

# Description tag embedded in metadata (finalize step):
METADATA_DESCRIPTION = "https://github.com/spotbye/SpotiFLAC"
```

There are **no app-ids, secret strings, signing keys, or crypto** in this module. There is **no Tidal search / catalog endpoint** anywhere in `tidal.go` — extended-mode catalog search is NOT implemented here (the only catalog-resolution is via SongLink in `songlink.go`/`songstats.go`, out of scope). Do not invent one.

## 1. Instance configuration (`GetCustomTidalAPISetting` / `getConfiguredTidalAPIAttemptList`)

The Tidal "user-instance" base URL comes entirely from user config key `customTidalApi`. Normalization (`normalizeCustomTidalAPIValue`):

```python
def normalize_custom_tidal_api(value):
    s = (value or "")
    s = s.strip().rstrip("/")          # Go: strings.TrimRight(strings.TrimSpace(v), "/")
    if s.startswith("https://"):       # MUST be https:// (http:// is rejected → "")
        return s
    return ""
```

- `getConfiguredTidalAPIAttemptList()` returns `[customAPI]` if non-empty, else error `"no configured custom tidal api instance"`. The "rotating APIs" path is a list of length **0 or 1** — there is effectively only one instance. (`legacyTidalAPICacheFile = "tidal-api-urls.json"` is dead/legacy; not used for resolution.)
- `NewTidalDownloader(apiURL)` does `apiURL = strings.TrimRight(strings.TrimSpace(apiURL), "/")` again, and sets `client.Timeout = 5s`, `timeout = 5s`, `maxRetries = 3` (maxRetries is unused), `apiURL`.

## 2. URL / track-ID extraction

- `GetTrackIDFromURL(tidalURL)`: split on the literal `"/track/"`. If `< 2` parts → error `"invalid tidal URL format"`. Take `parts[1]`, split on `"?"`, take `[0]`, `strip()`. Parse as int64 via `fmt.Sscanf("%d")` (leading integer; Go Sscanf stops at first non-digit). On parse failure → error. `trackID == 0` is treated as "no track ID found" by callers.

## 3. Fetch download URL — `GetDownloadURL(trackID, quality)` (the core HTTP call)

**Request:**
- Method: `GET`
- URL (Go `fmt.Sprintf`): `{apiURL}/track/?id={trackID}&quality={quality}`
  - `trackID` formatted with `%d` (plain decimal int64).
  - `quality` inserted **raw, NOT URL-encoded** (values are `LOSSLESS` / `HI_RES` / `HI_RES_LOSSLESS`, all URL-safe). Reproduce as raw f-string, do NOT urlencode.
  - Example: `https://my.instance/track/?id=12345678&quality=LOSSLESS`
- Headers: `DEFAULT_HEADERS` (User-Agent + Accept above). No others.
- Body: none.
- Timeout: **5 seconds** (`t.client.Timeout`).
- Redirects: Go default `http.Client` follows up to 10 redirects automatically — replicate by allowing redirects (e.g. requests default `allow_redirects=True`).

**Pre-check:** if `apiURL.strip() == ""` → error `"no configured custom tidal api instance"` (returned before any HTTP).

**Response handling (in this exact order):**
1. If `resp.status_code != 200` → error `"API returned status code: {code}"`. (Only 200 is accepted; no retry/refresh logic on 400/401 here.)
2. Read full body bytes.
3. **Try v2 object shape first** — `json.Unmarshal(body, &TidalAPIResponseV2)`. If it unmarshals AND `data.manifest != ""` → return the sentinel string `"MANIFEST:" + data.manifest` (the base64 manifest, prefixed). Print `"✓ Tidal manifest found (v2 API)"`.
   - v2 object keys: `version`, and `data` object with keys `trackId`(int64), `assetPresentation`, `audioMode`, `audioQuality`, `manifestMimeType`, `manifestHash`, `manifest`(string base64), `bitDepth`(int), `sampleRate`(int). Only `data.manifest` is load-bearing.
   - **Go quirk:** `json.Unmarshal` into a struct succeeds even when the JSON is an object missing these keys (zero-values). It FAILS if the top-level JSON is an array. So the v2 branch is taken only for object responses that carry a non-empty `data.manifest`. In Python: parse JSON once; if it is a `dict` and `dict.get("data",{}).get("manifest")` is a non-empty string → manifest path.
4. **Else try legacy array shape** — `json.Unmarshal(body, &[]TidalAPIResponse)`. Element key probed: `"OriginalTrackUrl"` (exact JSON casing — capital O,T,U). On unmarshal error → error `"failed to decode response: {err} (response: {first 200 chars of body}...)"`.
   - If array empty → error `"no download URL in response"`.
   - Iterate items in order; return the first item whose `OriginalTrackUrl != ""`. Print `"✓ Tidal download URL found"`.
   - If none non-empty → error `"download URL not found in response"`.

Return value is EITHER a sentinel `"MANIFEST:<base64>"` OR a direct https URL string.

## 4. Dispatch — `DownloadFile(url, filepath, quality)`

- If `url.startswith("MANIFEST:")` → strip prefix, call `DownloadFromManifest(rest, filepath, quality)`.
- Else: plain `GET url` with `DEFAULT_HEADERS`, **5s timeout** (this uses `t.client`, the 5s client). Reject non-200 (`"download failed with status {code}"`). Stream body to `filepath` (the legacy direct FLAC URL case). No transcode here — file is written as-is (already `.flac` per `outputFilename`).

## 5. Quality fallback ordering

Two callers wire fallback:

`isTidalHiResQuality(quality)`:
```python
def is_tidal_hires_quality(q):
    n = q.strip().upper()
    return n == "HI_RES" or n == "HI_RES_LOSSLESS"
```

- `DownloadByURL`: call `GetDownloadURL(trackID, quality)`. On error, **if `is_tidal_hires_quality(quality)` and `allowFallback`** → retry `GetDownloadURL(trackID, "LOSSLESS")`. Else propagate error. (Fallback only triggers for HI_RES/HI_RES_LOSSLESS requests; a plain `LOSSLESS` request never falls back.)
- `DownloadByURLWithFallback` → `downloadWithRotatingAPIs`: builds `qualities = [quality]`, and **if `is_tidal_hires_quality(quality)` and `allowFallback`** appends `"LOSSLESS"`. Tries each quality in order across the (single) configured API via `tryDownloadAcrossTidalAPIs`. First success wins.
- Note: the fallback's added quality is always the literal `"LOSSLESS"` (never `HI_RES` from `HI_RES_LOSSLESS`).

## 6. Manifest parsing — `parseManifest(manifestB64)` → `(directURL, initURL, mediaURLs, mimeType)`

Step 1: `base64.StdEncoding.DecodeString` (standard alphabet, `+/`, padding required). On error → `"failed to decode manifest"`.

Step 2: decode bytes to string. **Branch on `manifestStr.strip().startswith("{")`:**

### 6a. BTS JSON branch (`urls[0]` is direct FLAC)
- `json.Unmarshal` into `TidalBTSManifest` keys: `mimeType`, `codecs`, `encryptionType`, `urls` (array of strings). On error → `"failed to parse BTS manifest"`.
- If `len(urls) == 0` → `"no URLs in BTS manifest"`.
- Print `"Manifest: BTS format ({mimeType}, {codecs})"`.
- Return `directURL = urls[0]`, `initURL = ""`, `mediaURLs = None`, `mimeType = btsManifest.mimeType`. (Codecs not returned.)

### 6b. DASH MPD XML branch (else)
Print `"Manifest: DASH format"`. Parse with the XML model:

- `MPD → Period → AdaptationSet[]`, each with attrs `mimeType`, `codecs`, an optional child `SegmentTemplate`, and `Representation[]` (attrs `id`, `codecs`, `bandwidth`(int), optional child `SegmentTemplate`).
- `SegmentTemplate` attrs: `initialization`, `media`; child `SegmentTimeline` with `S[]` elements having attrs `d`(int64 duration) and `r`(int repeat).

**Representation selection (exact Go logic):**
```
segTemplate = None
selectedBandwidth = 0
selectedCodecs = ""; selectedMimeType = ""
for as in adaptationSets:
    if as.SegmentTemplate is not None and segTemplate is None:
        segTemplate = as.SegmentTemplate
        selectedCodecs = as.codecs
        selectedMimeType = as.mimeType
    for rep in as.representations:
        if rep.SegmentTemplate is not None and rep.bandwidth > selectedBandwidth:
            selectedBandwidth = rep.bandwidth
            segTemplate = rep.SegmentTemplate
            selectedCodecs = rep.codecs if rep.codecs != "" else as.codecs
            selectedMimeType = as.mimeType
```
- So: an AdaptationSet-level SegmentTemplate is used as initial default (only if no rep wins). Among Representations that have a SegmentTemplate, pick the **highest `bandwidth`** (strict `>`; first-seen wins ties). `selectedBandwidth` starts at 0, so any rep with bandwidth>0 replaces an AdaptationSet-level default.
- If `selectedBandwidth > 0`: `dashMimeType = f'{selectedMimeType}; codecs="{selectedCodecs}"'` (literal `; codecs="..."`, with the quotes). Print `Selected stream: Codec=..., Bandwidth=... bps`. If no rep had positive bandwidth, `dashMimeType` stays `""`.

**Segment count + URL build (XML path):**
```
if segTemplate is not None:
    initURL = segTemplate.initialization
    mediaTemplate = segTemplate.media
    segmentCount = sum(seg.r + 1 for seg in segTemplate.timeline.S)   # Σ(r+1)
```
If `segmentCount > 0 and initURL != "" and mediaTemplate != ""`:
- **Unescape `&amp;` → `&`** in BOTH `initURL` and `mediaTemplate` (Go `strings.ReplaceAll`, only this entity — do NOT full HTML-unescape). Note: Go's XML decoder already unescaped `&amp;` once when populating attrs; this ReplaceAll handles any literal `&amp;` that survived (e.g. double-escaped). For a faithful port: take the attribute value as the XML parser yields it, then additionally do `.replace("&amp;", "&")`.
- Print `Parsed manifest via XML: {segmentCount} segments`.
- Build `mediaURLs = [ mediaTemplate.replace("$Number$", str(i)) for i in range(1, segmentCount+1) ]` (i = 1..count inclusive).
- Return `directURL="", initURL, mediaURLs, dashMimeType`.

**Regex fallback (XML path didn't yield usable segments):**
Print `"Using regex fallback for DASH manifest..."`. Operate on the **raw `manifestStr`** (pre-XML-unescape). Patterns (verbatim):
```python
INIT_RE   = r'initialization="([^"]+)"'
MEDIA_RE  = r'media="([^"]+)"'
SEG_TAG_RE = r'<S\s+[^>]*>'
R_RE      = r'r="(\d+)"'
```
- `initURL = INIT_RE.search(...).group(1)` (first match) else stays `""`; same for `mediaTemplate` via `MEDIA_RE`.
- If `initURL == ""` → error `"no initialization URL found in manifest"`.
- Then `.replace("&amp;", "&")` on both `initURL` and `mediaTemplate`.
- `segmentCount = 0`; for each `<S ...>` tag matched by `SEG_TAG_RE` (findall): extract `r="(\d+)"` if present (`repeat`, else 0); `segmentCount += repeat + 1`.
- If `segmentCount == 0` → error `"no segments found in manifest (XML: {len(matches)}, Regex: 0)"`.
- Print `Parsed manifest via Regex: {segmentCount} segments`.
- Build `mediaURLs` with `$Number$` → 1..count as above. Return `"", initURL, mediaURLs, dashMimeType`.
  - **Quirk:** in the regex fallback, `dashMimeType` is whatever the XML selection set earlier (often `""` if XML parse failed) — it is NOT recomputed from regex.

## 7. Lossless guard — `DownloadFromManifest(manifestB64, outputPath, quality)`

After `parseManifest`:
```python
isLosslessRequested = quality in ("LOSSLESS", "HI_RES", "HI_RES_LOSSLESS")   # exact ==, case-sensitive
isActualLossless    = ("flac" in mimeType.lower()) or (mimeType == "")
if isLosslessRequested and not isActualLossless:
    raise Error(f"requested {quality} quality but Tidal provided lossy format ({mimeType}). Aborting download")
```
- `quality` comparison is **case-sensitive exact** (Go `==`), unlike `isTidalHiResQuality` which upper-cases. Pass the quality through unchanged.
- Empty `mimeType` (`""`) counts as lossless (passes the guard) — this is the DASH case where `dashMimeType` ended up `""`.

## 8. Download paths inside `DownloadFromManifest`

A **separate `http.Client` with 120-second timeout** is created here for all manifest segment/direct downloads (`doRequest` uses `DEFAULT_HEADERS`). Note: this differs from the 5s client used in `GetDownloadURL`/plain `DownloadFile`.

**Path A — direct FLAC (BTS or any direct URL whose mime is flac/empty):**
- Condition: `directURL != "" and ("flac" in mimeType.lower() or mimeType == "")`.
- GET directURL (120s), reject non-200 (`"download failed with status {code}"`), stream body straight to `outputPath` (the `.flac` path). **No ffmpeg, no remux.** Print progress; `"Download complete"`. Return.

**Path B — direct non-FLAC URL:**
- `directURL != ""` but mime is lossy → write raw stream to `tempPath = outputPath + ".m4a.tmp"`. (This path only reachable when `isLosslessRequested` was false, since the guard would have aborted lossy otherwise.) On write error: remove temp.

**Path C — DASH segmented (`directURL == ""`):**
- `tempPath = outputPath + ".m4a.tmp"`; create it.
- Print `"Downloading {len(mediaURLs)+1} segments..."`.
- **Download `initURL` FIRST**, GET (120s); on transport error or non-200 → close+remove temp, error (`"failed to download init segment"` / `"init segment download failed with status {code}"`). Append raw body to temp. Print `"OK"`.
- Then iterate `mediaURLs` in order (1..count); for each: GET, reject non-200 (`"segment {i+1} download failed with status {code}"`), append raw body to the SAME temp file (raw byte concatenation, init then segment 1, 2, …). Track bytes; update `SetDownloadSpeed`/`SetDownloadProgress`; print `\rDownloading: {MB} MB ({i+1}/{total} segments)`. Any error → close+remove temp, error.
- Close temp; print final size.

**Remux/transcode (Paths B and C, after temp written):**
- Print `"Converting to FLAC..."`. Resolve ffmpeg via `GetFFmpegPath()` + `ValidateExecutable`.
- Command (verbatim argv): `ffmpeg -y -i {tempPath} -vn -c:a flac {outputPath}`.
  - **IMPORTANT divergence from the goal-file's "`-c copy` remux" description:** there is **NO `-c copy` branch** in this code. Every DASH/non-direct path is **always transcoded with `-c:a flac`**, regardless of whether the source codec is already FLAC. The `-c copy` remux and the "`-c:a flac` only for non-lossless" distinction described in the assignment **do not exist in this Go file** — flag this as a divergence to the Python engineer. The only "copy"-style fast path is Path A (direct FLAC URL written byte-for-byte with no ffmpeg at all).
- On ffmpeg failure: rename temp to `outputPath.removesuffix(".flac") + ".m4a"` (Go `strings.TrimSuffix`) and error `"ffmpeg conversion failed (M4A saved as {m4aPath}): {err} - {stderr}"`.
- On success: `os.remove(tempPath)`; print `"Download complete"`.
- ffmpeg stderr captured; window hidden (`setHideWindow`, Windows-only no-op elsewhere).

## 9. Cleanup / artifacts

`cleanupTidalDownloadArtifacts(outputPath)`: if non-empty, remove `outputPath` and `outputPath + ".m4a.tmp"` (ignore errors). Called on any download failure in the by-URL flows.

## 10. Filename build — `buildTidalFilename` (for output path)

- `numberToUse = position`; if `useAlbumTrackNumber and trackNumber > 0` → `numberToUse = trackNumber`.
- `year = releaseDate[:4]` if `len >= 4` else `""`.
- If `format` contains `"{"` → template mode, replace tokens (all `strings.ReplaceAll`, replace-all): `{title}`,`{artist}`,`{album}`,`{album_artist}`,`{year}`,`{date}`(→`SanitizeFilename(releaseDate)`),`{isrc}`(→`SanitizeOptionalFilename(extra[0])`). `{disc}` → `str(discNumber)` if `discNumber>0` else `""`. `{track}` → `"%02d" % numberToUse` if `numberToUse>0`; else strip `{track}` with these regexes in order: `r'\{track\}\.\s*'`, `r'\{track\}\s*-\s*'`, `r'\{track\}\s*'` (replace-all, empty).
- Else preset: `"artist-title"`→`f"{artist} - {title}"`; `"title"`→`title`; default→`f"{title} - {artist}"`. Then if `includeTrackNumber and position > 0` → prefix `f"{numberToUse:02d}. "`.
- Always append `".flac"`.
- Output path = `os.path.join(outputDir, filename)`, then `ResolveOutputPathForDownload(..., GetRedownloadWithSuffixSetting())` for de-dup/suffix and the `alreadyExists` check. If exists → returns sentinel `"EXISTS:" + outputFilename` from the by-URL flows.

## 11. Quirks checklist for the porter

- v2 detection MUST be "parse JSON, is it a dict with non-empty `data.manifest`" — an array body fails the v2 struct unmarshal in Go and falls through to the legacy `OriginalTrackUrl` path.
- Legacy key is exactly `OriginalTrackUrl` (PascalCase). First non-empty in array order wins.
- `quality` is passed raw into the URL (not encoded) and compared case-sensitively in the lossless guard, but case-insensitively in `isTidalHiResQuality`.
- Two distinct timeouts: 5s for the metadata/`/track/` call and plain direct `DownloadFile`; 120s for manifest segment/direct downloads.
- `&amp;`→`&` is a literal single-entity replace, applied after the XML parse and (separately) in the regex path on the raw string. Do not full-HTML-unescape (no `&lt;`, `&quot;`, etc.).
- Segment numbering substitutes `$Number$` (literal) with 1..count; init segment is downloaded once before media segments and concatenated first.
- DASH output is ALWAYS ffmpeg-transcoded `-c:a flac` (no `-c copy`); only the direct-FLAC URL path skips ffmpeg. (Divergence from assignment description — flagged.)
- No `tidal` search/catalog endpoint, no app-id, no crypto/signature anywhere in this file.