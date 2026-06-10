# Feature workflow — build, verify, ship without regressions

The standing process for landing changes in Setlist. Distilled from the two-backend
landing of 2026-06 (librespot 320k OGG + Real FLAC), where the unit suite was fully
green while a live run still caught a real bug (the backend seam's `audio_format`
kwarg was silently ignored). The core principle:

> **A green unit suite is necessary, never sufficient. Anything a mock sits in front of
> must also be proven live, on real files, with real bytes.**

Roles when agents are involved: one agent **plans/orchestrates/verifies**, a second
independent agent **implements** from an explicit written brief, a third independent
agent **reviews** adversarially. No agent reviews its own work.

All commands below run **from the `setlist` repo root** with the project venv
(`./.venv/bin/python`). Note: local venv is Python 3.13; CI tests on 3.11 — a local
pass does not excuse waiting for CI.

---

## Fast path (small changes)

Use the full workflow for anything touching: the backend seam / `fetch()`, audio
bytes or formats, metadata tag writing, download-source selection, resume/manifest
logic, config keys / Settings UI, or rate-limit/session behavior.

Otherwise scale down:
- **Docs/comments-only**: Phase 2's marker scan + `git diff --check` + CI.
- **Tests-only**: Phase 2 in full (the arithmetic check matters most here).
- **Small code change outside the protected areas**: Phases 1–3 + the single E2E cell
  closest to the change, skip the rest of the matrix.

## Phase 0 — Spec before code

- Write a goal/spec doc before any implementation: scope, exact invariants, intentional
  divergences (things that must NOT be "fixed"), expected test-count arithmetic
  (current baseline + planned additions), and acceptance criteria.
- List hazards that will NOT surface mechanically (e.g. git 3-way merges apply two
  same-named method insertions with **no conflict marker** — later def silently wins).
- Pin environmental constraints so nobody "fixes" them mid-flight (the one-attempt-only
  `/v1/search` rule, Spotify session serialization, loopback-only Tidal URLs, etc.).

## Phase 1 — Implement from a written brief

- The implementation brief is a file, not a chat message: hard constraints (which files
  may change, "test additions only — never edit a test to make it pass", no commits, no
  pushes), the verification loop to iterate on, and the exact gate command.
- Implementer iterates until its own gate is green. The orchestrator then re-verifies
  independently — never trust the implementer's report alone.

## Phase 2 — Mechanical gates (after every implementation pass)

```bash
git status --short                          # know exactly what changed, incl. untracked
git ls-files --others --exclude-standard    # untracked files that would be missed below
git grep -nE '^(<{7} |={7}$|>{7} )' -- '*.py' '*.txt' '*.spec' '*.md'   # conflict markers, anchored (docs legitimately contain ===== separator lines; plain grep wades into .venv)
git ls-files '*.py' | xargs ./.venv/bin/python scripts/dup_method_check.py
git diff --check && git diff --cached --check
./.venv/bin/python -m pytest tests/ -q     # 0 failures
ruff check . && ruff format --check .      # CI runs these; fail them locally first
```

- **Reconcile the test-count arithmetic**: collected count must equal the spec's
  baseline + additions. A "passing" suite with fewer collected tests than expected
  means tests were silently lost — that is a failure even at 0 red. (Don't trust a
  remembered number; measure the baseline on the base commit.)
- Full-suite prerequisites in `.venv`: `pyotp`, `cryptography`, and the pinned
  `librespot` package (one strict-picker test `importorskip`s it; without it the test
  silently skips and the count shifts by one).
- If `web-app/` was touched, also run its lint/typecheck/build (CI does).

## Phase 3 — Independent adversarial review

- A second model reviews the diff read-only against the spec (e.g.
  `codex exec -s read-only -m gpt-5.5 -c model_reasoning_effort=xhigh`), briefed to be
  adversarial: per-invariant verification, lost-hunk sweep (side-authored files must be
  byte-identical to their source), silent-shadowing sweep, "did anything get 'improved'
  that the spec says to leave alone". Loop until APPROVE.
