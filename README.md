<div align="center">

# ♪ Setlist

**A Spotify downloader with a multi-playlist queue and real lossless sources.**

*Queue your setlist — download every playlist.*

</div>

---

Setlist takes a Spotify **playlist / album / track** URL and downloads the audio
from your choice of **source**: YouTube (default, via `yt-dlp`), genuine-FLAC
lossless providers, or Spotify's own stream. Every file gets proper tags +
embedded cover art for its container (ID3 for `.mp3`, iTunes atoms for `.m4a`,
Vorbis comments for `.flac` / `.ogg` / `.opus`). It's a desktop app (PyQt5) with
a sidebar-navigated UI: **Home**, **Queue**, **History**, and **Settings**.

## Download sources

| Source | What you get | Notes |
|---|---|---|
| **YouTube** (default) | mp3 / m4a / opus / ogg at your chosen bitrate, or **`original`** — the source codec kept as-is, never re-encoded | Optional **YouTube Premium cookies** unlock ~256 kbps source streams (formats 774/141; needs yt-dlp's JS runtime — see the in-app help) |
| **Real FLAC** | Genuine lossless from Qobuz / Tidal / Amazon, up to Hi-Res 24-bit | ISRC-exact matching; a file is only kept as `.flac` when its bytes really are FLAC — a lossy stream is **never** dressed up as lossless. Tidal needs a self-hosted API instance (URL in Settings) |
| **Spotify (librespot)** | Spotify's own ~320 kbps OGG Vorbis, captured verbatim (no re-encode) | Opt-in; requires **your** Spotify Premium login and may violate Spotify's ToS — read the in-app disclaimer first |

**If unavailable, try…** — a per-source fallback chain (e.g. lossless → Spotify →
YouTube) decides what happens when the primary source can't serve a track.
Fallback downloads stay honest: a YouTube fallback keeps its real lossy codec
(`original`) and is labelled with the source that actually served it — you never
get a fake-lossless file.

## What this fork adds

| Feature | Setlist | Upstream (Sunnify) |
|---|:---:|:---:|
| **Real FLAC + Spotify (librespot) download sources** with byte-level lossless honesty | ✅ | — |
| **User-ordered fallback chain** ("If unavailable, try") | ✅ | — |
| **`original` format** — keep the downloaded codec, zero re-encoding | ✅ | — |
| **Per-source format/quality offerings** — only formats a source can honestly deliver | ✅ | one global list |
| **YouTube Premium cookies** for ~256 kbps source audio | ✅ | — |
| **Multi-playlist queue** — paste many URLs, download sequentially, each into its own folder, with a live per-item status panel | ✅ | — |
| **Extended-mix mode** — optionally prefer extended / club cuts over the radio edit | ✅ (opt-in) | — |
| **Redesigned UI** — sidebar navigation, in-window Settings pane, live track list, download History | ✅ | gradient / frameless window |
| **Configurable filename order** — `Title - Artist` *or* `Artist - Track` | ✅ | one fixed convention |
| **Max length / file-size caps** — reject over-long or oversized candidates (avoids grabbing an hour-long mix) | ✅ | — |
| YouTube sourcing via `yt-dlp`, duration-locked match | ✅ | ✅ |
| Resume manifest for large playlists | ✅ | ✅ |
| Parallel track downloads | ✅ | ✅ |
| Tags + embedded cover art (ID3 / iTunes atoms / Vorbis comments) | ✅ incl. `.ogg`/`.opus` | ✅ (skips ogg/opus) |

> The rows marked ✅ on both sides are upstream's work that Setlist inherits —
> the point of the table is the *delta*, not a scoreboard.

## Install a release build

Grab the latest binaries from the
[**Releases page**](https://github.com/payne0420/setlist/releases) —
`Setlist-macOS.zip`, `Setlist-Windows.exe`, or `Setlist-Linux` (FFmpeg is
bundled). On macOS there's also a Homebrew cask:

```bash
brew tap payne0420/setlist https://github.com/payne0420/setlist
brew install --cask setlist
```

## Run from source

Requires **Python 3.9+** and **FFmpeg** on your PATH (`brew install ffmpeg`,
`apt install ffmpeg`, or the Windows build).

```bash
git clone https://github.com/payne0420/setlist.git
cd setlist
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r req.txt
python Spotify_Downloader.py
```

Paste a Spotify URL on **Home** and hit **Download**, or **⬇ Add to Download
Queue** to line up several playlists/albums/tracks and run them in sequence.
Download source, the fallback chain, audio format/quality (including
`original`), lossless quality tier, extended-mix mode, filename order, the
size/length caps, Spotify login, and YouTube cookies all live in **Settings**.

## Build a standalone app

```bash
pip install pyinstaller
pyinstaller Setlist.spec     # produces dist/ for your platform
```

## Tests

```bash
python -m pytest -q
```

## Fork, license & attribution

**Setlist is a fork** of [sunnify-spotify-downloader](https://github.com/sunnypatell/sunnify-spotify-downloader)
by [Sunny Patel](https://github.com/sunnypatell), diverged to pursue a power-user
direction (real lossless sources, download queue, extended-mix mode, more output
controls). The original engineering and copyright remain Sunny's, and it's
distributed under the **same license** — see [LICENSE](LICENSE). Original
copyright © 2024 Sunny Patel. Thanks to the original project; this fork builds
on top of it.

This is a **student / educational portfolio project**: only use it with content
you own or have permission to download, and in compliance with your local laws
and the source services' terms. The librespot and lossless-provider sources in
particular may violate those services' ToS — they're opt-in, ship no
credentials, and show their own disclaimers in-app. See
[DISCLAIMER.md](DISCLAIMER.md).

To pull upstream bug-fixes into this fork:

```bash
git remote add upstream https://github.com/sunnypatell/sunnify-spotify-downloader.git  # once
git fetch upstream && git cherry-pick <commit>
```
