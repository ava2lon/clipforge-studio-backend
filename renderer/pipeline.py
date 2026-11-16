"""Speech-led clip selection and accurate FFmpeg extraction."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from fractions import Fraction
from typing import Any, Callable

class PipelineError(RuntimeError):
    pass


_REMOTION_LOCK = threading.Lock()


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
            word_items = []
            for word in segment.get("words", []):
                if word.get("start") is not None and word.get("end") is not None and word.get("word"):
                    word_items.append({"start": float(word["start"]), "end": float(word["end"]),
                                       "text": str(word["word"]).strip()})
            normalized.append({"start": start, "end": end, "text": text, "words": word_items})
    return normalized


def _scene_times(source: Path) -> list[float]:
    try:
        from scenedetect import ContentDetector, detect
        scenes = detect(str(source), ContentDetector(threshold=27.0, min_scene_len=15))
        return [float(start.get_seconds()) for start, _ in scenes[1:]]
    except Exception:
        return []


def _parse_auto_editor_timeline(data: dict[str, Any]) -> list[tuple[float, float]]:
    """Read Auto-Editor's stable v1 chunks; also accept v3 for newer CLI builds."""
    if data.get("chunks") is not None:
        fps = float(Fraction(data.get("timebase", "30/1")))
        if fps <= 0:
            return []
        ranges = []
        for chunk in data["chunks"]:
            if len(chunk) < 3:
                continue
            start, end, speed = float(chunk[0]), float(chunk[1]), float(chunk[2])
            # v1 speed 0/99999 represents a cut; other speeds are retained media.
            if end > start and speed not in (0.0, 99999.0):
                ranges.append((start / fps, end / fps))
        return sorted(ranges)

    fps = float(Fraction(data.get("timebase", "30/1")))
    if fps <= 0:
        return []
    layers = data.get("v") or data.get("a") or []
    ranges = []
    for layer in layers:
        for clip in layer:
            start = float(clip.get("offset", 0)) / fps
            end = start + float(clip.get("dur", 0)) / fps
            if end > start:
                ranges.append((start, end))
    return sorted(ranges)


def _auto_editor_ranges(source: Path, output_dir: Path) -> list[tuple[float, float]]:
    """Run Auto-Editor's audio activity analysis and parse its stable v1 cut list."""
    binary = shutil.which("auto-editor")
    if not binary:
        return []
    timeline = output_dir / "auto-editor-analysis.v1"
    try:
        proc = subprocess.run(
            [binary, str(source), "--edit", "audio:threshold=0.04,stream=all",
             "--export", "v1", "-o", str(timeline)],
            capture_output=True, text=True, timeout=900,
        )
        if proc.returncode != 0 or not timeline.is_file():
            return []
        data = json.loads(timeline.read_text(encoding="utf-8"))
        return _parse_auto_editor_timeline(data)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return []
    finally:
        timeline.unlink(missing_ok=True)

def _active_ratio(start: float, end: float, ranges: list[tuple[float, float]]) -> float:
    if not ranges or end <= start:
        return 0.5
    active = sum(max(0.0, min(end, right) - max(start, left)) for left, right in ranges)
    return min(1.0, active / (end - start))


def _render_with_remotion(source: Path, output: Path, duration: float,
                          captions: list[dict[str, Any]]) -> Path:
    # Remotion's public/clip.mp4, props file and output path are shared by all workers.
    with _REMOTION_LOCK:
        project = Path(__file__).resolve().parents[1] / "remotion"
        package = project / "node_modules" / ".bin" / "remotion"
        if not package.is_file():
            raise PipelineError("Remotion dependencies are not installed in the container")
        public_dir = project / "public"
        public_dir.mkdir(parents=True, exist_ok=True)
        public_media = public_dir / "clip.mp4"
        props_path = project / "clipforge-props.json"
        render_dir = project / "out"
        render_dir.mkdir(parents=True, exist_ok=True)
        rendered = render_dir / "clipforge.mp4"
        try:
            shutil.copy2(source, public_media)
            props_path.write_text(json.dumps({"durationSeconds": duration, "captions": captions}),
                                  encoding="utf-8")
            rendered.unlink(missing_ok=True)
            proc = subprocess.run(
                [str(package), "render", "src/index.ts", "ClipForge", str(rendered),
                 f"--props={props_path}", "--codec=h264", "--concurrency=2", "--overwrite"],
                cwd=project, capture_output=True, text=True, timeout=3600,
            )
            if proc.returncode != 0 or not rendered.is_file() or rendered.stat().st_size == 0:
                raise PipelineError("Remotion render failed: " + (proc.stderr or proc.stdout)[-2500:])
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(rendered), str(output))
            return output
        finally:
            public_media.unlink(missing_ok=True)
            props_path.unlink(missing_ok=True)
            rendered.unlink(missing_ok=True)

