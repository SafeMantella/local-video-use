---
name: local-video-use-install
description: Install local-video-use into the current agent (Claude Code, Codex, Hermes, Openclaw, etc.) and wire up ffmpeg + a local whisper.cpp build so the user can start editing immediately, at zero API cost.
---

# local-video-use install

Use this file only for first-time install or reconnect. For daily editing, read `SKILL.md`. Always read `helpers/` — that's where the scripts live.

## What you're doing

You're setting up a conversation-driven video editor for the user. After install, the user drops raw footage into any folder, runs their agent (`claude`, `codex`, etc.) there, and says "edit these into a launch video." You do the rest by reading `SKILL.md`.

Four things must exist on this machine:

1. The `local-video-use` repo cloned somewhere stable.
2. `ffmpeg` on `$PATH` (plus optional `yt-dlp` for online sources).
3. A `whisper.cpp` build with a GGML model, ideally on a large/external drive if the internal disk is tight — transcription is fully local, no API key, no per-minute cost.
4. `WHISPER_CPP_BIN` and `WHISPER_CPP_MODEL` pointing at that build in `.env` at the repo root.

And one thing must be true about the current agent:

5. It can discover `SKILL.md` — either via a global skills directory (`~/.claude/skills/`, `~/.codex/skills/`) or via a `CLAUDE.md` / system-prompt import.

## Install prompt contract

- Do everything yourself. Only ask the user for things you cannot generate — where to put the whisper.cpp build (internal vs. an external/large drive, if the user has one and disk space is tight), and confirmation before `brew install`.
- Prefer a stable clone path like `~/Developer/local-video-use` (not `/tmp`, not `~/Downloads`).
- The skill references helpers by bare name (`transcribe.py`, `render.py`). That works because SKILL.md and `helpers/` ship together — keep them as siblings when you register the skill.
- After install, verify by running one real command against one real file. Don't declare success on file-existence checks alone.

## Steps

### 1. Clone to a stable path

```bash
test -d ~/Developer/local-video-use || git clone https://github.com/SafeMantella/local-video-use ~/Developer/local-video-use
cd ~/Developer/local-video-use
```

If the repo is already there, `git pull --ff-only` and continue.

### 2. Install Python deps

```bash
# Prefer uv if available; fall back to pip.
command -v uv >/dev/null && uv sync || pip install -e .
```

`pyproject.toml` lists `matplotlib`, `pillow`, `numpy`, plus an optional `diarize` extra (`pyannote.audio`). No console scripts — helpers are invoked directly as `python helpers/<name>.py`.

### 3. Install ffmpeg with zscale support (+ optional yt-dlp)

`ffmpeg` and `ffprobe` are hard requirements, and specifically need `zscale` (needs `libzimg`) — `render.py` unconditionally runs an HDR→SDR tonemap on any HDR source (`is_hdr_source()`, keyed on `color_transfer` being PQ or HLG), which is common on modern phone footage, and that's a correctness rule, not something the EDL can skip. Homebrew's plain `ffmpeg` formula does **not** bundle `zimg` — confirmed by testing against a real HLG phone recording, where `render.py` failed with `No such filter: 'zscale'`. Use `ffmpeg-full` instead (same Homebrew core, bottled, no slow source build):

```bash
# macOS
if ! ffmpeg -filters 2>/dev/null | grep -q zscale; then
    brew install ffmpeg-full
    brew link --overwrite ffmpeg-full   # ffmpeg-full is keg-only; this puts it on PATH system-wide
fi
command -v yt-dlp >/dev/null || brew install yt-dlp     # optional

# Debian / Ubuntu (ensure the build includes --enable-libzimg)
# sudo apt-get update && sudo apt-get install -y ffmpeg
# pip install yt-dlp

# Arch (extra/ffmpeg is built with zimg)
# sudo pacman -S ffmpeg yt-dlp
```

`brew link --overwrite` replaces the system-wide `ffmpeg`/`ffprobe` for every app that shells out to them, not just this skill — tell the user before running it if they didn't already ask for this install, since it's a machine-wide version change (last confirmed: 8.1.1 → 9.0.1), not something scoped to this repo.

If `brew` / `apt` / `pacman` requires a sudo prompt, tell the user the exact command and wait. Do not invent a password.

### 4. Register the skill with the current agent

Figure out which agent you are running under, and register once. A symlink of the whole repo directory is the right shape — helpers/ needs to sit next to SKILL.md.

- **Claude Code** (`~/.claude/` present):

    ```bash
    mkdir -p ~/.claude/skills
    ln -sfn ~/Developer/local-video-use ~/.claude/skills/local-video-use
    ```

- **Codex** (`$CODEX_HOME` set, or `~/.codex/` present):

    ```bash
    mkdir -p "${CODEX_HOME:-$HOME/.codex}/skills"
    ln -sfn ~/Developer/local-video-use "${CODEX_HOME:-$HOME/.codex}/skills/local-video-use"
    ```

- **Hermes / Openclaw / another agent with a skills directory**: symlink `~/Developer/local-video-use` into that agent's skills directory under the name `local-video-use`. If the agent has no skills directory, add a line to its system prompt / config pointing at `~/Developer/local-video-use/SKILL.md` (e.g. an `@~/Developer/local-video-use/SKILL.md` import in a `CLAUDE.md`-equivalent).

If you can't tell which agent you're in, ask the user once: "which agent am I running under — Claude Code, Codex, or something else?" Then pick the right target.

### 5. Build whisper.cpp

