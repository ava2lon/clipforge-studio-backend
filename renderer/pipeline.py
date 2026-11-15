"""Orchestration boundary for the ClipForge processing stack.

Keep each stage isolated so model loading, GPU constraints and subprocess failures
can be handled without taking down the API process.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any


class PipelineError(RuntimeError):
    pass


def probe_media(media_path: str | Path) -> dict[str, Any]:
    path = Path(media_path).resolve()
    if not path.is_file():
        raise PipelineError(f"Input media does not exist: {path}")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise PipelineError("FFprobe is required but was not found")
    import json
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=45,
    )
    if proc.returncode != 0:
        raise PipelineError(proc.stderr[-2000:] or "Media probe failed")
    return json.loads(proc.stdout)


def extract_clip(source: str | Path, output: str | Path, start: float, end: float) -> Path:
    """Accurately re-encode a clip; stream-copy is intentionally not the default."""
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
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", str(start),
         "-i", str(src), "-t", str(end - start), "-map", "0:v:0?", "-map", "0:a:0?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(dst)],
        capture_output=True, text=True, timeout=1800,
    )
    if proc.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise PipelineError(proc.stderr[-3000:] or "FFmpeg extraction failed")
    return dst
