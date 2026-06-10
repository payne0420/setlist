<div align="center">

# ♪ Setlist

**A Spotify → YouTube downloader with a multi-playlist queue.**

*Queue your setlist — download every playlist.*

</div>

---

Setlist takes a Spotify **playlist / album / track** URL, resolves each track on
YouTube with `yt-dlp`, downloads the audio, and writes proper ID3 tags + cover
art. It's a desktop app (PyQt5).

## What this fork adds

| Feature | Setlist | Upstream (Sunnify) |
|---|:---:|:---:|
| **Multi-playlist queue** — paste many URLs, download sequentially, each into its own folder, with a live per-item status panel | ✅ | — |
| **Extended-mix mode** — optionally prefer extended / club cuts over the radio edit | ✅ (opt-in) | — |
| **Configurable filename order** — `Title - Artist` *or* `Artist - Track` | ✅ | one fixed convention |
| **Max length cap** — reject an over-long candidate (avoids grabbing an hour-long mix) | ✅ | — |
| **Max file-size cap** — discard an oversized download and try the next match | ✅ | — |
| Restyled UI — Spotify-dark theme + native title bar | ✅ | custom gradient / frameless |
| YouTube sourcing via `yt-dlp`, duration-locked match | ✅ | ✅ |
| Resume manifest for large playlists | ✅ | ✅ |
| Parallel track downloads | ✅ | ✅ |
| Formats: mp3 / m4a / opus / original (no re-encode) / flac / wav + bitrate | ✅ | ✅ |
| ID3 tags + embedded cover art | ✅ | ✅ |

> The rows marked ✅ on both sides are upstream's work that Setlist inherits —
> the point of the table is the *delta*, not a scoreboard.

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

Paste a Spotify URL and hit **Get**, or click **⬇ Download Queue** to paste
several playlists/albums/tracks at once and run them in sequence. Output folder,
audio format/quality, extended-mix mode, filename order, and the size/length
caps all live in **Settings (⚙)**.

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
direction (download queue, extended-mix mode, more output controls). The original
engineering and copyright remain Sunny's, and it's distributed under the **same
license** — see [LICENSE](LICENSE). Original copyright © 2024 Sunny Patel. Thanks
to the original project; this fork builds on top of it.

This is a **student / educational portfolio project**: only use it with content
you own or have permission to download, and in compliance with your local laws
and the source services' terms. See [DISCLAIMER.md](DISCLAIMER.md).

To pull upstream bug-fixes into this fork:

```bash
git remote add upstream https://github.com/sunnypatell/sunnify-spotify-downloader.git  # once
git fetch upstream && git cherry-pick <commit>
```
