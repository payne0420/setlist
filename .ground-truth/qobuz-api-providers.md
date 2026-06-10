# Ground-truth spec: qobuz-api-providers
Source Go files: qobuz_api.go, qobuz_providers.go, provider_priority.go, provider_endpoints.go

I now have everything needed. Producing the spec.

# Qobuz API Credentials + Provider Ordering — Porting Spec

Source files: `qobuz_api.go`, `qobuz_providers.go`, `provider_priority.go`, `provider_endpoints.go` (package `backend`).

This spec covers (1) Qobuz API app credential acquisition (embedded fallback, scrape, probe-validate, cache), (2) the request-signing algorithm, (3) the frontend download-provider ordering + adaptive scoreboard, (4) all endpoint host constants.

---

## 0. Shared constants (verbatim)

### App-dir / cache location
- App directory = `os.UserHomeDir()` joined with `.spotiflac` (i.e. `~/.spotiflac`). `GetFFmpegDir()` and `EnsureAppDir()` both return this, creating it with mode `0o755` if missing.
- Qobuz credentials cache file: `~/.spotiflac/qobuz-api-credentials.json` (`qobuzCredentialsCacheFile = "qobuz-api-credentials.json"`).
- Provider-priority DB: `~/.spotiflac/provider_priority.db` (a **bbolt** key/value DB — see §3 for the Python implication).

### User-Agent (used by all Qobuz HTTP calls in this file)
`qobuzDefaultUA = DefaultDownloaderUserAgent`, defined in `http_headers.go`:
```python
QOBUZ_DEFAULT_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
```

### Qobuz API constants (`qobuz_api.go` lines 20–29)
```python
QOBUZ_API_BASE_URL                 = "https://www.qobuz.com/api.json/0.2"
QOBUZ_DEFAULT_API_APP_ID           = "712109809"        # 9 digits
QOBUZ_DEFAULT_API_APP_SECRET       = "589be88e4538daea11f509d29e4a23b1"  # 32 hex chars
QOBUZ_CREDENTIALS_CACHE_FILE       = "qobuz-api-credentials.json"
QOBUZ_CREDENTIALS_CACHE_TTL        = 24 * 3600          # 24h, in seconds
QOBUZ_CREDENTIALS_PROBE_TRACK_ISRC = "USUM71703861"
QOBUZ_OPEN_TRACK_PROBE_URL         = "https://open.qobuz.com/track/1"
```
Note: There is **no `qobuzProbeTrackID` constant** in this code — the probe uses the **ISRC string `USUM71703861`** as a search query (see §1.3). (The assignment mentioned a "qobuzProbeTrackID approach" as an alternative; it does not exist here — only the ISRC search-probe path exists.)

### Regex patterns (verbatim — `qobuz_api.go` lines 34–35)
```python
import re
# bundle <script src=...main.js> finder
QOBUZ_OPEN_BUNDLE_SCRIPT_PATTERN = re.compile(
    r'<script[^>]+src="([^"]+/js/main\.js|/resources/[^"]+/js/main\.js)"'
)
# app_id / app_secret extractor (Go used named groups app_id/app_secret)
QOBUZ_OPEN_API_CONFIG_PATTERN = re.compile(
    r'app_id:"(?P<app_id>\d{9})",app_secret:"(?P<app_secret>[a-f0-9]{32})"'
)
```
Go applies these against the **entire response body as one string** (no `re.DOTALL` needed — `[^>]` / `[^"]` / `\d` / `[a-f0-9]` never need to cross newlines). Use `pattern.search(text)` and read groups `1` and `2`.

### Credential record shape (JSON cache file)
```python
# qobuzAPICredentials, marshalled with json.MarshalIndent(creds, "", "  ")
{
  "app_id": "<str>",
  "app_secret": "<str>",
  "source": "<str, omitempty>",       # omitted from JSON if empty string
  "fetched_at_unix": <int64>          # always present (no omitempty)
}
```
JSON struct tags: `AppID→app_id`, `AppSecret→app_secret`, `Source→source` (`omitempty`), `FetchedAtUnix→fetched_at_unix` (no omitempty → emitted even when 0). Cache file written 2-space-indented, file mode `0o644`, dir mode `0o755`.