def _prompt_tokens(prompt: str | None) -> set[str]:
    if not prompt:
        return set()
    stop = {"a", "an", "and", "are", "as", "at", "be", "best", "clip", "clips",
            "find", "for", "from", "in", "into", "is", "it", "me", "of", "on",
            "or", "please", "show", "that", "the", "this", "to", "video", "with"}
    return {token.lower() for token in re.findall(r"[\\w']+", prompt)
            if len(token) > 2 and token.lower() not in stop}


def _candidate_score(text: str, start: float, end: float, scenes: list[float],
                     active_ranges: list[tuple[float, float]], prompt_terms: set[str]):
    tokens = [token.lower() for token in re.findall(r"[\\w']+", text)]
    density = len(tokens) / max(end - start, 1.0)
    speech = min(20.0, density / 3.0 * 20.0)
    hook_patterns = (r"\\?", r"\\b(how|why|secret|mistake|never|stop|truth|surprising|shocking|imagine|watch|listen)\\b",
                     r"\\b(you won'?t believe|this is what|the biggest|the most important)\\b")
    hook = min(20.0, sum(bool(re.search(x, text, re.I)) for x in hook_patterns) * 8.0)
    weak_open = bool(re.match(r"^(and|but|so|because|then|they|he|she|it|this|that)\\b", " ".join(tokens[:8])))
    context = max(0.0, (15.0 if len(tokens) >= 12 else 9.0) - (5.0 if weak_open else 0.0))
    ending = 8.0 + (3.0 if re.search(r"[.!?][\\\"'”’)]*$", text.strip()) else 0.0)
    if re.search(r"\\b(therefore|that'?s why|in conclusion|the point is|so remember)\\b", text, re.I):
        ending += 4.0
    if re.search(r"\\b(and then|but anyway|to be continued)\\s*$", text, re.I):
        ending -= 4.0
    ending = max(0.0, min(15.0, ending))
    scene = 10.0 if any(abs(point - start) <= 2.0 for point in scenes) else 0.0
    audio = 5.0 if not active_ranges else _active_ratio(start, end, active_ranges) * 10.0
    matched = set(tokens).intersection(prompt_terms)
    relevance = min(10.0, 10.0 * len(matched) / max(1, len(prompt_terms))) if prompt_terms else 5.0
    factors = {"speech_density": round(speech, 2), "hook_strength": round(hook, 2),
               "context_completeness": round(context, 2), "ending_strength": round(ending, 2),
               "scene_alignment": round(scene, 2), "audio_activity": round(audio, 2),
               "prompt_relevance": round(relevance, 2)}
    score = round(sum(factors.values()), 1)
    reasons = [name.replace("_", " ") for name, value in factors.items() if value >= 7.0]
    match_ratio = len(matched) / max(1, len(prompt_terms)) if prompt_terms else 1.0
    return score, factors, reasons, match_ratio


