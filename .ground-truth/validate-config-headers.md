# Ground-truth spec: validate-config-headers
Source Go files: download_validation.go, config.go, http_headers.go

I have complete information. Producing the spec.

# Porting Spec: Download Validation, Config / Service Ordering, Shared HTTP Headers

Covers `download_validation.go`, `config.go`, `http_headers.go` (Go package `backend`). All three are pure-logic / no-network except where they read/write the local JSON config file. There are **no HTTP requests** originated in these three files; `http_headers.go` only constructs request objects with shared headers that *other* modules send.

---

## 1. `download_validation.go` — Duration Validation

### 1.1 Constants (copy VERBATIM)

```python
PREVIEW_MAX_SECONDS          = 35     # previewMaxSeconds
PREVIEW_EXPECTED_MIN_SECONDS = 60     # previewExpectedMinSeconds
LARGE_MISMATCH_MIN_EXPECTED  = 90     # largeMismatchMinExpected
MIN_ALLOWED_DURATION_DIFF    = 15     # minAllowedDurationDiff
DURATION_DIFF_RATIO          = 0.25   # durationDiffRatio  (Go: float64)
```

### 1.2 Function `ValidateDownloadedTrackDuration(filePath string, expectedSeconds int) -> (bool, error)`

Returns a 2-tuple `(shouldDelete bool, err error)`. **CRITICAL Go semantics for the port:** the first return value is NOT "is valid". It is effectively a "the caller should act on `err`" / "file-was-handled" flag. Reproduce the exact return tuples below; do not invert.

Exact control flow (line-by-line):

```python
def validate_downloaded_track_duration(file_path: str, expected_seconds: int) -> tuple[bool, Exception | None]:
    # 1. Guard: empty path or non-positive expected -> (False, None)  [NOT an error, NOT delete]
    if file_path == "" or expected_seconds <= 0:
        return (False, None)

    # 2. Probe actual duration (float seconds). See GetAudioDuration below.
    try:
        actual_duration = get_audio_duration(file_path)   # float64 seconds
    except Exception:
        # Go: err != nil  -> (False, None)
        return (False, None)
    if actual_duration <= 0:                               # Go: actualDuration <= 0
        return (False, None)

    # 3. Round to nearest int (Go math.Round = round-half-away-from-zero)
    actual_seconds = int(math_round_half_away_from_zero(actual_duration))
    if actual_seconds <= 0:
        return (False, None)

    # 4. PREVIEW / SAMPLE DETECTION
    #    expectedSeconds >= 60  AND  actualSeconds <= 35
    if expected_seconds >= PREVIEW_EXPECTED_MIN_SECONDS and actual_seconds <= PREVIEW_MAX_SECONDS:
        return (True, Exception(
            f"detected preview/sample download: file is {actual_seconds}s, "
            f"expected about {expected_seconds}s. file was removed"))

    # 5. WRONG-RECORDING / LARGE MISMATCH DETECTION
    #    Only checked when expectedSeconds >= 90
    if expected_seconds >= LARGE_MISMATCH_MIN_EXPECTED:
        # allowedDiff = max(15, round(0.25 * expected))   -- both args coerced via float64 then int()
        allowed_diff = int(max(
            float(MIN_ALLOWED_DURATION_DIFF),
            math_round_half_away_from_zero(float(expected_seconds) * DURATION_DIFF_RATIO),
        ))
        diff = int(abs(float(actual_seconds - expected_seconds)))   # = abs(actual - expected)
        if diff > allowed_diff:                                     # STRICTLY greater than
            return (True, Exception(
                f"downloaded file duration mismatch: file is {actual_seconds}s, "
                f"expected about {expected_seconds}s. file was removed"))

    # 6. All good
    return (True, None)
```

### 1.3 Exact thresholds restated (for the porting checklist)

