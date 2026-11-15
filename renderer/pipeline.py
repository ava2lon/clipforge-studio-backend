"""Speech-led clip selection and accurate FFmpeg extraction."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

class PipelineError(RuntimeError):
    pass


def probe_media(media_path: str | Path) -> dict[str, Any]:
    path = Path(media_path).resolve()
    if not path.is_file():
        raise PipelineError(f"Input media does not exist: {path}")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise PipelineError("FFprobe is required but was not found")
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=45,
    )
    if proc.returncode:
        raise PipelineError(proc.stderr[-2000:] or "Media probe failed")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError("FFprobe returned invalid JSON") from exc


def extract_clip(source: str | Path, output: str | Path, start: float, end: float) -> Path:
    src, dst = Path(source).resolve(), Path(output).resolve()
    if not src.is_file():
        raise PipelineError("Source media does not exist")
    if start < 0 or end <= start:
        raise PipelineError("Clip boundaries must satisfy 0 <= start < end")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PipelineError("FFmpeg is required but was not found")
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.3f}",
         "-i", str(src), "-t", f"{end-start:.3f}", "-map", "0:v:0?", "-map", "0:a:0?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)],
        capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode or not dst.is_file() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise PipelineError(proc.stderr[-3000:] or "FFmpeg extraction failed")
    return dst


def _transcribe(source: Path) -> list[dict[str, Any]]:
    try:
        import whisperx
        import torch
    except Exception as exc:
        raise PipelineError(f"WhisperX dependencies could not be imported: {exc}") from exc
    device = os.getenv("WHISPER_DEVICE", "cuda" if torch.cuda.is_available() else "cpu")
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE", "float16" if device == "cuda" else "int8")
    model = whisperx.load_model(os.getenv("WHISPER_MODEL", "small"), device, compute_type=compute_type)
    audio = whisperx.load_audio(str(source))
    result = model.transcribe(audio, batch_size=int(os.getenv("WHISPER_BATCH_SIZE", "4")))
    segments = result.get("segments", [])
    language = result.get("language")
    if segments and language:
        try:
            align_model, metadata = whisperx.load_align_model(language_code=language, device=device)
            aligned = whisperx.align(segments, align_model, metadata, audio, device,
                                     return_char_alignments=False)
            segments = aligned.get("segments", segments)
        except Exception:
            # Segment timestamps remain usable when forced alignment is unavailable.
            pass
    normalized = []
    for segment in segments:
        start, end = float(segment.get("start", 0)), float(segment.get("end", 0))
        text = str(segment.get("text", "")).strip()
        if end > start and text:
            normalized.append({"start": start, "end": end, "text": text})
    return normalized


def _scene_times(source: Path) -> list[float]:
    try:
        from scenedetect import ContentDetector, detect
        scenes = detect(str(source), ContentDetector(threshold=27.0, min_scene_len=15))
        return [float(start.get_seconds()) for start, _ in scenes[1:]]
    except Exception:
        return []


def _auto_editor_check(source: Path) -> bool:
    binary = shutil.which("auto-editor")
    if not binary:
        return False
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def _rank_candidates(segments: list[dict[str, Any]], scenes: list[float],
                     duration: float) -> list[dict[str, Any]]:
    min_len = float(os.getenv("CLIP_MIN_SECONDS", "20"))
    max_len = float(os.getenv("CLIP_MAX_SECONDS", "60"))
    max_clips = int(os.getenv("CLIP_MAX_COUNT", "8"))
    words = []
    for seg in segments:
        tokens = re.findall(r"[\w']+", seg["text"])
        if tokens:
            words.append({**seg, "tokens": tokens})
    candidates = []
    for left in range(len(words)):
        start = max(0.0, words[left]["start"] - 1.0)
        text_parts = []
        last_end = start
        for right in range(left, len(words)):
            seg = words[right]
            if seg["end"] - start > max_len:
                break
            text_parts.append(seg["text"])
            last_end = seg["end"]
            span = last_end - start
            if span < min_len:
                continue
            text = " ".join(text_parts).strip()
            count = sum(len(re.findall(r"[\w']+", part)) for part in text_parts)
            density = count / max(span, 1)
            hook = 1.0 if re.search(r"\?|\b(how|why|secret|mistake|never|best|worst|truth)\b", text, re.I) else 0.0
            scene_bonus = 0.15 if any(abs(point-start) <= 2 for point in scenes) else 0.0
            score = min(1.0, density / 3.0) * 0.65 + hook * 0.2 + scene_bonus
            end = min(duration, last_end + 0.8, start + max_len)
            candidates.append({"start": round(start, 3), "end": round(end, 3),
                               "duration": round(end-start, 2),
                               "score": round(score, 4), "transcript": text[:3000]})
            break
    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected = []
    for candidate in candidates:
        if any(candidate["start"] < item["end"] and candidate["end"] > item["start"] for item in selected):
            continue
        selected.append(candidate)
        if len(selected) >= max_clips:
            break
    return sorted(selected, key=lambda item: item["start"])


def analyze_and_extract(source: Path, output_dir: Path, media: dict[str, Any],
                        progress: Callable[[int, str], None] = lambda *_: None) -> list[dict[str, Any]]:
    duration = float(media.get("format", {}).get("duration", 0))
    if duration <= 0:
        raise PipelineError("Media duration is missing or invalid")
    streams = media.get("streams", [])
    if not any(stream.get("codec_type") == "video" for stream in streams):
        raise PipelineError("Input must contain a video stream")
    if not any(stream.get("codec_type") == "audio" for stream in streams):
        raise PipelineError("Input has no audio stream; speech-led clipping requires audio")
    progress(18, "Detecting scene boundaries with PySceneDetect")
    scenes = _scene_times(source)
    progress(25, "Checking Auto-Editor availability")
    auto_editor_available = _auto_editor_check(source)
    progress(30, "Transcribing speech with WhisperX")
    segments = _transcribe(source)
    if not segments:
        raise PipelineError("WhisperX found no usable speech segments")
    progress(72, "Ranking transcript-led clip candidates")
    candidates = _rank_candidates(segments, scenes, duration)
    if not candidates:
        raise PipelineError("No candidate segments fit the configured clip duration range")
    results = []
    for index, candidate in enumerate(candidates, 1):
        name = f"clip-{index:02d}.mp4"
        path = extract_clip(source, output_dir / name, candidate["start"], candidate["end"])
        results.append({**candidate, "name": name, "path": str(path),
                        "scene_detection": "PySceneDetect",
                        "auto_editor_available": auto_editor_available})
        progress(72 + int(18 * index / len(candidates)), f"Rendering clip {index}/{len(candidates)} with FFmpeg")
    return results