def _rank_candidates(segments: list[dict[str, Any]], scenes: list[float],
                     duration: float, active_ranges: list[tuple[float, float]] | None = None,
                     prompt: str | None = None) -> list[dict[str, Any]]:
    min_len = float(os.getenv("CLIP_MIN_SECONDS", "20"))
    max_len = float(os.getenv("CLIP_MAX_SECONDS", "60"))
    max_clips = max(1, int(os.getenv("CLIP_MAX_COUNT", "8")))
    if min_len <= 0 or max_len < min_len or duration <= 0:
        return []
    prompt_terms = _prompt_tokens(prompt)
    words = []
    for seg in segments:
        tokens = re.findall(r"[\\w']+", str(seg.get("text", "")))
        if tokens and float(seg.get("end", 0)) > float(seg.get("start", 0)):
            words.append({**seg, "tokens": tokens})
    words.sort(key=lambda x: (float(x["start"]), float(x["end"])))
    candidates = []
    for left in range(len(words)):
        start = max(0.0, float(words[left]["start"]) - 1.0)
        text_parts = []
        for right in range(left, len(words)):
            segment = words[right]
            if float(segment["end"]) - start > max_len:
                break
            text_parts.append(str(segment["text"]))
            end = min(duration, float(segment["end"]) + 0.8, start + max_len)
            if end - start < min_len:
                continue
            text = " ".join(text_parts).strip()
            score, factors, reasons, match_ratio = _candidate_score(
                text, start, end, scenes, active_ranges or [], prompt_terms)
            if prompt_terms and match_ratio == 0:
                continue
            caption_items = []
            for part in words[left:right + 1]:
                part_words = part.get("words") or [
                    {"start": part["start"], "end": part["end"], "text": part["text"]}]
                for word in part_words:
                    if word.get("start") is None or word.get("end") is None:
                        continue
                    ws, we = float(word["start"]), float(word["end"])
                    if we > start and ws < end:
                        caption_items.append({"start": round(max(0.0, ws-start), 3),
                                              "end": round(max(0.0, min(end, we)-start), 3),
                                              "text": str(word["text"])})
            candidates.append({"start": round(start, 3), "end": round(end, 3),
                "duration": round(end-start, 2), "score": score, "virality_score": score,
                "score_factors": factors, "score_reasons": reasons,
                "prompt_match": round(match_ratio, 3),
                "active_ratio": round(_active_ratio(start, end, active_ranges or []), 3),
                "transcript": text[:3000], "captions": caption_items})
    candidates.sort(key=lambda x: (-x["score"], x["start"], x["end"]))
    selected = []
    for candidate in candidates:
        if any(candidate["start"] < item["end"] and candidate["end"] > item["start"] for item in selected):
            continue
        selected.append(candidate)
        if len(selected) >= max_clips:
            break
    return sorted(selected, key=lambda x: x["start"])

def analyze_and_extract(source: Path, output_dir: Path, media: dict[str, Any],
                        progress: Callable[[int, str], None] = lambda *_: None,
                        clip_prompt: str | None = None) -> list[dict[str, Any]]:
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
    progress(25, "Analyzing speech activity with Auto-Editor")
    active_ranges = _auto_editor_ranges(source, output_dir)
    progress(30, "Transcribing speech with WhisperX")
    segments = _transcribe(source)
    if not segments:
        raise PipelineError("WhisperX found no usable speech segments")
    progress(72, "Ranking explainable clip candidates")
    candidates = _rank_candidates(segments, scenes, duration, active_ranges, clip_prompt)
    if not candidates:
        raise PipelineError("No candidate segments fit the configured clip duration range")
    results = []
    for index, candidate in enumerate(candidates, 1):
        name = f"clip-{index:02d}.mp4"
        raw_path = extract_clip(source, output_dir / f"raw-{name}", candidate["start"], candidate["end"])
        path = _render_with_remotion(raw_path, output_dir / name, candidate["duration"],
                                     candidate.get("captions", []))
        raw_path.unlink(missing_ok=True)
        results.append({**candidate, "name": name, "path": str(path),
                        "scene_detection": "PySceneDetect",
                        "silence_analysis": "Auto-Editor" if active_ranges else "no_ranges_or_unavailable"})
        progress(72 + int(18 * index / len(candidates)), f"Rendering clip {index}/{len(candidates)} with FFmpeg")
    return results