- Only review a **stable tree** — reviews of a moving tree produce stale findings.
  Re-check any REQUEST-CHANGES claim against the code on disk before acting on it
  (reviewers are sometimes wrong; code + a green gate outrank an unverified claim).

## Phase 4 — Config & UI wiring (BEFORE live runs)

Do this before the E2E matrix: live driver runs pass kwargs directly, so they will
happily pass while GUI wiring is broken.

- Every new config key needs the full chain: `load_config` default + validation/
  coercion → `MainWindow` reads it → `ScraperThread` → `MusicScraper` → backend →
  `SettingsPanel` control + enable/grey sync + persistence.
- Verify with an offscreen `QApplication` harness driving the REAL `SettingsPanel`:
  defaults, coercion of garbage config values, dropdown contents/order, card syncs on
  EVERY path (including dialog-decline revert paths), persistence of each toggle,
  secret fields use password echo. **Always run with a fake `HOME`** so the harness
  can't clobber the real `~/Library/Application Support/Setlist/config.json`.
- Finish with an offscreen `MainWindow()` construction smoke test.

## Phase 5 — Live E2E matrix (the part mocks cannot cover)

**Prerequisites — check before burning time on runs:**

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN || (cd ../hifi-api && ./start.sh)   # Tidal leg
curl -s -m 3 http://127.0.0.1:8000/ | head -c 80                     # expect a version banner
ls ~/Library/Application\ Support/Setlist/librespot/credentials.json # librespot session
ffmpeg -hide_banner -encoders | grep -E "libmp3lame|vorbis|flac"     # transcode encoders
```

- **Serialize Spotify-session runs.** Two concurrent processes logging into one account
  (librespot source, or any run whose Mercury metadata service has stored credentials)
  cause transient `BadCredentials`. One session-opening run at a time. A first-attempt
  login failure that succeeds on retry is environmental; a repeatable one is a bug.
- Infra findings are findings (2026-06 state: Amazon resolver DNS-dead, Qobuz community
  frontends returning not-found, host ffmpeg without `libvorbis` → ogg-format YouTube
  fallbacks fail cleanly). Record them; don't paper over them.

Canonical test playlist: `https://open.spotify.com/playlist/36VYomlRUyF3uyNdWR5qFP`
(16 well-known tracks, several 10+ min — exercises duration guards, hi-res sources,
catalog misses).

Skeleton command (every row below = this + the row's extra flags, fresh `--out` per run):

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/python scripts/e2e_driver.py \
  --url "https://open.spotify.com/playlist/36VYomlRUyF3uyNdWR5qFP" \
  --out /tmp/e2e/<run-name> --artist-first --source <source> [flags...]
```

| Run | Extra flags | Must observe |
|---|---|---|
| YouTube baseline | `--source youtube --format mp3 --quality 320` | 16/16 honest 320 CBR mp3, full ID3 (TRCK/TPOS need the Mercury metadata service) |
| librespot native | `--source librespot --format ogg` | vorbis 44.1k, VBR ~260–380 around declared 320, protobuf tags + cover |
| librespot resume | rerun the same `--out` | manifest skips done tracks, fetches only missing |
| librespot extended | `--source librespot --format ogg --extended --librespot-extended-yt-fallback --client-id <ID> --client-secret <SECRET>` | client-credentials search (no 429), strict-title YT extended or clean per-track failure |
| lossless via Tidal | `--source lossless --format flac --tidal-api-url http://127.0.0.1:8000` | genuine FLAC at native mixed formats (44.1/16 … 192/24), provider tags + art |
| lossless fallback | `--source lossless --format flac` with services dead/absent | honest **mp3 320** + "MP3 320k, not lossless" status + `via_youtube_fallback: true` — never flac/wav from a lossy source |
| pure lossless | add `--no-lossless-yt-fallback`, services failing | every track fails loudly, **zero files** |
| metadata A/B | one track, `--flac-metadata-source spotify` vs `provider` | tags switch release (provider = same release as the bytes) |

(Read client id/secret from the app config with python; never echo the secret.)

## Phase 6 — Artifact verification (bitrates and metadata must be TRUE)

```bash
./.venv/bin/python scripts/audio_verify.py /tmp/e2e/<run> --json /tmp/e2e/<run>-verify.json
```

`audio_verify.py` is a **reporter, not a judge** — it never fails on its own. You (or
the orchestrating agent) must assert, per run:

1. **File count** == expected tracks (cross-check `_e2e_events.json` `done`/`count`
   events and any `Error downloading` statuses).
2. **Codec/container** matches the run's promise (vorbis for librespot, flac for
   Tidal-served, mp3 for fallbacks — `magic` column agrees: `OggS`/`fLaC`/`ID3`).
