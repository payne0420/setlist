#!/usr/bin/env python
"""Audio authenticity + metadata verifier for Setlist E2E outputs.

For every audio file under the given directory, reports:
  - container/codec/sample-rate/bit-depth/declared bitrate (ffprobe)
  - TRUE average bitrate computed from file size / duration
  - spectral energy above 19 kHz (ffmpeg highpass+volumedetect)
  - first-4-bytes magic (fLaC / OggS / ID3)
  - full tag dump incl. cover-art presence (mutagen)

How to read the results (signatures verified live on this project):
  - Genuine librespot capture: vorbis, 44100 Hz, declared 320k, true kbps
    VBR ~260-380 varying per track.
  - Genuine Tidal/Qobuz FLAC: mixed native formats (44.1/16, 48/24, 96/24,
    192/24), true kbps varying widely (550-5500), rich tags + pictures.
  - FAKE lossless (lossy source transcoded into flac/wav): uniform 48000 Hz,
    encoder tag Lavf*, similar ~1300-1700 true kbps, no/sparse tags. NOTE:
    the 19 kHz energy check does NOT catch Opus-sourced fakes (Opus is
    fullband to 20 kHz) — rely on the uniform-48k/Lavf/tag signature and the
    e2e event log's via_youtube_fallback instead.

Usage: ./.venv/bin/python scripts/audio_verify.py <dir> [--json out.json]
"""

import json
import os
import re
import subprocess
import sys

AUDIO_EXT = {".flac", ".ogg", ".mp3", ".m4a", ".opus", ".wav", ".webm"}


def ffprobe(path):
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_name,sample_rate,bit_rate,channels,bits_per_raw_sample:"
            "format=duration,bit_rate,format_name",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"error": out.stderr.strip()[:200]}


def hf_energy(path):
    """mean/max volume of the >19kHz band. ~-91dB mean == digital silence."""
    out = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            path,
            "-af",
            "highpass=f=19000,volumedetect",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
    )
    res = {}
    for key in ("mean_volume", "max_volume"):
        m = re.search(rf"{key}: (-?[\d.]+) dB", out.stderr)
        if m:
            res[key] = float(m.group(1))
    return res


def magic(path):
    with open(path, "rb") as fh:
        return fh.read(4)


def tags(path):
    try:
        import mutagen

        f = mutagen.File(path)
        if f is None:
            return {"error": "mutagen could not parse"}
        info = {}
        for k in sorted(f.keys() if f.tags else []):
            v = f.tags[k]
            sv = str(v)
            if len(sv) > 120:
                sv = sv[:120] + f"...({len(sv)} chars)"
            info[str(k)] = sv
        pics = 0
        try:
            from mutagen.flac import FLAC

            if isinstance(f, FLAC):
                pics = len(f.pictures)
        except Exception:
            pass
        if not pics and f.tags:
            for k in f.tags:
                ks = str(k).upper()
                if "APIC" in ks or "PICTURE" in ks or ks == "COVR":
                    pics += 1
        if (
            not pics
            and hasattr(f, "tags")
            and f.tags
            and "metadata_block_picture" in [str(k).lower() for k in f.tags]
        ):
            pics = 1
        return {
            "tags": info,
            "pictures": pics,
            "length_s": round(getattr(f.info, "length", 0), 1),
            "mutagen_bitrate": getattr(f.info, "bitrate", None),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:200]}


def main():
    root = sys.argv[1]
    results = []
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            ext = os.path.splitext(name)[1].lower()
            if ext not in AUDIO_EXT:
                continue
            path = os.path.join(dirpath, name)
            size = os.path.getsize(path)
            probe = ffprobe(path)
            fmt = probe.get("format", {})
            streams = probe.get("streams", [{}])
            dur = float(fmt.get("duration") or 0) or None
            true_kbps = round(size * 8 / dur / 1000, 1) if dur else None
            rec = {
                "file": os.path.relpath(path, root),
                "size_mb": round(size / 1048576, 2),
                "magic": repr(magic(path)),
                "codec": streams[0].get("codec_name"),
                "sample_rate": streams[0].get("sample_rate"),
                "bits": streams[0].get("bits_per_raw_sample"),
                "channels": streams[0].get("channels"),
                "declared_stream_kbps": round(int(streams[0]["bit_rate"]) / 1000)
                if streams[0].get("bit_rate")
                else None,
                "declared_format_kbps": round(int(fmt["bit_rate"]) / 1000)
                if fmt.get("bit_rate")
                else None,
                "duration_s": round(dur, 1) if dur else None,
                "true_avg_kbps": true_kbps,
                "hf_above_19k": hf_energy(path),
                "meta": tags(path),
            }
            results.append(rec)
            print(json.dumps(rec, indent=1))
    if "--json" in sys.argv:
        out = sys.argv[sys.argv.index("--json") + 1]
        with open(out, "w") as fh:
            json.dump(results, fh, indent=1)
        print(f"wrote {out} ({len(results)} files)")


if __name__ == "__main__":
    main()