---

## 1. Credential acquisition flow

### 1.1 Public entry: `getQobuzAPICredentials(forceRefresh bool)`
Holds a global mutex (`qobuzCredentialsMu`) for the entire body. There is an in-memory cached pointer `qobuzCachedCredentials` (process-global) and an on-disk cache. **Exact ordering** (lines 302–350):

1. **If `not forceRefresh` AND in-memory cache is fresh** → return in-memory creds. (Freshness = §1.2.)
2. Load disk cache (`loadQobuzCachedCredentials`). On read/parse error, print `"Warning: failed to read Qobuz credentials cache: <err>"` and treat as `nil` (do **not** abort).
3. **If `not forceRefresh` AND disk creds are fresh** → set in-memory = disk creds, return them.
4. Otherwise **scrape** (`scrapeQobuzOpenCredentials`) using an `http.Client{Timeout: 30s}`.
   - If scrape **succeeded**: **validate** via signed probe (`qobuzCredentialsSupportSignedMetadata`, §1.3) using the **same 30s client**.
     - If validation passes: set in-memory = scraped, write disk cache (on write error print `"Warning: failed to write Qobuz credentials cache: <err>"` but continue), print `"Loaded fresh Qobuz credentials from <source> (app_id=<app_id>)\n"`, return scraped.
     - If validation fails: set `scrapeErr = "scraped qobuz credentials did not pass validation"` and fall through.
5. Fallback ordering when scrape failed OR validation failed:
   - **If disk creds non-nil** (even if stale): in-memory = disk creds, print `"Warning: failed to refresh Qobuz credentials, using cached credentials: <scrapeErr>"`, return them.
   - **Else if in-memory cache non-nil**: print `"Warning: failed to refresh Qobuz credentials, using in-memory credentials: <scrapeErr>"`, return in-memory.
   - **Else**: build embedded default (`defaultQobuzAPICredentials()`), set in-memory = it, and if `scrapeErr != nil` print `"Warning: failed to refresh Qobuz credentials, using embedded fallback: <scrapeErr>"`, return it.

**Embedded default** (`defaultQobuzAPICredentials`):
```python
{
  "app_id": QOBUZ_DEFAULT_API_APP_ID,          # "712109809"
  "app_secret": QOBUZ_DEFAULT_API_APP_SECRET,  # "589be88e4538daea11f509d29e4a23b1"
  "source": "embedded-default",
  "fetched_at_unix": <now unix>
}
```
> DIVERGENCE FLAG: The fallback path **prefers a STALE disk cache over the embedded default**. The embedded default is used **only** when there is no disk cache AND no in-memory cache AND scrape/validation failed. A naive port that jumps straight to the embedded constants on scrape failure would diverge.

### 1.2 Freshness (`qobuzCredentialsCacheIsFresh`)
Returns `False` if creds is `None`, OR `fetched_at_unix == 0`, OR `app_id.strip() == ""`, OR `app_secret.strip() == ""`. Otherwise fresh iff `now - fetched_at_unix < 24h` (`time.Since(time.Unix(fetched_at_unix,0)) < TTL`).

`loadQobuzCachedCredentials`: reads file; if not-exist → returns `(None, None)` (no error); on other read error → error; parse JSON; if `app_id.strip()==""` or `app_secret.strip()==""` → error `"qobuz credentials cache is incomplete"`.

### 1.3 Scrape (`scrapeQobuzOpenCredentials`)
Two sequential GETs on the passed client (30s timeout):

**Call A — shell HTML**
- Method `GET`, URL `https://open.qobuz.com/track/1`
- Header: `User-Agent: <QOBUZ_DEFAULT_UA>` (only header set)
- If status != 200: read up to 512 bytes of body, error `"open.qobuz.com returned status <code>: <preview-trimmed>"`.
- Read full body. Apply `QOBUZ_OPEN_BUNDLE_SCRIPT_PATTERN`. If no match (group count < 2) → error `"qobuz open bundle URL not found"`.
- `bundleURL = match.group(1).strip()`. **If it starts with `/`, prefix with `https://open.qobuz.com`** (relative→absolute). If empty after that → error `"qobuz open bundle URL is empty"`.