3. **True bitrate** in the genuine range (see signatures in the script docstring).
   Fake-lossless signature: uniform 48 kHz + `Lavf` encoder + sparse tags. The 19 kHz
   spectral check does NOT catch Opus-sourced fakes — use the signature set plus the
   event log's `via_youtube_fallback`.
4. **Tags**: title/artist/album/tracknumber/discnumber/date present and CORRECT
   (spot-check track numbers against the real release); **pictures ≥ 1**. Per format:
   FLAC vorbis-comments + pictures; OGG vorbis-comments + `metadata_block_picture`
   (macOS Finder won't render it — player limitation, verify bytes); MP3 ID3 + APIC.
5. **Fallback provenance**: every `via_youtube_fallback: true` file is an honest lossy
   container; every genuine-source file is not.

## Phase 7 — Pre-push audit, then push

- Audit the FULL push payload — `git log -p <base>..HEAD` *plus* every merge commit's
  combined diff (`git show <merge-sha>`; `log -p` omits them) — since content
  added-then-removed and evil-merge hunks still get published. Multi-lens: credential
  patterns, personal/machine data (incl. `/Users/<name>` paths — agent-authored `.md`
  docs love absolute paths), file inventory per commit (no caches/credentials/`.pyc`/
  binaries with personal metadata), shipped defaults all empty/safe. An adversarial
  verifier re-greps the payload to falsify "clean" claims.
- Known-deliberate public constants (the SpotiFLAC snapshot set in
  `lossless/constants.py` and `.ground-truth/`) are allowlisted — do not "fix" them.
- Scrub findings by **history rewrite before pushing** (amend/commit-tree surgery),
  never with an after-the-fact commit — the old content would still be in the pushed
  history. Verify the rewrite changed only the intended lines, then re-run the suite.
- Push only to `origin`, only with explicit user approval. `upstream` is read-only
  forever. After pushing, watch GitHub Actions (tests on 3.11 + ruff + webclient).

## After a failure — what to re-run

| Failure found in | Fix lands in | Re-run |
|---|---|---|
| Phase 2/3 (gates, review) | code/tests | Phase 2 → 3 |
| Phase 4 (config/UI) | wiring/UI | Phase 4, plus any E2E cell controlled by that setting |
| Phase 5/6 (live, artifacts) | backend/seam/metadata code | Phase 2 → 3, then the affected E2E cells + Phase 6 on their outputs |
| Phase 7 (audit) | history rewrite | payload re-sweep + full suite, then push |

## Pinned lessons (why each phase exists)

1. **Mocks lie at seams.** `YouTubeBackend.fetch` ignored its `audio_format` kwarg; the
   whole unit suite passed while live fallbacks produced fake FLAC. Live runs are the gate.
2. **Test-count arithmetic catches silent loss** the suite itself can't.
3. **Same-name defs merge without conflict markers** — `dup_method_check.py` after every
   merge or large agent-authored change.
4. **Concurrent logins on one Spotify account fail spuriously** — serialize.
5. **`/v1/search` keymaster retries escalate to ~24h bans** — fail-fast is load-bearing;
   never add retries there.
6. **Verify reviewer claims against the code** — an xhigh review still mis-read a test
   that did stub the selector.
7. **Local paths leak via committed docs** — sweep `.md` files too, not just code.
8. **Driver args ≠ GUI wiring** — config/UI verification must precede live runs, or the
   matrix proves nothing about what users actually toggle.
9. **Agent-authored code ships unformatted** — the 2026-06 landing pushed code that
   failed CI's `ruff format --check` despite 438 green tests. Run the ruff pair locally
   before every commit; check Actions after every push.