- **Preview reject:** trigger when `expected >= 60` **AND** `actual <= 35`. (Both bounds inclusive.)
- **Wrong-recording reject:** only when `expected >= 90`. Compute `allowedDiff = max(15, round(0.25 * expected))`. Reject when `abs(actual - expected) > allowedDiff` (strict `>`, not `>=`).
  - Worked examples (verify your port):
    - `expected=90` → `0.25*90=22.5` → `round=23` → `allowedDiff=max(15,23)=23`. Reject if `|actual-90| > 23` (i.e. actual `<67` or `>113`).
    - `expected=200` → `0.25*200=50` → `allowedDiff=50`. Reject if `|actual-200| > 50`.
    - `expected=100` → `0.25*100=25` → `allowedDiff=25`. Reject if `|actual-100| > 25`.
    - For `expected` in `[90, 120]`, the ratio term may be below/around 15; e.g. `expected=90`→23 (ratio wins). `0.25*expected` only drops to 15 at `expected=60`, which never enters this branch, so the ratio term always dominates the floor of 15 within this branch (`expected>=90` ⇒ `round(0.25*expected) >= 23 > 15`). The `max(15, …)` floor is therefore effectively dead for all reachable inputs — **but keep it verbatim** for byte-for-byte parity.

### 1.4 Go quirks to preserve

- **`math.Round`** rounds half **away from zero** (Go semantics). Python's built-in `round()` is banker's rounding (round-half-to-even) and will diverge on `*.5` values (e.g. `22.5`). Implement `math_round_half_away_from_zero(x) = math.floor(x + 0.5)` for `x >= 0` (all values here are non-negative durations/ratios), or use `decimal`/explicit logic. Do **not** use bare `round()`.
- `int(...)` in Go truncates toward zero after the `math.Round`/`math.Max`/`math.Abs` float ops; since inputs are non-negative here, `int(float)` == truncation == the rounded integer. Replicate by `int(math.trunc(...))` semantics, but since the values are already integral after rounding, plain `int()` is fine.
- Error messages are **verbatim** (used in logs/UI). Preserve exact wording, the `%ds`/`%ds` substitutions, the period, and the trailing `. file was removed`.
- The function itself does **not** delete the file. Deletion is the caller's responsibility (signaled by the `(True, err)` tuple where `err != nil`). The message text says "file was removed" but removal happens elsewhere — flag this for the Python caller: when this returns `(True, err)` with non-nil err, the caller must `os.remove(file_path)`.

### 1.5 Dependency `GetAudioDuration(filePath) -> float64` (from `metadata.go`, for context)

Returns duration in **float seconds**. Logic:
1. If extension (lowercased) is `.flac`: try `getFlacDuration` → parse FLAC STREAMINFO metadata block (first meta block, `data` len ≥ 18): `sampleRate = data[10]<<12 | data[11]<<4 | data[12]>>4`; `totalSamples = (data[13]&0x0F)<<32 | data[14]<<24 | data[15]<<16 | data[16]<<8 | data[17]`; return `totalSamples / sampleRate` if `sampleRate > 0`. If that fails or returns `<= 0`, fall through.
2. Fallback `getDurationWithFFprobe`: runs `ffprobe -v quiet -print_format json -show_format <file>`, parses JSON `{"format":{"duration":"<string>"}}`, `strconv.ParseFloat` of that string. Empty string → error.

For the validation port, treat `get_audio_duration` as: "best-effort float seconds; raises/returns error if undeterminable." Any error or `<= 0` short-circuits validation to `(False, None)` (no deletion).

---

## 2. `config.go` — Settings Sanitization & Service Ordering

This module reads/writes a local JSON file `config.json` in the app dir. No network. All keys are camelCase JSON keys in the persisted config.

### 2.1 Constants & external constants used

```python
LEGACY_TIDAL_API_CACHE_FILE = "tidal-api-urls.json"   # legacyTidalAPICacheFile

# Defined in link_resolver.go, referenced here:
LINK_RESOLVER_PROVIDER_SONGSTATS         = "songstats"        # linkResolverProviderSongstats
LINK_RESOLVER_PROVIDER_DEEZER_SONGLINK   = "deezer-songlink"  # linkResolverProviderDeezerSongLink
```

Config file is `<appDir>/config.json`; written with `json.MarshalIndent(…, "", "  ")` (2-space indent) and file mode `0o644`. `EnsureAppDir()` / `GetConfigPath()` define `<appDir>` (platform-specific, not in these files).

