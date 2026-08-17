"""Transcribe a video with a local whisper.cpp install (Metal-accelerated).

Extracts mono 16kHz audio via ffmpeg, runs whisper-cli with DTW word-level
alignment, reconstructs word-level timestamps from the token stream, and
writes {"words": [...]} to <edit_dir>/transcripts/<video_stem>.json — same
shape the rest of the pipeline (pack_transcripts.py, timeline_view.py,
render.py) already expects.

Cached: if the output file already exists, transcription is skipped.

No API key, no network call — everything runs on-device via
WHISPER_CPP_BIN / WHISPER_CPP_MODEL (resolved from .env or the environment).

Diarization is optional (whisper.cpp has none built in for mono audio):
pass --diarize to run pyannote.audio as a second pass and assign speaker_id
per word. Requires `pip install -e ".[diarize]"` and an HF_TOKEN in .env.

Usage:
    python helpers/transcribe.py <video_path>
    python helpers/transcribe.py <video_path> --edit-dir /custom/edit
    python helpers/transcribe.py <video_path> --language pt
    python helpers/transcribe.py <video_path> --diarize --num-speakers 2
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# Model filename substring -> whisper.cpp --dtw alignment-heads preset.
# Covers the two models this skill installs; unknown models just skip DTW
# (still works, less precise word boundaries).
DTW_PRESETS = {
    "large-v3-turbo": "large.v3.turbo",
    "large-v3": "large.v3",
}


def _read_env(key: str) -> str | None:
    import os

    for candidate in [Path(__file__).resolve().parent.parent / ".env", Path(".env")]:
        if candidate.exists():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    return os.environ.get(key) or None


def resolve_whisper_config() -> tuple[Path, Path]:
    """Resolve the whisper-cli binary and model path from .env / environment."""
    bin_path = _read_env("WHISPER_CPP_BIN")
    model_path = _read_env("WHISPER_CPP_MODEL")
    if not bin_path or not model_path:
        sys.exit(
            "WHISPER_CPP_BIN and WHISPER_CPP_MODEL must be set in .env or the "
            "environment (see .env.example)"
        )
    bin_path, model_path = Path(bin_path), Path(model_path)
    if not bin_path.exists():
        sys.exit(f"whisper-cli not found: {bin_path}")
    if not model_path.exists():
        sys.exit(f"whisper model not found: {model_path}")
    return bin_path, model_path


def extract_audio(video_path: Path, dest: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _dtw_preset(model_path: Path) -> str | None:
    name = model_path.stem.lower()
    for needle, preset in DTW_PRESETS.items():
        if needle in name:
            return preset
    return None


def _words_from_tokens(transcription: list[dict]) -> list[dict]:
    """Reconstruct word-level {text, start, end} from whisper.cpp's token stream.

    A token starting with a leading space begins a new word (BPE convention);
    tokens without one (punctuation, sub-word continuations) attach to the
    previous word. Word start comes from the DTW-aligned timestamp of its
    first token (`t_dtw`, in centiseconds); word end is interpolated as the
    start of the next word, since DTW gives point estimates, not spans.
    """
    points: list[tuple[str, float]] = []
    for seg in transcription:
        for tok in seg.get("tokens", []):
            text = tok.get("text", "")
            if not text or text.startswith("[_"):
                continue
            t_dtw = tok.get("t_dtw", -1)
            if t_dtw is not None and t_dtw >= 0:
                t = t_dtw / 100.0
            else:
                t = tok.get("offsets", {}).get("from", 0) / 1000.0
            points.append((text, t))

    words: list[dict] = []
    for text, t in points:
        if text.startswith(" ") or not words:
            words.append({"type": "word", "text": text.strip(), "start": t, "end": t, "speaker_id": None})
        else:
            words[-1]["text"] += text

    # ponytail: no real spoken word takes >2s; a longer span means the next
    # "word" is actually the start of the following speech run after a long
    # silence Whisper hallucinated into (single repeated word, e.g. "não. não.
    # não." across a 20s gap — a known Whisper failure mode on quiet audio).
    # Cap the display duration instead of trying to detect/strip hallucinations —
    # the LLM reading takes_packed.md already treats an isolated one-word phrase
    # with a huge gap around it as noise. Upgrade path: real VAD pre-filtering,
    # if whisper.cpp's CLI ever remaps -ojf timestamps through VAD segments.
    MAX_WORD_DURATION = 2.0
    for i in range(len(words) - 1):
        words[i]["end"] = min(words[i + 1]["start"], words[i]["start"] + MAX_WORD_DURATION)
    if words:
        words[-1]["end"] = max(words[-1]["end"], words[-1]["start"] + 0.3)

    return [w for w in words if w["text"]]


def run_whisper(
    audio_path: Path,
    bin_path: Path,
    model_path: Path,
    language: str | None = None,
) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        out_stem = Path(tmp) / "out"
        cmd = [
            str(bin_path),
            "-m", str(model_path),
            "-f", str(audio_path),
            "-l", language or "auto",
            "-ojf", "-of", str(out_stem),
            "-np", "-nt",
        ]
        preset = _dtw_preset(model_path)
        if preset:
            cmd += ["-dtw", preset, "-nfa"]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"whisper-cli failed: {result.stderr[-1000:]}")

        out_json = out_stem.with_suffix(".json")
        raw = json.loads(out_json.read_text())

    words = _words_from_tokens(raw.get("transcription", []))
    return {"words": words, "language": raw.get("result", {}).get("language")}


def assign_speakers(
    words: list[dict],
    audio_path: Path,
    num_speakers: int | None = None,
) -> None:
    """Run pyannote diarization and tag each word's speaker_id in place.

    Requires `pip install -e ".[diarize]"` and HF_TOKEN in .env (accept the
    pyannote/speaker-diarization-3.1 model terms on huggingface.co first).
    """
    from pyannote.audio import Pipeline

    hf_token = _read_env("HF_TOKEN")
    if not hf_token:
        sys.exit("HF_TOKEN not found in .env — required for --diarize (see .env.example)")

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", use_auth_token=hf_token
    )
    diarization = pipeline(str(audio_path), num_speakers=num_speakers)

    turns = [(turn.start, turn.end, speaker) for turn, _, speaker in diarization.itertracks(yield_label=True)]

    for w in words:
        mid = (w["start"] + w["end"]) / 2
        for start, end, speaker in turns:
            if start <= mid <= end:
                w["speaker_id"] = speaker
                break


def transcribe_one(
    video: Path,
    edit_dir: Path,
    language: str | None = None,
    diarize: bool = False,
    num_speakers: int | None = None,
    verbose: bool = True,
) -> Path:
    """Transcribe a single video. Returns path to transcript JSON.

    Cached: returns existing path immediately if the transcript already exists.
    """
    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        if verbose:
            print(f"cached: {out_path.name}")
        return out_path

    bin_path, model_path = resolve_whisper_config()

    if verbose:
        print(f"  extracting audio from {video.name}", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / f"{video.stem}.wav"
        extract_audio(video, audio)
        if verbose:
            print(f"  transcribing {video.stem}.wav ({model_path.name})", flush=True)
        payload = run_whisper(audio, bin_path, model_path, language)
        if diarize:
            if verbose:
                print("  diarizing", flush=True)
            assign_speakers(payload["words"], audio, num_speakers)

    out_path.write_text(json.dumps(payload, indent=2))
    dt = time.time() - t0

    if verbose:
        kb = out_path.stat().st_size / 1024
        print(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s")
        print(f"    words: {len(payload['words'])}")

    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Transcribe a video with local whisper.cpp")
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <video_parent>/edit)",
    )
    ap.add_argument(
        "--language",
        type=str,
        default=None,
        help="Optional ISO language code (e.g., 'pt', 'en'). Omit to auto-detect.",
    )
    ap.add_argument(
        "--diarize",
        action="store_true",
        help="Run pyannote speaker diarization as a second pass (needs [diarize] extra + HF_TOKEN).",
    )
    ap.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Optional number of speakers when known. Only used with --diarize.",
    )
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()

    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        language=args.language,
        diarize=args.diarize,
        num_speakers=args.num_speakers,
    )


if __name__ == "__main__":
    main()
