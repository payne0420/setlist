# `local/combined` — local development branch

This branch is **local-only** (do not push it). It integrates both open PRs so you
can run the app with every in-flight feature at once:

- **PR #47** — `feature/extended-mix-mode`: optional "extended mix" download mode
  (keyword + duration-capped selection, configurable max length, fallback to the
  original, `(Extended Mix)` marker in filename + tag).
- **PR #48** — `feature/filename-artist-order`: optional `Artist - Track` filename
  order (`artist_first`).

The two PRs both edit `Spotify_Downloader.py`, so merging them produces conflicts
that must be reconciled (not just unioned). The reconciliation rules:

1. **Settings/threading** — union every key/param: `extended_mix`,
   `max_extended_minutes`, `artist_first` flow through `load_config` →
   `MusicScraper.__init__` → `ScraperThread.__init__` → `SettingsDialog`
   (checkbox + spin box + checkbox) → `MainWindow`'s `ScraperThread(...)`.
2. **Filenames** — the extended-mix `display_title` (the `(Extended Mix)` marker)
   must flow **through** PR #48's `_format_track_filename(...)` helper so the
   `artist_first` ordering applies to extended downloads too. Also make
   `_resolve_extended_output` build its unmarked fallback name via
   `_format_track_filename` (not an inline f-string) so the fallback rename
   honors `artist_first`.

## Refresh after either PR branch changes

```bash
# 1. enable rerere once so git remembers this resolution next time
git config rerere.enabled true

# 2. recreate the branch from the two updated feature branches
git branch -D local/combined
git checkout feature/extended-mix-mode
git checkout -b local/combined
git merge --no-ff feature/filename-artist-order
#    -> resolve conflicts per the rules above (rerere will auto-apply if recorded)

# 3. verify (clean virtualenv)
python3 -m venv /tmp/sv && /tmp/sv/bin/pip install -q -r req.txt pytest pytest-mock
/tmp/sv/bin/python3 -m pytest tests/test_spotify_downloader.py -q   # expect: all green
rm -rf /tmp/sv
```

Sanity check that both features work together — with `extended_mix` and
`artist_first` both on, a track resolves to e.g.
`Artist - Track (Extended Mix).mp3`.
