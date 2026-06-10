# Ground-truth spec: bridge
Source Go files: songlink.go, link_resolver.go, soundplate.go, songstats.go

I now have everything needed for the bridge spec, including the resolver-ordering config and the ISRC-source helper signatures. Producing the spec.

# SpotiFLAC Bridge — Porting Spec (Spotify → Tidal-id / Amazon-ASIN)

Source files: `songlink.go`, `link_resolver.go`, `soundplate.go`, `songstats.go` (plus load-bearing helpers in `config.go`, `isrc_finder.go`). Package `backend`. Reproduce byte-for-byte in Python.

This subsystem maps a Spotify track to **Tidal track URLs**, **Amazon Music ASIN URLs**, and **Deezer URLs**, using ISRC as the join key. There are two interchangeable resolvers (Songstats and Deezer+Songlink), ordered by config. **Songlink is fed a Deezer URL, never a Spotify URL.** Qobuz availability is adjacent (touched here only via `checkQobuzAvailability`) and out of scope except where it shares the ISRC flow.

---

## 1. Verbatim constants

```python
# songlink.go:14
SONGLINK_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"

# soundplate.go:13-15
SOUNDPLATE_SPOTIFY_API_URL = "https://phpstack-822472-6184058.cloudwaysapps.com/api/spotify.php"
SOUNDPLATE_REFERER_URL     = "https://phpstack-822472-6184058.cloudwaysapps.com/?"
SOUNDPLATE_USER_AGENT      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
# NOTE: Songlink UA is Chrome/145, Soundplate UA is Chrome/146 — DIFFERENT, do not unify.

# link_resolver.go:17-18  (config string values)
LINK_RESOLVER_PROVIDER_SONGSTATS       = "songstats"
LINK_RESOLVER_PROVIDER_DEEZER_SONGLINK = "deezer-songlink"
```

### Regexes (verbatim — Go `regexp`, RE2 syntax)

```python
import re
# songlink.go:17-19
ISRC_PATTERN            = re.compile(r"\b([A-Z]{2}[A-Z0-9]{3}\d{7})\b")
AMAZON_ALBUM_TRACK_PATH = re.compile(r"/albums/[A-Z0-9]{10}/(B[0-9A-Z]{9})")
AMAZON_TRACK_PATH       = re.compile(r"/tracks/(B[0-9A-Z]{9})")

# songstats.go:13 — Go flags (?is): i=case-insensitive, s=dot matches newline.
# Python equivalent: re.IGNORECASE | re.DOTALL. Note Go's [^>]+ requires >=1 char before the close.
SONGSTATS_SCRIPT_PATTERN = re.compile(
    r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.IGNORECASE | re.DOTALL,
)
```

Quirk on `ISRC_PATTERN`: ASIN-style `B...` codes can never match (require 12-char ISRC shape `[A-Z]{2}[A-Z0-9]{3}\d{7}`). The 5th char of an ISRC year/owner block plus 7 trailing digits.

---

## 2. HTTP client

`NewSongLinkClient()` (songlink.go:60) → a single `http.Client{Timeout: 30s}` reused for: Songlink, Deezer-ISRC lookup, Soundplate, Songstats. Go default redirect policy (follows up to 10 redirects, dropping body but re-issuing GET). Two endpoints create their OWN short-lived clients:
- `getDeezerISRC` → `http.Client{Timeout: 10s}` (songlink.go:273)
- (Qobuz/Spotify-album helpers use 30s, out of scope)

Python: use one `requests.Session`/`httpx.Client` with a 30s timeout for the shared client; a separate 10s call for `getDeezerISRC`.

---

## 3. Top-level entry points (the bridge surface)

All three call `resolveSpotifyTrackLinks(spotifyTrackID, region)` first, then post-process.