**Call B — JS bundle**
- Method `GET`, URL = `bundleURL`
- Header: `User-Agent: <QOBUZ_DEFAULT_UA>`
- If status != 200: read up to 512 bytes, error `"qobuz open bundle returned status <code>: <preview-trimmed>"`.
- Read full body. Apply `QOBUZ_OPEN_API_CONFIG_PATTERN`. If no match (group count < 3) → error `"qobuz api app_id/app_secret pair not found in open bundle"`.
- Return creds: `app_id = group(1).strip()`, `app_secret = group(2).strip()`, `source = bundleURL`, `fetched_at_unix = now`.

Redirects: Go's default `http.Client` follows up to 10 redirects automatically; replicate with `allow_redirects=True`.

### 1.4 Probe validation (`qobuzCredentialsSupportSignedMetadata`)
Builds a **signed** request (§2) with path `track/search` and params `query=USUM71703861`, `limit=1`. Executes on the passed 30s client. Returns `True` iff: request built OK **and** HTTP request succeeded **and** status == 200 **and** JSON decodes **and** `tracks.total > 0`. Any failure → `False`.

Probe response parse shape (only this key is read):
```python
# qobuzCredentialProbeResponse
payload["tracks"]["total"]  # int
```

---

## 2. Request signing (`newQobuzSignedRequestWithCredentials` + helpers)

This is the load-bearing crypto. Reproduce exactly.

### 2.1 Path normalization (`qobuzNormalizedPath`)
```python
def qobuz_normalized_path(path: str) -> str:
    return path.strip().strip("/")   # Go: strings.Trim(strings.TrimSpace(path), "/")
```
Trims leading/trailing whitespace, then trims leading/trailing `/`. E.g. `"track/search"` → `"track/search"`; `"/track/get/"` → `"track/get"`.

### 2.2 Signature payload (`qobuzSignaturePayload`)
```python
def qobuz_signature_payload(path: str, params: dict[str, list[str]], timestamp: str, secret: str) -> str:
    # normalizedPath with ALL slashes removed
    normalized = qobuz_normalized_path(path).replace("/", "")   # "track/search" -> "tracksearch"
    keys = [k for k in params.keys()
            if k not in ("app_id", "request_ts", "request_sig")]
    keys.sort()                                                  # Go sort.Strings -> bytewise ascending
    out = [normalized]
    for key in keys:
        values = params[key]
        if len(values) == 0:
            out.append(key)                                      # key with no values -> just the key
            continue
        for value in values:
            out.append(key)
            out.append(value)                                    # key, value, key, value...
    out.append(timestamp)
    out.append(secret)
    return "".join(out)
```
Critical quirks:
- The path used for the signature has **every slash stripped** (not just leading/trailing): `track/search` → `tracksearch`, `track/get` → `trackget`.
- Excluded keys (never in signature): `app_id`, `request_ts`, `request_sig`.
- Keys sorted ascending (Go `sort.Strings` = Unicode code-point/byte order; equivalent to Python `sorted()` on ASCII keys).
- For a multi-valued key, **every** value is appended as `key+value` repeated. (In practice params are single-valued here.)
- Order of concatenation: `normalizedPathNoSlash` + (for each sorted key: `key`+`value`…) + `timestamp` + `secret`.
- **IMPORTANT — which params are signed:** `qobuzSignaturePayload` receives the **ORIGINAL `params`** passed into `newQobuzSignedRequestWithCredentials`, NOT the cloned set that has `app_id`/`request_ts` added (see line 256: `qobuzRequestSignature(normalizedPath, params, timestamp, ...)`). So even though the exclusion list also guards those keys, the signed input is the caller's params only.

