# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This is a Claude Code **skill** — not a conventional application. `SKILL.md` (frontmatter `name: local-video-use`) is the entrypoint an agent reads when a user asks it to edit video; it defines a conversation-driven editing workflow (transcribe → propose a cut strategy → confirm with the user → render → self-evaluate). `install.md` is a separate, one-time setup doc (clone, deps, ffmpeg, whisper.cpp, skill registration) — it is not meant to be re-read every session. `helpers/` holds the actual Python scripts SKILL.md's workflow calls out to.

When making code changes here, you are almost always editing `helpers/*.py` in service of the process SKILL.md describes — read the relevant section of SKILL.md before changing a helper's behavior, since SKILL.md encodes *why* the code is shaped the way it is (see "Hard Rules" below).

There is no build step, no lint config, and no automated test suite in this repo — verification is manual: run the helper against a real or synthetic clip and inspect the output (JSON, PNG, or rendered MP4).

## Commands

```bash
# Install Python deps (editable install; no console scripts — helpers are always invoked directly)
pip install -e .                    # or: uv sync
pip install -e ".[diarize]"         # optional: pyannote.audio, for transcribe.py --diarize

# ffmpeg MUST be ffmpeg-full, not plain ffmpeg — render.py's HDR tonemap needs zscale (libzimg),
# which the plain Homebrew ffmpeg formula doesn't bundle.
brew install ffmpeg-full && brew link --overwrite ffmpeg-full

# Run helpers directly against a video (all take a video/EDL path; --edit-dir defaults to <video_parent>/edit)
python helpers/transcribe.py <video.mp4> --language pt          # single-file transcription (whisper.cpp, local)
python helpers/transcribe_batch.py <videos_dir>                 # batch transcription, 1 worker by default (GPU-bound)
python helpers/pack_transcripts.py --edit-dir <edit_dir>         # transcripts/*.json -> takes_packed.md
python helpers/timeline_view.py <video> <start> <end> -o out.png # filmstrip + waveform PNG, on-demand
python helpers/render.py <edl.json> -o out.mp4 --preview --build-subtitles
python helpers/grade.py <in.mp4> -o out.mp4 --preset warm_cinematic
python helpers/grade.py --list-presets                           # inspect presets without touching a file
```

No test suite exists. To validate a change to a helper, run it against real footage (or synthesize a quick test clip with macOS `say` + `ffmpeg -f lavfi -i color=black`) and check the produced JSON/PNG/MP4 directly.

## Architecture

### The pipeline and its shared contract

The helpers are independent CLI scripts, not a library — they communicate entirely through files on disk, keyed by naming convention, not through Python imports (except `transcribe_batch.py` importing `transcribe_one` from `transcribe.py`, and `render.py` importing `grade.py`'s presets). The two contracts every helper agrees on:

1. **Transcript JSON** — `<edit_dir>/transcripts/<video_stem>.json`, shape `{"words": [{"type": "word", "text": ..., "start": ..., "end": ..., "speaker_id": ...}], "language": ...}`. Produced by `transcribe.py`; read by `pack_transcripts.py`, `timeline_view.py`, and `render.py`'s subtitle builder. If you change this shape, all three readers need updating together.
2. **EDL JSON** — the cut list (`sources`, `ranges`, `grade`, `overlays`, `subtitles`) that `render.py` consumes to actually assemble a video. Format is documented in SKILL.md's "EDL format" section.

Normal flow: `transcribe_batch.py` → `pack_transcripts.py` (produces `takes_packed.md`, the primary artifact an editing agent reads to pick cuts) → an LLM builds an EDL → `render.py` → `grade.py` (invoked internally by `render.py` per-segment, or standalone).

### Local transcription (no external API)

`transcribe.py` shells out to a locally-built `whisper-cli` (whisper.cpp, Metal-accelerated) rather than calling any hosted API — there is no API key anywhere in this pipeline. Config (`WHISPER_CPP_BIN`, `WHISPER_CPP_MODEL`, optional `HF_TOKEN` for diarization) comes from `.env` at the repo root, resolved relative to `transcribe.py`'s own file location — not the caller's cwd — so it works when the skill is invoked from any directory. Two things worth knowing before touching `run_whisper()`:

- Word-level timestamps come from whisper.cpp's `-ojf` token stream reconstructed into words, using DTW-aligned (`-dtw <preset>`) per-token timestamps, not the coarser `-oj`/segment offsets — DTW requires `-nfa`/`--no-flash-attn`, since flash-attention silently disables it otherwise.
- `--vad` is deliberately never passed: whisper.cpp's CLI JSON exporter reads raw (VAD-compressed-timeline) token times, not the timeline-remapped ones its own C API exposes, so `--vad` would silently desync every timestamp from the real video.

Diarization (`--diarize`) is a separate, optional pass (`pyannote.audio`, lazy-imported) that runs after transcription and assigns `speaker_id` per word by proximity to detected speaker turns — it's not built into the base transcription call.

### Render pipeline order (Hard Rules in SKILL.md)

`render.py` enforces a specific, non-negotiable operation order — deviating produces silent visual/audio defects, not crashes:

1. Per-segment extract (grade + 30ms audio fades baked in per-clip) → lossless `-c copy` concat. Never a single-pass filtergraph once overlays exist (forces a double re-encode).
2. HDR→SDR tonemap (`is_hdr_source()` check, unconditional for PQ/HLG sources) happens before any grade filter, per-segment.
3. Overlays composited with a PTS shift so each overlay's frame 0 lands at its window start.
4. Subtitles burned in **last**, after every overlay — otherwise overlays visually hide the captions.
5. Master SRT timestamps are computed on the *output* timeline (`word.start - segment_start + segment_offset`), not the source timeline.

### Vendored sub-skill

`skills/manim-video/` is a separate, self-contained skill (Manim diagram/animation generation) referenced by SKILL.md for the optional Animations workflow. It has its own `SKILL.md` and is unrelated to the transcription/render pipeline.

### Output isolation

Session outputs (EDLs, transcripts, rendered clips) belong in `<videos_dir>/edit/` — the user's footage folder — never inside this repo. `.gitignore` has a safety-net list of the exact directory/file names the pipeline produces (`edit/`, `transcripts/`, `takes_packed.md`, `preview.mp4`, etc.) in case a helper is ever accidentally invoked from inside the repo itself.