### 3a. `GetAllURLsFromSpotify(spotifyTrackID, region) -> SongLinkURLs` (songlink.go:68)
1. `links, err = resolveSpotifyTrackLinks(id, region)`.
2. If `err != nil` AND (`links is None` OR both `TidalURL` and `AmazonURL` empty) → return error.
3. Build result: `TidalURL = links.TidalURL`, `AmazonURL = normalizeAmazonMusicURL(links.AmazonURL)`, `ISRC = links.ISRC`. (Deezer not exposed here.)
4. If both Tidal+Amazon still empty → return `err` if set else `fmt.Errorf("no streaming URLs found")`.

JSON shape (`SongLinkURLs`, songlink.go:26-30): `{"tidal_url","amazon_url","isrc"}`.

### 3b. `CheckTrackAvailability(spotifyTrackID) -> TrackAvailability` (songlink.go:91; region `""`)
- Populates booleans + URLs for Tidal/Amazon/Deezer from `links`; `*URL = normalize...(...)`; bool = `url != ""`.
- ISRC resolution chain: `links.ISRC` (trimmed) → if empty and `DeezerURL` present, `getDeezerISRC(DeezerURL)` → if still empty, `lookupSpotifyISRC(id)` (Soundplate/Spotify direct).
- If ISRC present → `checkQobuzAvailability(isrc)` sets `Qobuz`,`QobuzURL`.
- Return success if ANY of Tidal/Amazon/Deezer/Qobuz true, else the error or `"no platforms found"`.

JSON (`TrackAvailability`, songlink.go:32-42): `{"spotify_id","tidal","amazon","qobuz","deezer","tidal_url"?,"amazon_url"?,"qobuz_url"?,"deezer_url"?}` (URL fields `omitempty`).

### 3c. `GetDeezerURLFromSpotify(spotifyTrackID) -> str` (songlink.go:227; region `""`)
- Prefer `links.DeezerURL` (normalized). Else resolve ISRC (links.ISRC → Soundplate fallback) → `lookupDeezerTrackURLByISRC(isrc)`. Else error or `"deezer link not found"`.
- Side effect: `fmt.Printf("Found Deezer URL: %s\n", deezerURL)`.

### 3d. `GetISRC` / `GetISRCDirect` (songlink.go:301, 331)
- `GetISRC`: `links.ISRC` → `getDeezerISRC(links.DeezerURL)` → `lookupSpotifyISRC(id)` → combined error `"%v | %v"`.
- `GetISRCDirect`: just `lookupSpotifyISRC(id)`.

---

## 4. `resolveSpotifyTrackLinks` — the orchestrator (link_resolver.go:21)

```
links = resolvedTrackLinks{}          # fields: TidalURL, AmazonURL, DeezerURL, ISRC (all "")
attempts = []
isrc, err = lookupSpotifyISRC(spotifyTrackID)
if err: attempts.append(f"spotify isrc: {err}")
else:   links.ISRC = isrc

if links.ISRC != "":
    for resolver in orderedLinkResolvers():        # see §5
        if resolver == "songstats":
            added, e = resolveLinksViaSongstats(links)
            if e: attempts.append(f"songstats: {e}")
            elif added: print("Using Songstats as configured link resolver")
        elif resolver == "deezer-songlink":
            added, e = resolveLinksViaDeezerSongLink(links, region)
            if e: attempts.append(f"deezer-songlink: {e}")
            elif added: print("Using Songlink as configured link resolver")
        if links.TidalURL != "" and links.AmazonURL != "":   # short-circuit: BOTH present
            return links, None

if hasAnySongLinkData(links):           # any of Tidal/Amazon/Deezer non-empty
    return links, None
if not attempts: attempts = ["no streaming URLs found"]
return links, Error(" | ".join(attempts))
```

Key behaviors:
- **ISRC is mandatory** to run any resolver. No ISRC ⇒ no resolver runs.
- Resolvers **mutate `links` in place and skip already-filled fields** (each resolver only fills empties), so order matters but the second resolver can fill gaps the first missed.
- Early return ONLY when BOTH Tidal and Amazon are set. If only one is set after all resolvers, it still returns success via `hasAnySongLinkData` (which also counts Deezer-only).