### 2.3 Signature (`qobuzRequestSignature`)
```python
import hashlib
def qobuz_request_signature(path, params, timestamp, secret) -> str:
    payload = qobuz_signature_payload(path, params, timestamp, secret)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()   # lowercase hex, 32 chars
```
MD5 over the UTF-8 bytes of the payload, lowercase hex (`hex.EncodeToString` is lowercase).

### 2.4 Worked example (probe call — verify your port against this)
Inputs: path `"track/search"`, params `{"query": ["USUM71703861"], "limit": ["1"]}`, secret `"589be88e4538daea11f509d29e4a23b1"`, suppose `timestamp = "1700000000"`.
- normalized-no-slash = `"tracksearch"`
- sorted keys (excluding signing keys) = `["limit", "query"]`
- payload = `"tracksearch" + "limit" + "1" + "query" + "USUM71703861" + "1700000000" + "589be88e4538daea11f509d29e4a23b1"`
  = `"tracksearchlimit1queryUSUM717038611700000000589be88e4538daea11f509d29e4a23b1"`
- `request_sig = md5(payload).hexdigest()`

### 2.5 Building the request (`newQobuzSignedRequestWithCredentials`)
```python
def new_qobuz_signed_request(method, path, params, creds):
    normalized = qobuz_normalized_path(path)
    if normalized == "":
        raise ValueError("qobuz request path is empty")
    if creds is None or not creds["app_id"].strip() or not creds["app_secret"].strip():
        raise ValueError("qobuz credentials are incomplete")

    timestamp = str(int(time.time()))                  # fmt.Sprintf("%d", time.Now().Unix())
    sig = qobuz_request_signature(normalized, params, timestamp, creds["app_secret"])

    # cloned params = original params PLUS the three signing params
    query = {**params, "app_id": [creds["app_id"]],
             "request_ts": [timestamp], "request_sig": [sig]}
    # URL = base + "/" + normalized + "?" + urlencode(query, sorted-by-key)
    qs = go_url_values_encode(query)
    req_url = f"{QOBUZ_API_BASE_URL}/{normalized}?{qs}"
    # headers:
    headers = {
        "User-Agent": QOBUZ_DEFAULT_UA,
        "Accept": "application/json",
        "X-App-Id": creds["app_id"],
    }
    return method, req_url, headers
```
URL composition: `fmt.Sprintf("%s/%s?%s", base, normalized, clonedParams.Encode())`.