Transcription runs fully local via [whisper.cpp](https://github.com/ggml-org/whisper.cpp) — no API key, no per-minute cost. The binary is tiny but GGML models run 500MB–3GB+, so put the whole clone on whichever drive has room (an external drive is fine — everything, binary and models, ends up as siblings inside that one clone directory).

1. Check existing state first — skip straight to step 5.2 (writing `.env`) if this is already done:

    ```bash
    [ -n "$WHISPER_CPP_BIN" ] && [ -x "$WHISPER_CPP_BIN" ] && echo "env: bin ok"
    grep -q '^WHISPER_CPP_BIN=.\+' ~/Developer/local-video-use/.env 2>/dev/null && echo "dotenv: bin set"
    ```

2. If not set up yet, ask the user once where to put it if the internal disk looks tight (`df -h /`) — otherwise default to a stable path like `~/whisper.cpp`. Then clone and build:

    ```bash
    WHISPER_DIR="$HOME/whisper.cpp"   # or the external path the user chose
    git clone https://github.com/ggml-org/whisper.cpp "$WHISPER_DIR"
    cd "$WHISPER_DIR"

    command -v cmake >/dev/null || brew install cmake   # small build tool, fine on internal disk
    cmake -B build -DGGML_METAL=ON   # macOS/Apple Silicon: confirm "Metal framework found" in the output
    cmake --build build -j --config Release
    ```

    On Apple Silicon, Metal must be enabled — check the configure output said `Including METAL backend`. CPU-only inference is much slower and is an anti-pattern (see SKILL.md).

3. Download models into the same clone (lands on whichever drive the clone is on — no separate path config needed). `large-v3` fp16 (~3GB) is the default: on real recordings with quiet passages or background noise, the quantized `large-v3-turbo-q5_0` (~550MB) has been observed to hallucinate — looping a single repeated word across long silence stretches instead of correctly recognizing quieter speech. `large-v3` is ~2x slower but didn't hallucinate on the same test material. Download both if disk space allows, so you can compare on the user's actual footage:

    ```bash
    bash ./models/download-ggml-model.sh large-v3
    bash ./models/download-ggml-model.sh large-v3-turbo-q5_0   # optional, faster, less reliable
    ```

4. Write the resolved paths to `.env` (never commit it — already in `.gitignore`):

    ```bash
    cat >> ~/Developer/local-video-use/.env <<EOF
    WHISPER_CPP_BIN=$WHISPER_DIR/build/bin/whisper-cli
    WHISPER_CPP_MODEL=$WHISPER_DIR/models/ggml-large-v3.bin
    EOF
    chmod 600 ~/Developer/local-video-use/.env
    ```

5. `--diarize` (multi-speaker takes) is optional and needs a second, heavier install (`pyannote.audio`, PyTorch) — only set this up if the user actually records with 2+ people:

    ```bash
    cd ~/Developer/local-video-use && pip install -e ".[diarize]"
    ```

    Then get a Hugging Face token, accept the terms for `pyannote/speaker-diarization-3.1` at https://huggingface.co/pyannote/speaker-diarization-3.1, and append `HF_TOKEN=...` to `.env`. Skip all of this for solo-speaker recordings — `pack_transcripts.py` works fine with no diarization at all.

### 6. Verify end-to-end

Run one real thing. Prefer the lightest verification that still proves the pipeline is wired up:

```bash
python ~/Developer/local-video-use/helpers/timeline_view.py --help >/dev/null && echo "helpers OK"
ffprobe -version | head -1
```

Transcription is free and local now, so a full test at install time is cheap — worth running once against any short sample WAV/MP4 you have handy (or synthesize one with `say`) to confirm `WHISPER_CPP_BIN`/`WHISPER_CPP_MODEL` actually work end to end, not just that the paths exist. The first real run also downloads nothing further and just pays the model-load time (a few seconds).

### 7. Hand off

Tell the user, in one short message:

- Where the skill is installed (`~/Developer/local-video-use`).
- That they should `cd` into their footage folder and start their agent there (e.g. `claude`).
- That a good first message is: *"edit these into a launch video"* or *"inventory these takes and propose a strategy."*
- That all outputs land in `<videos_dir>/edit/` — the repo stays clean.

## Keeping the skill current

- `cd ~/Developer/local-video-use && git pull --ff-only` pulls the latest code. The symlink auto-picks it up on the next run.
- If `pyproject.toml` changed deps, re-run `uv sync` / `pip install -e .` after pulling.

## Cold-start reminders

- Symlink the **whole directory**, not just `SKILL.md`. The helpers need to sit next to it.
- If `.env` exists but `WHISPER_CPP_BIN`/`WHISPER_CPP_MODEL` are empty or point at paths that don't exist, treat it the same as missing — don't assume existence means validity.
- `ffmpeg` from static builds works fine. Any modern (≥ 4.x) build is enough.
- `yt-dlp` is optional. Don't block install on it; install lazily the first time a user asks to pull from a URL.
- Node.js/npm are only needed for HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- HyperFrames, Remotion, and Manim are optional animation engines. Don't install or prefer one globally during setup; pick the engine per animation slot in `SKILL.md`. HyperFrames can run through `npx --yes hyperframes ...` in the slot directory. Remotion can be scaffolded with `npx create-video@latest` or installed inside the slot before rendering.
- Transcription is local and free now — fine to run as part of install verification. It's just slower the first time (model load + first inference).
- If the user is on Linux without a package manager Claude recognizes, print the manual `ffmpeg` install URL and wait rather than guessing.