`hasAnySongLinkData` (songlink.go:492): true if `TidalURL` OR `AmazonURL` OR `DeezerURL` non-empty (ISRC alone does NOT count).

---

## 5. Resolver ordering (link_resolver.go:70; reads `config.go`)

```python
def orderedLinkResolvers():
    preferred = GetLinkResolverSetting()          # default "deezer-songlink"
    if not GetLinkResolverAllowFallback():        # default True
        return ["deezer-songlink"] if preferred == "deezer-songlink" else ["songstats"]
    if preferred == "deezer-songlink":
        return ["deezer-songlink", "songstats"]
    return ["songstats", "deezer-songlink"]
```

`GetLinkResolverSetting()` (config.go:218): reads `settings["linkResolver"]` (string), lowercased+trimmed. `"songlink"` or `"deezer-songlink"` → `"deezer-songlink"`; `"songstats"` → `"songstats"`; `""`/unknown/error → **default `"deezer-songlink"`**.

`GetLinkResolverAllowFallback()` (config.go:237): reads `settings["allowResolverFallback"]` (bool). Missing/wrong-type/error → **default `True`**.

**Net default behavior**: resolver order is `["deezer-songlink", "songstats"]` (Songlink-first, Songstats fallback).

---

## 6. Deezer-Songlink resolver (link_resolver.go:107)

```
before = copy(links)
attempts = []
if links.DeezerURL == "":
    print(f"Resolving Deezer track from ISRC {links.ISRC}")
    url, e = lookupDeezerTrackURLByISRC(links.ISRC)
    if e: attempts.append(f"deezer isrc: {e}")
    else:
        links.DeezerURL = url
        print(f"Found Deezer URL: {links.DeezerURL}")
if links.DeezerURL != "":
    print("Resolving streaming URLs from song.link via Deezer URL...")
    resp, e = fetchSongLinkLinksByURL(links.DeezerURL, region)   # §6b — Deezer URL passed to Songlink
    if e: attempts.append(f"song.link deezer: {e}")
    else: mergeSongLinkResponse(links, resp)                     # §6c
    if links.ISRC == "":
        isrc2, e2 = getDeezerISRC(links.DeezerURL)
        if not e2: links.ISRC = isrc2
# return (changed, err)
if links != before:
    return (True, None if not attempts else Error(" | ".join(attempts)))
if not attempts: attempts = ["no links found via deezer-songlink"]
return (False, Error(" | ".join(attempts)))
```

Returns `(addedData, err)`. Note it can return `(True, <error>)` when something changed but a sub-step also failed.

### 6a. `lookupDeezerTrackURLByISRC` (songlink.go:378)
- **URL**: `GET https://api.deezer.com/track/isrc:<ISRC>` where `<ISRC>` = `isrc.upper().strip()` inserted RAW (no URL-escaping — colon and code go straight in).
- **Headers**: `User-Agent: <SONGLINK_USER_AGENT>` only.
- **Client**: shared 30s.
- Non-200 → error `"Deezer ISRC API returned status %d"`.
- Parse JSON `{"id":int64,"isrc":str,"link":str}`. Probe order: if `.link != ""` → `normalizeDeezerTrackURL(link)`; elif `.id > 0` → `normalizeDeezerTrackURL("https://www.deezer.com/track/<id>")`; else error `"deezer track link not found for ISRC %s"` (un-uppercased original `isrc`).

### 6b. `fetchSongLinkLinksByURL(rawURL, region)` — **THE SONGLINK CALL** (songlink.go:335)
- **URL template**: `https://api.song.link/v1-alpha.1/links?url=<url.QueryEscape(rawURL)>`
  - `rawURL` here is the **Deezer track URL** (e.g. `https://www.deezer.com/track/3135556`), NOT Spotify.
  - `url.QueryEscape` (Go) encodes per `application/x-www-form-urlencoded`: space→`+`, and escapes reserved chars. For a typical Deezer URL `https://www.deezer.com/track/3135556` the encoded value is `https%3A%2F%2Fwww.deezer.com%2Ftrack%2F3135556`.
  - Python equivalent: `urllib.parse.quote_plus(rawURL, safe="")` — note `quote_plus` already maps space→`+` and does NOT keep `/` safe, matching Go's `QueryEscape`. (Do NOT use `quote` with default safe `/`.)