### 2.2 `normalizeCustomTidalAPIValue(value) -> str` — tidal_api_url normalization

This is the **`customTidalApi` / tidal_api_url** normalization. Exact steps:

```python
def normalize_custom_tidal_api_value(value) -> str:
    # Go: customAPI, _ := value.(string)  -> non-string yields ""
    custom_api = value if isinstance(value, str) else ""
    # strings.TrimRight(strings.TrimSpace(customAPI), "/")
    #   1. TrimSpace: strip leading+trailing Unicode whitespace
    #   2. TrimRight by cutset "/": strip ALL trailing '/' chars
    custom_api = custom_api.strip()           # Go TrimSpace strips Unicode whitespace
    custom_api = custom_api.rstrip("/")        # TrimRight cutset = "/" (only the '/' char)
    # require https:// prefix, else discard
    if custom_api.startswith("https://"):
        return custom_api
    return ""
```

Quirks:
- Order is **TrimSpace first, then TrimRight("/")**. A value like `"https://x.com/  "` → TrimSpace→`"https://x.com/"` → TrimRight→`"https://x.com"`. A value like `"https://x.com/ "` (trailing space after slash): TrimSpace removes the trailing space → `"https://x.com/"` → `"https://x.com"`.
- `TrimRight(s, "/")` removes **every** trailing `/`, e.g. `"https://x.com///"` → `"https://x.com"`.
- Must start with literal `https://` (lowercase, exact). `http://`, `HTTPS://`, bare host, or anything else → returns `""` (discarded). Note: `Go strings.HasPrefix` is case-sensitive — do **not** lowercase before the prefix check.
- Non-string JSON value (number/bool/null/object) → `""`.

### 2.3 `sanitizeDownloaderValue(value, allowTidal) -> str` — `downloader` key (selected service)

```python
def sanitize_downloader_value(value, allow_tidal: bool) -> str:
    downloader = value if isinstance(value, str) else ""
    key = downloader.strip().lower()           # TrimSpace + ToLower
    if key == "tidal":
        return "tidal" if allow_tidal else "auto"
    if key == "qobuz":
        return "qobuz"
    if key == "amazon":
        return "amazon"
    return "auto"                              # default / unknown / "auto" / ""
```

- Valid outputs: `"tidal"`, `"qobuz"`, `"amazon"`, `"auto"`.
- `"tidal"` is downgraded to `"auto"` unless `allowTidal` is true (i.e. unless a valid custom Tidal API is configured).
- Anything unrecognized (including `"auto"`, empty, non-string) → `"auto"`.

### 2.4 `sanitizeAutoOrderValue(value, allowTidal) -> str` — `autoOrder` key (auto service order)

This implements the **default service order and auto-ordering rule**.

```python
def sanitize_auto_order_value(value, allow_tidal: bool) -> str:
    auto_order = value if isinstance(value, str) else ""

    # allowed set + fallback depend on allowTidal
    allowed = {"qobuz", "amazon"}
    fallback = "qobuz-amazon"
    if allow_tidal:
        allowed = {"qobuz", "amazon", "tidal"}
        fallback = "tidal-qobuz-amazon"

    seen = set()
    parts = []
    # Split on "-" AFTER TrimSpace+ToLower of the whole string
    for raw_part in auto_order.strip().lower().split("-"):
        part = raw_part.strip()        # per-part TrimSpace
        if part == "":
            continue
        if part not in allowed:        # drop unknown tokens
            continue
        if part in seen:               # dedupe, keep first occurrence order
            continue
        seen.add(part)
        parts.append(part)

    if len(parts) < 2:                 # need at least 2 valid services
        return fallback

    return "-".join(parts)
```