**Go `url.Values.Encode()` quirks to replicate exactly:**
- Encodes as `key=value&key=value`, with **keys sorted ascending** (`Encode` sorts by key). For multi-valued keys, values appear in insertion order under that key.
- Escaping uses Go `url.QueryEscape`: spaces → `+`; unreserved `A–Z a–z 0–9 - _ . ~` left as-is; everything else percent-encoded uppercase hex. (Note: `~` is NOT escaped by Go; Python's `urllib.parse.quote_plus` also leaves `-_.~` unescaped by default via `safe`? — Python's `quote_plus` does NOT keep `~` safe by default; pass `quote_via=quote_plus` and `safe=""` won't add `~`. To match Go exactly, use a custom encoder that keeps `~` unescaped and turns spaces into `+`.) For the literal ASCII params used here (`USUM71703861`, `1`, app_id digits, ts digits, md5 hex) there are no characters that differ between encoders, so any standard `urlencode` works for the current call sites — but a faithful port should mirror Go's `~`/space rules.

**Headers — exact casing/values:** `User-Agent`, `Accept: application/json`, `X-App-Id: <app_id>`. (Go canonicalizes header names; emit them with this casing.) No `Content-Type`, no body (GET with `nil` body).

### 2.6 Refresh-on-failure execution (`doQobuzSignedRequest`)
```python
def do_qobuz_signed_request(method, path, params, client=None):
    # default client timeout 20s
    def call(force_refresh):
        creds = get_qobuz_api_credentials(force_refresh)
        method_, url_, headers_ = new_qobuz_signed_request(method, path, params, creds)
        return client.request(method_, url_, headers=headers_)

    resp = call(force_refresh=False)
    if qobuz_should_refresh_credentials(resp.status_code):  # 400 or 401
        resp.close()
        return call(force_refresh=True)                     # re-scrape, retry ONCE
    return resp
```
`qobuzShouldRefreshCredentials(code)` → `True` iff `code == 400` (HTTP Bad Request) **or** `code == 401` (Unauthorized). The retry happens **exactly once** (no loop). `forceRefresh=True` forces a re-scrape path in `getQobuzAPICredentials` (skips the "fresh cache" early returns).

`doQobuzSignedJSONRequest`: GET via `doQobuzSignedRequest` with a fresh `http.Client{Timeout: 20s}`. If final status != 200 → read up to 2048 bytes, error `"qobuz request failed: HTTP <code>: <snippet-trimmed>"`. Else `json.Decode` into target.

Timeouts summary: scrape+probe client = **30s**; signed-request default client = **20s**; JSON-request client = **20s**.

---

## 3. Provider ordering

### 3.1 Frontend default download-provider order
Two distinct orderings exist — do not conflate them:

**(a) URL list `defaultQobuzDownloadProviderURLs`** (`provider_endpoints.go`) — order:
```python
DEFAULT_QOBUZ_DOWNLOAD_PROVIDER_URLS = [
    "https://music.wjhe.top/api/music/qobuz/url",   # WJHE stream
    "https://music.gdstudio.xyz/api.php",           # GDStudio .xyz
    "https://music.gdstudio.org/api.php",           # GDStudio .org
    "https://www.musicdl.me/api/qobuz/download",    # MusicDL (last)
]
```
`GetQobuzDownloadProviderURLs()` returns a copy of this slice.

**(b) Provider-object order `getQobuzDownloadProviders()`** (`qobuz_providers.go` lines 77–83):
```
[ QobuzProviderWJHE, QobuzProviderGDStudio, QobuzProviderMusicDL ]
```
Each provider yields `qobuzProviderAttempt`s via `Attempts(trackID, quality)`:
- **WJHE** → 1 attempt, `ID = GetQobuzWJHEStreamAPIURL()` (`https://music.wjhe.top/api/music/qobuz/url`), download = `DownloadFromWJHE(trackID, quality)`.
- **GDStudio** → one attempt per URL in `GetQobuzGDStudioAPIURLs()` = `["https://music.gdstudio.xyz/api.php", "https://music.gdstudio.org/api.php"]` (XYZ first, ORG second), each download = `DownloadFromGDStudio(trackID, quality, apiURL)`. (Go closure captures `currentAPIURL := apiURL` to avoid loop-var aliasing; in Python just bind per-iteration.)
- **MusicDL** → 1 attempt, `ID = GetQobuzMusicDLDownloadAPIURL()` (`https://www.musicdl.me/api/qobuz/download`), download = `DownloadFromMusicDL(trackID, quality)`.

So the **flattened attempt-ID order** before any adaptive reordering is: WJHE → GDStudio.xyz → GDStudio.org → MusicDL.

### 3.2 "MusicDL pinned last" helper (`moveQobuzAttemptIDsLast`)
```python
def move_qobuz_attempt_ids_last(provider_ids, *last_ids):
    if not provider_ids or not last_ids:
        return list(provider_ids)               # copy
    last_set = set(last_ids)
    ordered, trailing = [], []
    for pid in provider_ids:
        (trailing if pid in last_set else ordered).append(pid)
    return ordered + trailing                    # stable; trailing keeps relative order
```
Stable partition: non-last IDs keep their order, listed last-IDs are appended preserving their encounter order. This is how MusicDL's URL is forced to the bottom regardless of adaptive scores. (Note: the call sites passing `qobuzMusicDLDownloadAPIURL` live in other files; this file only defines the helper. Replicate the helper; the caller pins MusicDL.)

### 3.3 Adaptive success/failure scoreboard (`provider_priority.go`)
A persistent, per-service reordering layer applied to a list of provider keys (strings). Backed by a **bbolt** DB at `~/.spotiflac/provider_priority.db`, bucket `"ProviderPriority"`.

> PORT NOTE: bbolt is a Go-specific embedded B+tree KV store with its own on-disk format. A Python port should NOT attempt to read Go's `.db` file. Use any local KV/JSON/SQLite store keyed the same way (`service|provider`) with the same record schema and the same sort logic. The behavior (not the file format) is what must match. If cross-compat with the Go app's existing DB is required, that's a separate concern (bbolt has no native Python reader).

**Record schema** (`providerPriorityEntry`, JSON-marshalled with `json.Marshal` — compact, no indent):
```python
{
  "service": str,          # lowercased, trimmed service name
  "provider": str,         # provider key as given (trimmed only)
  "last_outcome": str,     # "success" | "failure" | ""
  "last_attempt": int,     # unix seconds
  "last_success": int,
  "last_failure": int,
  "success_count": int,
  "failure_count": int,
}
```

**Key function** (`providerPriorityKey`):
```python
def provider_priority_key(service, provider):
    return service.strip().lower() + "|" + provider.strip()
# NOTE asymmetry: service is lowercased+trimmed; provider is ONLY trimmed (case preserved).
```

**Recording outcomes** (`recordProviderOutcome(service, provider, success)`):
- No-op if `service.strip()==""` or `provider.strip()==""`.
- `serviceKey = service.strip().lower()`, key = `provider_priority_key`, `now = unix()`.
- Load existing entry (if any) for that key; else new entry with `service=serviceKey, provider=provider`.
- Always set `last_attempt = now`.
- If success: `last_outcome="success"`, `last_success=now`, `success_count += 1`.
- Else: `last_outcome="failure"`, `last_failure=now`, `failure_count += 1`.
- Persist JSON. `recordProviderSuccess`/`recordProviderFailure` are thin wrappers.

**Outcome rank** (`providerOutcomeRank`):
```python
def provider_outcome_rank(outcome):
    o = outcome.strip().lower()
    if o == "success": return 2
    if o == "":        return 1
    # anything else (e.g. "failure") -> 0
    return 0
```

**Reordering** (`prioritizeProviders(service, providers)`):
1. `ordered = list(providers)` (copy). If `len < 2`, return as-is (no DB touch).
2. Init DB; on error print `"Warning: failed to init provider priority DB: <err>"` and return `ordered` unchanged.
3. `serviceKey = service.strip().lower()`. Read each provider's entry from the bucket (key = `serviceKey|provider.strip()`). Missing keys → no entry. On read error print `"Warning: failed to read provider priority DB: <err>"` and return `ordered` unchanged.
4. Capture `originalIndex[provider] = idx` (pre-sort positions, for tie-break stability).
5. **Stable sort** (`sort.SliceStable`) with comparator "i before j iff":
   - Primary: higher `providerOutcomeRank(last_outcome)` first (`success`=2 > `unknown/""`=1 > `failure/other`=0).
   - Tie → higher `last_success` (more recent) first.
   - Tie → higher `last_attempt` first.
   - Tie → lower `originalIndex` first (preserve original order).
   Missing entries behave as a zero-value entry: `last_outcome=""` → rank 1, `last_success=0`, `last_attempt=0`.
6. Return `ordered`.

Replicate with a stable sort and a key of `(-rank, -last_success, -last_attempt, original_index)` (ascending) — equivalent to the Go comparator. Use Python `sorted(..., key=...)` (stable) or implement the exact comparator.

> DIVERGENCE FLAGS for the scoreboard:
> - `failure_count`/`success_count` are tracked but **NOT used in sorting** — only `last_outcome`, `last_success`, `last_attempt`, and original index matter. A port that sorts by win/loss counts would diverge.
> - The "MusicDL pinned last" behavior is **independent** of `prioritizeProviders` — it comes from `moveQobuzAttemptIDsLast` at the caller, applied so MusicDL's URL is forced to the tail even if its adaptive score is high. Order of operations at the call site (not in these files) determines whether pin-last runs before/after prioritize; the helper itself unconditionally pins. Ensure your pipeline applies the MusicDL pin AFTER adaptive prioritization so MusicDL truly ends up last.
> - Provider key casing: service is lowercased, provider is case-sensitive (trim only). Keep providers' exact casing consistent between record and lookup.

---

## 4. All endpoint host/URL constants (`provider_endpoints.go`, verbatim)

```python
AMAZON_MUSIC_API_BASE_URL      = "https://amazon.spotbye.qzz.io"

QOBUZ_WJHE_BASE_URL            = "https://music.wjhe.top"
QOBUZ_WJHE_SEARCH_API_URL      = "https://music.wjhe.top/api/music/qobuz/search"   # base + "/api/music/qobuz/search"
QOBUZ_WJHE_STREAM_API_URL      = "https://music.wjhe.top/api/music/qobuz/url"      # base + "/api/music/qobuz/url"
QOBUZ_MUSICDL_DOWNLOAD_API_URL = "https://www.musicdl.me/api/qobuz/download"
QOBUZ_GDSTUDIO_API_URL_XYZ     = "https://music.gdstudio.xyz/api.php"
QOBUZ_GDSTUDIO_API_URL_ORG     = "https://music.gdstudio.org/api.php"
QOBUZ_GDSTUDIO_VERSION         = "2026.5.10"
```

Accessor behaviors:
- `GetQobuzGDStudioAPIURLs()` → `[XYZ, ORG]` (always this order).
- `GetQobuzGDStudioPrimaryAPIURL()` → XYZ; `GetQobuzGDStudioFallbackAPIURL()` → ORG.
- `GetQobuzGDStudioSignatureHost(apiURL)` → parse URL, return `parsed.Host` trimmed; on parse error or empty host return `""`. (`Host` = `host[:port]`; for these URLs it's `music.gdstudio.xyz` / `music.gdstudio.org`.)
- `GetQobuzGDStudioVersion()` → `"2026.5.10"`.
- `IsQobuzWJHEProviderURL(raw)` → `True` iff `raw.strip()` equals the WJHE stream URL **or** starts with `WJHE_STREAM_API_URL + "?"`.
- `IsQobuzMusicDLProviderURL(raw)` → case-insensitive equality (`strings.EqualFold`) of `raw.strip()` vs MusicDL download URL.
- `IsQobuzGDStudioProviderURL(raw)` → `True` iff `raw.strip()` equals any GDStudio URL **or** starts with `<gdurl> + "?"` (for XYZ or ORG).
- `GetAmazonMusicAPIBaseURL()` → `"https://amazon.spotbye.qzz.io"`.

---

## 5. Cross-cutting quirks checklist (for the porter)

- MD5 signature payload uses path with **all** slashes removed (`tracksearch`, not `track/search`).
- Signed params: keys sorted; `app_id`/`request_ts`/`request_sig` excluded from the signature payload; the signature is computed over the **caller's original params**, then `app_id`/`request_ts`/`request_sig` are added to the query string.
- `request_ts` = current unix seconds as decimal string; identical value used in both the signature and the query param.
- Refresh-on-400/401 retries the signed request exactly once with `forceRefresh=True`.
- Credential cache TTL = 24h; staleness measured against `fetched_at_unix`. A stale disk cache is still preferred over the embedded default constants when refresh fails.
- Scrape requires BOTH the bundle-URL regex (1 capture group) AND the app_id/app_secret regex (2 capture groups, `\d{9}` and `[a-f0-9]{32}`). Relative bundle URLs (leading `/`) are made absolute against `https://open.qobuz.com`.
- Probe validation requires HTTP 200 AND `tracks.total > 0` for the search of ISRC `USUM71703861` (`limit=1`).
- Provider adaptive sort ignores counts; uses outcome-rank → last_success → last_attempt → original index, all stable.
- bbolt DB is not Python-readable; replicate the schema/keying/sort behavior in a Python-native store.

Relevant file paths (absolute):
- `SpotiFLAC/backend/qobuz_api.go`
- `SpotiFLAC/backend/qobuz_providers.go`
- `SpotiFLAC/backend/provider_priority.go`
- `SpotiFLAC/backend/provider_endpoints.go`
- `SpotiFLAC/backend/http_headers.go` (UA constant)
- `SpotiFLAC/backend/ffmpeg.go` (app-dir `~/.spotiflac`)