- If `region != ""`: append `&userCountry=<url.QueryEscape(region)>`. (Here region is `""` for all bridge callers except `GetAllURLsFromSpotify` which passes the caller's region.)
- **Method**: GET. **Headers**: `User-Agent: <SONGLINK_USER_AGENT>` only. No `Accept`, no API key.
- **Timeout**: shared 30s.
- Non-200 → read up to 256 bytes preview → error `"song.link returned status %d (%s)"`.
- Empty body → `"song.link returned empty response"`.
- Parse into `songLinkAPIResponse` (songlink.go:44-48): only `linksByPlatform` is read, each value is `{"url": str}`. Other keys ignored. On JSON error → error with up to 200-char body preview + `"..."`.

### 6c. `mergeSongLinkResponse` (songlink.go:416) — reading the platforms
Probes EXACT platform keys, fills only-if-empty:
```python
m = resp["linksByPlatform"]
if (e := m.get("tidal"))       and e["url"] and links.TidalURL == "":
    links.TidalURL = e["url"].strip();                         print("✓ Tidal URL found")
if (e := m.get("amazonMusic")) and e["url"] and links.AmazonURL == "":
    links.AmazonURL = normalizeAmazonMusicURL(e["url"]);       print("✓ Amazon URL found")
if (e := m.get("deezer"))      and e["url"] and links.DeezerURL == "":
    links.DeezerURL = normalizeDeezerTrackURL(e["url"]);       print("✓ Deezer URL found")
```
- Tidal key: `"tidal"`. Amazon key: `"amazonMusic"`. Deezer key: `"deezer"`.
- Tidal URL is stored **raw-trimmed** (no further normalization at merge time). Tidal id extraction happens elsewhere (consumer side — see §9). Amazon URL is canonicalized immediately via `normalizeAmazonMusicURL`.

---

## 7. `getDeezerISRC` (songlink.go:265) — Deezer track → ISRC
- `extractDeezerTrackID(deezerURL)` (§8) → `trackID`.
- **URL**: `GET https://api.deezer.com/track/<trackID>` (numeric id, raw).
- **Client**: own `http.Client{Timeout: 10s}` (NOT the shared 30s).
- **Headers**: NONE set (plain `client.Get`). Go default UA `Go-http-client/1.1`.
- Non-200 → `"Deezer API returned status %d"`.
- Parse `{"id":int64,"isrc":str,"title":str}`. Empty isrc → error. Else `print("Found ISRC from Deezer: %s (track: %s)")`, return `isrc.strip().upper()`.

---

## 8. Deezer URL helpers (songlink.go:464-490)

`extractDeezerTrackID(rawURL)`:
- Trim. Empty → error.
- `parts = rawURL.split("/track/")`. If `< 2` parts → error.
- `trackID = parts[1].split("?")[0]`, then `strip("/ ")` (strip leading/trailing `/` and spaces). Empty → error.

`normalizeDeezerTrackURL(rawURL)`: if `extractDeezerTrackID` succeeds → `https://www.deezer.com/track/<id>`; else return `rawURL.strip()` unchanged.

---

## 9. Tidal id extraction — NOT in these files

Within this bridge, the Tidal URL is only **stored/normalized-trim**, never parsed to an id. The "split on `/track/`, int before `?`" extraction lives in the Tidal consumer (`tidal.go` or equivalent), not in `songlink.go`. For a faithful port of THIS subsystem:
- From Songlink: store `linksByPlatform["tidal"]["url"]` trimmed.
- From Songstats: store any link containing `listen.tidal.com/track` raw (see §11).
- The downstream id parse (split `"/track/"`, take part after, cut at `"?"`, parse int) is the SAME shape as `extractDeezerTrackID` but is performed by the Tidal downloader, not here. Flag for the Python porter: verify which module owns Tidal-id parsing and keep it identical.

---

## 10. Amazon ASIN extraction / canonicalization — `normalizeAmazonMusicURL` (songlink.go:437)

EXACT order (first match wins), all producing the canonical form `https://music.amazon.com/tracks/<ASIN>?musicTerritory=US`:

```python
def normalizeAmazonMusicURL(rawURL):
    u = rawURL.strip()
    if u == "": return ""
    # (1) query param trackAsin=
    if "trackAsin=" in u:
        parts = u.split("trackAsin=")
        if len(parts) > 1:
            track_asin = parts[1].split("&")[0]      # value up to next '&'
            if track_asin != "":
                return f"https://music.amazon.com/tracks/{track_asin}?musicTerritory=US"
    # (2) /albums/<10-char>/<ASIN>
    m = AMAZON_ALBUM_TRACK_PATH.search(u)            # r"/albums/[A-Z0-9]{10}/(B[0-9A-Z]{9})"
    if m: return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"
    # (3) /tracks/<ASIN>
    m = AMAZON_TRACK_PATH.search(u)                  # r"/tracks/(B[0-9A-Z]{9})"
    if m: return f"https://music.amazon.com/tracks/{m.group(1)}?musicTerritory=US"
    return ""                                         # no recognizable ASIN ⇒ ""
```

Important divergence notes:
- The `trackAsin=` branch does **no regex validation** — it takes whatever follows `trackAsin=` up to `&` verbatim (even if not a valid `B...` ASIN). Branches (2)/(3) require the strict `B[0-9A-Z]{9}` (ASIN = `B` + 9 alphanumerics; total 10 chars).
- `/albums/.../` requires a 10-char album id `[A-Z0-9]{10}` then `/` then the ASIN; it captures the ASIN (the track), not the album.
- Use `.search` (Go `FindStringSubmatch` scans, not anchored). Case-sensitivity: regexes are case-sensitive (uppercase `B`, uppercase `A-Z`); the URL is NOT lowercased before matching.
- A non-empty raw Amazon URL with no ASIN match returns `""` (the value gets dropped, fields stay empty).

---

## 11. Songstats resolver (link_resolver.go:92 + songstats.go)

`resolveLinksViaSongstats` (link_resolver.go:92): requires `links.ISRC`. Snapshots `before`, calls `populateLinksFromSongstats(links, links.ISRC)`, returns `(*links != before, err)`.

`populateLinksFromSongstats` (songstats.go:15):
- **URL**: `GET https://songstats.com/<ISRC>?ref=ISRCFinder`, `<ISRC>` = `isrc.upper().strip()` (raw in path).
- **Headers**: `User-Agent: <SONGLINK_USER_AGENT>` only. Shared 30s client.
- Non-200 → error. Read body.
- `matches = SONGSTATS_SCRIPT_PATTERN.findall(body)` (all `<script type="application/ld+json">…</script>` blocks). None → error `"Songstats JSON-LD not found"`.
- For each match: `scriptBody = html.unescape(match).strip()`; skip empty; `json.loads`; on parse error skip. Then `collectSongstatsLinks(payload, links)`; if links changed → `found = True`.
  - Go uses `html.UnescapeString` (unescapes `&amp; &lt; &gt; &#34; &#39;` etc.) BEFORE JSON parse. Python: `html.unescape`.
- If `not found and not hasAnySongLinkData(links)` → error `"no platform links found in Songstats"`.

`collectSongstatsLinks` (songstats.go:74): recursive walk of the decoded JSON. On any `dict`, if it has key `"sameAs"`, call `applySongstatsSameAs`, then recurse all values; on `list`, recurse each. (`sameAs` may be a string or list of strings.)

`assignSongstatsLink` (songstats.go:103) — substring routing, fill-only-if-empty:
- contains `"listen.tidal.com/track"` → `TidalURL = link` (raw); `print("✓ Tidal URL found via Songstats")`
- contains `"music.amazon.com"` → `n = normalizeAmazonMusicURL(link)`; if `n != ""` set `AmazonURL = n`; `print("✓ Amazon URL found via Songstats")`
- contains `"deezer.com"` → `DeezerURL = normalizeDeezerTrackURL(link)`; `print("✓ Deezer URL found via Songstats")`

`switch` is first-match (a link matching Tidal won't also be tested for Amazon). Tidal stored raw (not normalized) — same as Songlink.

---

## 12. ISRC source: `lookupSpotifyISRC` and Soundplate (soundplate.go + isrc_finder.go)

`lookupSpotifyISRC(spotifyTrackID)` (isrc_finder.go:121): delegates to `GetSpotifyTrackIdentifiersDirect(id)` (Spotify-token path; out of scope but it internally also uses Soundplate as a fallback). Returns `.ISRC` or error `"no Spotify ISRC found for track %s"`. This is the primary ISRC supplier feeding every resolver.

`lookupSpotifyISRCViaSoundplate(spotifyTrackID)` (soundplate.go:29) — the Soundplate ISRC API (returns `(isrc, resolvedTrackID, err)`):
- `extractSpotifyTrackID(id)` → `normalizedTrackID`.
- `spotifyTrackURL = f"https://open.spotify.com/track/{normalizedTrackID}"`.
- Query: `url.Values{}; q.Set("q", spotifyTrackURL); q.Encode()` → `?q=<encoded>`. (`url.Values.Encode` sorts keys and uses `QueryEscape`: space→`+`, `:`→`%3A`, `/`→`%2F`.)
- **URL**: `GET https://phpstack-822472-6184058.cloudwaysapps.com/api/spotify.php?q=<encoded spotify track url>`.
- **Headers** (exact casing + order set in Go):
  ```
  User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36
  Accept: */*
  Referer: https://phpstack-822472-6184058.cloudwaysapps.com/?
  Accept-Language: en-US,en;q=0.9,id;q=0.8
  Sec-CH-UA: "Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"
  Sec-CH-UA-Mobile: ?0
  Sec-CH-UA-Platform: "Windows"
  Sec-Fetch-Dest: empty
  Sec-Fetch-Mode: cors
  Sec-Fetch-Site: same-origin
  Priority: u=1, i
  ```
  (Note the literal embedded double-quotes in `Sec-CH-UA*` values and `Referer` ending in `/?`.)
- Shared 30s client. Read body first, THEN check status: non-200 → error with up-to-256-char preview `"Soundplate ISRC returned status %d (%s)"`.
- Parse `soundplateSpotifyResponse` (soundplate.go:18-27): keys `name,artist,album,album_type,artwork_url,isrc,year,spotify_url`.
- ISRC: `firstISRCMatch(payload.ISRC)`; if empty `firstISRCMatch(string(body))` (regex over whole body); if still empty → error `"ISRC missing in Soundplate response"`.
- `resolvedTrackID`: if `payload.spotify_url != ""` and `extractSpotifyTrackID` succeeds → that id; else `""`.

`firstISRCMatch(body)` (songlink.go:499): `m = ISRC_PATTERN.search(body.upper())`; return `m.group(1).strip()` or `""`. (Uppercases the whole input before matching.)

`extractSpotifyTrackID(value)` (isrc_finder.go:312):
- Trim; empty → error.
- `spotify:track:` prefix → substring after last `:`.
- Else `url.Parse`; if scheme http/https → split `path.Trim("/")` on `/`; if `>=2` parts and `parts[0]=="track"` → `parts[1]`; else error.
- Else if `len(value)==22` → value as-is.
- Else error.

---

## 13. `resolvedTrackLinks` struct & equality semantics

```python
@dataclass
class ResolvedTrackLinks:
    TidalURL: str = ""
    AmazonURL: str = ""
    DeezerURL: str = ""
    ISRC: str = ""
```
Go compares structs by value (`*links != before`) to detect "did anything change". In Python replicate by snapshotting all four fields (e.g. tuple or dataclass `==`) before/after each resolver and Songstats parse. **All four fields participate**, including `ISRC` — a resolver that only newly fills `ISRC` (e.g. via `getDeezerISRC`) counts as "changed/addedData", even though `hasAnySongLinkData` (URLs only) would be false.

---

## 14. Caching

There is **no caching in the bridge subsystem itself**. The only cache touched in this neighborhood is ISRC caching via `PutCachedISRC`/`GetCachedISRC` inside `isrc_finder.go` (`cacheResolvedSpotifyTrackISRC`, isrc_finder.go:133) — invoked by the Spotify identifier path, not by `resolveSpotifyTrackLinks`. No Songlink/Songstats/Deezer/Amazon response is cached, and there are no TTLs or refresh-on-400/401 conditions in these four files. Songlink/Deezer calls carry no auth, so no token refresh applies here.

---

## 15. Side-effect prints (stdout — reproduce if logs are compared)

`✓ Tidal URL found`, `✓ Amazon URL found`, `✓ Deezer URL found` (Songlink merge); `✓ Tidal URL found via Songstats`, `✓ Amazon URL found via Songstats`, `✓ Deezer URL found via Songstats`; `Found Deezer URL: %s`; `Resolving Deezer track from ISRC %s`; `Resolving streaming URLs from song.link via Deezer URL...`; `Fetching Songstats links for ISRC %s`; `Using Songstats as configured link resolver`; `Using Songlink as configured link resolver`; `Found ISRC from Deezer: %s (track: %s)`. The `✓` is the literal UTF-8 check mark U+2713.

---

## 16. Divergences / gotchas to flag for the Python porter

1. **Songlink is fed the Deezer URL, not Spotify.** A goal-file that says "Spotify → Songlink" is wrong: the chain is Spotify-ISRC → Deezer-track-URL (via `api.deezer.com/track/isrc:`) → Songlink `?url=<deezer url>`.
2. **Two different Chrome UA versions** (145 for Songlink/Songstats/Deezer-ISRC-lookup; 146 for Soundplate). Don't unify.
3. **`getDeezerISRC` sends NO User-Agent** (default Go UA), unlike `lookupDeezerTrackURLByISRC` which sends the Songlink UA. Different endpoints (`/track/<id>` vs `/track/isrc:<isrc>`), different clients (10s vs 30s).
4. **`url.QueryEscape` ≠ Python `quote`.** Use `quote_plus(..., safe="")` to match (space→`+`, `/`→`%2F`).
5. **Resolver default is `deezer-songlink` (Songlink), with Songstats as fallback** — i.e. Songlink-first by default, contrary to any "Songstats primary" assumption.
6. **`normalizeAmazonMusicURL` returns `""` (drops the value) when no ASIN pattern matches**, but the `trackAsin=` branch is unvalidated (accepts any token).
7. **Fill-only-if-empty + first-match switch**: both resolvers and Songstats routing never overwrite an already-set field, and Songstats's URL classification stops at the first substring match.
8. **Tidal URL is never id-parsed in these files** — only stored. The "split `/track/`, int before `?`" lives downstream (Tidal module); verify and mirror it there.
9. **No Songstats *fallback inside Songlink*** — "Songstats fallback" means the resolver-ordering list (§5), executed by `resolveSpotifyTrackLinks`, not a per-call fallback. Each resolver runs once; ordering decides who fills gaps.
10. **`html.UnescapeString` before JSON parse** in Songstats — Python `html.unescape`. The `&amp;`-unescaping matters for embedded URLs in JSON-LD.
11. **Body-then-status order** in Soundplate (reads body before checking status code) — irrelevant to output but matches Go.
12. Short-circuit in `resolveSpotifyTrackLinks` only fires when **BOTH** Tidal and Amazon are present; Deezer-only or single-platform results do not short-circuit but still succeed via `hasAnySongLinkData`.