Key behaviors (CRITICAL — this is the service-ordering core):
- **Default order WITHOUT custom Tidal:** `"qobuz-amazon"` (Qobuz → Amazon). `allowed = {qobuz, amazon}`; `tidal` is NOT allowed and is silently dropped from any saved order.
- **Default order WITH custom Tidal configured (`allowTidal==True`):** fallback becomes `"tidal-qobuz-amazon"` (Tidal → Qobuz → Amazon), and `tidal` becomes a permitted token. **Tidal is only ever prepended/included when a valid custom Tidal instance is configured.**
- The returned string preserves the **user's token order** (first-seen order after dedup), not a fixed order — e.g. with `allowTidal`, input `"amazon-tidal-qobuz"` → `"amazon-tidal-qobuz"`. Only when fewer than 2 valid tokens survive does it reset to the fallback default.
- Tokenization: lowercase + trim the whole string, split on `-`, then trim each token. Empty tokens (from leading/trailing/double `-`) are skipped. Unknown tokens (e.g. `"deezer"`, or `"tidal"` when `!allowTidal`) are dropped. Duplicates dropped (first wins).
- **Edge:** if after filtering only 0 or 1 valid token remains → fallback default. E.g. (`!allowTidal`) input `"tidal-qobuz"` → tidal dropped → only `["qobuz"]` → `len<2` → returns `"qobuz-amazon"`. Input `"qobuz-amazon-tidal"` (`!allowTidal`) → `["qobuz","amazon"]` → `"qobuz-amazon"`.
- Go `strings.Split("", "-")` returns `[""]` (one empty element), which is skipped → empty/missing autoOrder → fallback. Reproduce: Python `"".split("-")` also yields `[""]`. (Both fine.)

### 2.5 `SanitizeSettingsMap(settings) -> map` — orchestration & cross-field dependency

```python
def sanitize_settings_map(settings: dict | None) -> dict | None:
    if settings is None:
        return None
    # shallow copy preserving ALL original keys/values
    sanitized = dict(settings)

    # 1. Normalize tidal API first — determines allowTidal
    custom_api = normalize_custom_tidal_api_value(sanitized.get("customTidalApi"))
    sanitized["customTidalApi"] = custom_api
    allow_tidal = custom_api != ""

    # 2. downloader and autoOrder depend on allowTidal
    sanitized["downloader"] = sanitize_downloader_value(sanitized.get("downloader"), allow_tidal)
    sanitized["autoOrder"]  = sanitize_auto_order_value(sanitized.get("autoOrder"), allow_tidal)

    return sanitized
```

- **Dependency ordering matters:** `customTidalApi` must be normalized **before** `downloader`/`autoOrder`, because `allowTidal = (normalized customTidalApi != "")` gates whether `tidal` is a valid downloader / auto-order token.
- All other keys pass through **unchanged** (shallow copy). Only `customTidalApi`, `downloader`, `autoOrder` are rewritten.
- `Go map[string]interface{}` ↔ Python `dict`. The `.get(key)` on a missing key returns `None` in Python, matching Go's zero-value `interface{}(nil)` from absent map keys; `normalize/sanitize` helpers all treat non-string (incl. `None`) as `""`/default.

### 2.6 Other config getters (all read `config.json` via `LoadConfigSettings`)

`LoadConfigSettings()` → returns `None` if config file does not exist (`os.IsNotExist`); else reads file, `json.Unmarshal` into `map[string]interface{}`, returns `SanitizeSettingsMap(settings)`. Any read/parse error is returned to caller (getters below treat error as "use default").

| Go function | JSON key | Default when missing/err/non-string | Allowed normalized values |
|---|---|---|---|
| `GetRedownloadWithSuffixSetting() bool` | `redownloadWithSuffix` | `false` | bool; non-bool→`false` |
| `GetCustomTidalAPISetting() string` | `customTidalApi` | `""` | `normalizeCustomTidalAPIValue` applied again |
| `GetExistingFileCheckModeSetting() string` | `existingFileCheckMode` | `"filename"` | `"isrc"` if (lower/trim) in {`isrc`,`upc`}, else `"filename"` |
| `GetLinkResolverSetting() string` | `linkResolver` | `"deezer-songlink"` | see below |
| `GetLinkResolverAllowFallback() bool` | `allowResolverFallback` | `true` | bool; missing/non-bool→`true` |

`normalizeExistingFileCheckMode(value)`:
```python
def normalize_existing_file_check_mode(value: str) -> str:
    return "isrc" if value.strip().lower() in ("isrc", "upc") else "filename"
```
Note: both `isrc` AND `upc` map to the SAME output `"isrc"`; everything else → `"filename"`.

`GetLinkResolverSetting()` switch on `resolver.strip().lower()`:
```python
def get_link_resolver_setting(resolver: str | None) -> str:
    if resolver is None or not isinstance(resolver, str):
        resolver = ""   # Go: type-assert default ""
    key = resolver.strip().lower()
    if key in ("songlink", "deezer-songlink"):   # both map to deezer-songlink
        return "deezer-songlink"
    if key == "songstats":
        return "songstats"
    if key == "":
        return "deezer-songlink"
    # default (anything else):
    return "deezer-songlink"
```
- `"songlink"` is an alias for `"deezer-songlink"`. Only `"songstats"` returns `"songstats"`. Every other value (incl. empty, unknown) → `"deezer-songlink"` (default resolver).

`GetLinkResolverAllowFallback()`: reads `allowResolverFallback`; the Go code uses `value, ok := settings[key].(bool)` — if not present or not a bool, `ok==false` → returns `true`. If present and bool, returns that bool. Default-on.

### 2.7 `SanitizePersistedConfigSettings() -> error`

Re-writes config.json in place with sanitized values:
1. `GetConfigPath()` → path; if file does not exist (`os.IsNotExist`) → return nil (no-op).
2. Read file → `json.Unmarshal` into `map[string]interface{}` (error → return error).
3. `sanitized = SanitizeSettingsMap(settings)`.
4. `payload = json.MarshalIndent(sanitized, "", "  ")` (2-space indent, keys sorted — Go's `encoding/json` **sorts map keys alphabetically** on marshal; Python: `json.dumps(sanitized, indent=2, sort_keys=True)` to match byte-for-byte, plus trailing-newline parity check — Go `MarshalIndent` does **not** append a trailing newline, and `os.WriteFile` writes bytes as-is, so do NOT add a trailing newline).
5. `os.WriteFile(configPath, payload, 0o644)`.

**Go map-key sort quirk:** `encoding/json.Marshal` of a `map` emits keys in **sorted (lexical byte) order**. To reproduce the on-disk file byte-for-byte, serialize with sorted keys. Also Go escapes `<`, `>`, `&` as `\u003c`,`\u003e`,`\u0026` by default in `encoding/json` (HTML escaping ON) — if any string values can contain those chars (e.g. a URL with `&`), Python must replicate: `json.dumps(..., ensure_ascii=False)` does NOT escape them, so emulate Go by post-escaping `<>&` to `\u003c/\u003e/\u0026`, OR accept divergence and flag it. For typical config (URLs without `&`, plain ASCII), `json.dumps(sanitized, indent=2, sort_keys=True)` matches.

### 2.8 `CleanupLegacyTidalPublicAPIState() -> error`

- `appDir = EnsureAppDir()`; `cachePath = appDir/"tidal-api-urls.json"`.
- `os.Remove(cachePath)`; if error and NOT `os.ErrNotExist` → return error; missing file is OK (no error).

### 2.9 `GetDefaultMusicPath() -> str`

```python
def get_default_music_path() -> str:
    home = user_home_dir()   # os.UserHomeDir()
    if home is None:         # Go: err != nil
        return "C:\\Users\\Public\\Music"   # literal Windows fallback path
    return os.path.join(home, "Music")       # filepath.Join(home, "Music")
```
- Fallback path verbatim: `C:\Users\Public\Music` (Go source escapes backslashes; the actual string is `C:\Users\Public\Music`).
- `filepath.Join` uses OS separator. On the target (Windows per fallback) → `\`; on the dev mac it'd be `/`. Match host OS.

---

## 3. `http_headers.go` — Shared Default Headers

### 3.1 Constant (copy VERBATIM)

```python
DEFAULT_DOWNLOADER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
# Single-line exact value:
# "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
```
Note the Chrome major version is **146** (`Chrome/146.0.0.0`).

### 3.2 `NewRequestWithDefaultHeaders(method, rawURL, body) -> request`

Constructs (does NOT send) an HTTP request with two shared headers:

```python
def new_request_with_default_headers(method: str, raw_url: str, body=None):
    # equivalent to http.NewRequest(method, raw_url, body)
    req = build_request(method, raw_url, body)
    # exact header names (canonical casing) + values:
    req.headers["User-Agent"] = DEFAULT_DOWNLOADER_USER_AGENT
    req.headers["Accept"]     = "application/json, text/plain, */*"
    return req
```

Exact headers set (casing + value verbatim):
- `User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36`
- `Accept: application/json, text/plain, */*`

Notes:
- `http.Header.Set` canonicalizes header names to `Title-Case` (`User-Agent`, `Accept`) — already canonical here. In Python `requests`/`httpx`, set the headers with this exact casing.
- No timeout is set here — timeout is the caller's `http.Client` concern; this function only builds the `*http.Request`. Do not invent a timeout.
- No body/content-type defaults; `Content-Type` is NOT set by this helper.
- This is the shared base used by other services (Qobuz/Amazon/Tidal/resolver modules) that may add their own headers on top.

---

## 4. Divergence flags / gotchas for the Python engineer

1. **`ValidateDownloadedTrackDuration` return value is not "isValid".** First tuple element is `True` whenever an actual measurable duration was obtained (even on a "good" file). The deletion signal is `err != nil`. Goal-file shorthand like "returns false to reject" would be WRONG — rejection is `(True, <error>)`, acceptance is `(True, None)`, and inability-to-validate is `(False, None)`. The actual file deletion is performed by the **caller**, not this function.
2. **`math.Round` ≠ Python `round`.** Use round-half-away-from-zero (`math.floor(x+0.5)` for non-negative). Critical at `0.25*expected` producing `*.5` (e.g. expected=90→22.5→23, not 22).
3. **`max(15, …)` floor is effectively dead** for all reachable inputs (`expected>=90` ⇒ ratio≥23). Keep it anyway.
4. **Tidal gating is purely derived** from `customTidalApi` being a non-empty `https://…` string after normalization. There is no separate "enable Tidal" flag. `downloader=="tidal"` and any `tidal` token in `autoOrder` are silently stripped/downgraded unless that condition holds.
5. **`autoOrder` preserves user token order**, it does not force `tidal-qobuz-amazon` — that fixed order is only the *fallback default* when <2 valid tokens survive.
6. **`https://` prefix check is case-sensitive** and requires exactly `https://` (no `http://`, no uppercase). TrimSpace happens before TrimRight("/").
7. **Config write byte-parity:** Go marshals maps with **sorted keys**, 2-space indent, **no trailing newline**, and HTML-escapes `<>&`. Use `json.dumps(obj, indent=2, sort_keys=True)` and emulate `<>&` escaping if such chars can appear; otherwise it matches.
8. **`existingFileCheckMode`:** both `"isrc"` and `"upc"` normalize to `"isrc"`; anything else → `"filename"`.
9. **`linkResolver`:** `"songlink"` is an alias of `"deezer-songlink"`; only `"songstats"` yields songstats; default/unknown/empty → `"deezer-songlink"`.
10. **`allowResolverFallback` defaults to `true`** when absent or non-bool (uses Go comma-ok idiom, not just falsy check).
11. **`GetDefaultMusicPath` fallback** is the literal Windows path `C:\Users\Public\Music`; the success path is `<home>/Music` joined with OS separator.
12. **User-Agent Chrome version is `146`** (not 120/124/etc.) and **`Accept`** is exactly `application/json, text/plain, */*`. No `Content-Type`, no `Accept-Encoding`/`Accept-Language` set by this shared helper.

### Source file paths
- `SpotiFLAC/backend/download_validation.go`
- `SpotiFLAC/backend/config.go`
- `SpotiFLAC/backend/http_headers.go`
- Dependencies referenced: `SpotiFLAC/backend/metadata.go` (`GetAudioDuration`, `getFlacDuration`, `getDurationWithFFprobe`), `SpotiFLAC/backend/link_resolver.go` (resolver provider constants).