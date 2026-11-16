"""Persistent job runner for a single-container Space deployment."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from renderer.job_store import JobStore
from renderer.pipeline import PipelineError, analyze_and_extract, probe_media
from renderer.security import is_allowed_supabase_storage_url
from renderer.ingestion import download_direct_mp4, download_social_video, ensure_supported_ingestion_source

_MAX_WORKERS = max(1, int(os.getenv("CLIPFORGE_MAX_WORKERS", "1")))
_MAX_ACTIVE_JOBS = _MAX_WORKERS * 3
_MAX_DOWNLOAD_BYTES = int(os.getenv("CLIPFORGE_MAX_SOURCE_BYTES", str(500 * 1024 * 1024)))
_RETENTION_SECONDS = max(3600, int(os.getenv("CLIPFORGE_JOB_RETENTION_SECONDS", str(7 * 24 * 60 * 60))))
_CLEANUP_INTERVAL_SECONDS = max(300, int(os.getenv("CLIPFORGE_CLEANUP_INTERVAL_SECONDS", "3600")))
_WORKDIR = Path(os.getenv("CLIPFORGE_WORKDIR", "/tmp/clipforge")).resolve()
_WORKDIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = Path(os.getenv("CLIPFORGE_DB_PATH", str(_WORKDIR / "clipforge.sqlite3")))
_STORE = JobStore(_DB_PATH)
# Source URLs are deliberately not stored in SQLite: signed URLs are bearer credentials.
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="clipforge")
_RENDER_LOCK = threading.RLock()


def get_job(job_id: str) -> dict[str, Any] | None:
    return _STORE.get(job_id)


def job_artifact(job_id: str) -> Path | None:
    return _STORE.artifact(job_id)


def create_job(url: str, workdir: Path, clip_prompt: str | None = None, aspect_ratio: str = "9:16",
               caption_style: str = "karaoke", caption_position: str = "bottom", audio_cleanup: bool = True,\n               transition: str = "none", transition_duration: float = 0.5) -> str:
    job_id = uuid.uuid4().hex
    job_dir = workdir / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    try:
        options_tmp = job_dir / "clipforge-options.json.part"
        options_tmp.write_text(json.dumps({
            "clip_prompt": (clip_prompt or "").strip()[:500],
            "aspect_ratio": aspect_ratio,
            "caption_style": caption_style,
            "caption_position": caption_position,
            "audio_cleanup": bool(audio_cleanup),
        }), encoding="utf-8")
        os.replace(options_tmp, job_dir / "clipforge-options.json")
        _STORE.create(job_id, f"/download/{job_id}", _MAX_ACTIVE_JOBS)
    except Exception:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    try:
        _EXECUTOR.submit(_run_job, job_id, url, job_dir)
    except Exception as exc:
        _STORE.update(job_id, state="FAILED", failure_reason="Worker submission failed",
                      metrics={"progress": 100, "stage": "Worker unavailable"})
        raise RuntimeError("Studio worker is unavailable") from exc
    return job_id


def _set(job_id: str, **values: Any) -> None:
    _STORE.update(job_id, **values)


class _SupabaseRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_allowed_supabase_storage_url(newurl):
            raise PipelineError("Source URL redirected outside the allowed Supabase Storage endpoint")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, target: Path) -> None:
    if not is_allowed_supabase_storage_url(url):
        source_type = ensure_supported_ingestion_source(url)
        if source_type in {"youtube", "tiktok", "facebook", "instagram"}:
            download_social_video(url, target, _MAX_DOWNLOAD_BYTES)
        else:
            download_direct_mp4(url, target, _MAX_DOWNLOAD_BYTES)
        return
    request = urllib.request.Request(url, headers={"User-Agent": "ClipForge-Studio/0.2"})
    total = 0
    opener = urllib.request.build_opener(_SupabaseRedirectHandler())
    with opener.open(request, timeout=60) as response, target.open("wb") as output:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > _MAX_DOWNLOAD_BYTES:
            raise PipelineError("Source exceeds configured maximum source size")
        content_type = response.headers.get("Content-Type", "")
        if not (content_type.startswith("video/") or content_type.startswith("application/octet-stream")):
            raise PipelineError(f"Unsupported source content type: {content_type or 'unknown'}")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_DOWNLOAD_BYTES:
                raise PipelineError("Source exceeds configured maximum source size")
            output.write(chunk)
    if total == 0:
        raise PipelineError("Downloaded source is empty")


def _run_job(job_id: str, url: str | None, job_dir: Path, resume_staged: bool = False,
             clip_prompt: str | None = None, aspect_ratio: str | None = None,
             caption_style: str | None = None, caption_position: str | None = None,
             audio_cleanup: bool | None = None, transition: str | None = None,\n             transition_duration: float | None = None) -> None:
    try:
        source = job_dir / "source.media"
        if resume_staged:
            _set(job_id, state="RUNNING", metrics={"progress": 8, "stage": "Resuming staged source"})
            if not source.is_file() or source.stat().st_size == 0:
                raise PipelineError("Staged source is missing; resubmit this job")
        else:
            _set(job_id, state="RUNNING", metrics={"progress": 3, "stage": "Downloading source"})
            if not url:
                raise PipelineError("Source URL is unavailable; resubmit this job")
            partial = job_dir / "source.media.part"
            _download(url, partial)
            os.replace(partial, source)
        if clip_prompt is None or aspect_ratio is None or caption_style is None or caption_position is None or audio_cleanup is None or transition is None or transition_duration is None:
            options_path = job_dir / "clipforge-options.json"
            if options_path.is_file():
                try:
                    options = json.loads(options_path.read_text(encoding="utf-8"))
                    if clip_prompt is None:
                        clip_prompt = str(options.get("clip_prompt") or "") or None
                    if aspect_ratio is None:
                        aspect_ratio = str(options.get("aspect_ratio") or "9:16")
                    if caption_style is None:
                        caption_style = str(options.get("caption_style") or "karaoke")
                    if caption_position is None:
                        caption_position = str(options.get("caption_position") or "bottom")
                    if audio_cleanup is None:
                        audio_cleanup = bool(options.get("audio_cleanup", True))
                except (OSError, ValueError, TypeError):
                    clip_prompt = None
        aspect_ratio = aspect_ratio or "9:16"
        caption_style = caption_style or "karaoke"
        caption_position = caption_position or "bottom"
        audio_cleanup = True if audio_cleanup is None else bool(audio_cleanup)
        media = probe_media(source)
        _set(job_id, metrics={"progress": 12, "stage": "Validating media"})
        clips_dir = job_dir / "clips"
        import shutil
        shutil.rmtree(clips_dir, ignore_errors=True)
        clips_dir.mkdir(parents=True, exist_ok=True)
        results = analyze_and_extract(source, clips_dir, media, progress=lambda p, s: _set(
            job_id, metrics={"progress": p, "stage": s}
        ), clip_prompt=clip_prompt, aspect_ratio=aspect_ratio,
            caption_style=caption_style, caption_position=caption_position, audio_cleanup=audio_cleanup,\n            transition=transition, transition_duration=transition_duration)
        if not results:
            raise PipelineError("No suitable speech-led clips were found in this source")
        _set(job_id, metrics={"progress": 92, "stage": "Packaging clips"})
        archive = job_dir / "clips.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for item in results:
                bundle.write(item["path"], arcname=Path(item["path"]).name)
            bundle.writestr("clipforge_manifest.json", json.dumps({
                "job_id": job_id,
                "source_duration_seconds": media["format"]["duration"],
                "clips": [{k: v for k, v in item.items() if k != "path"} for item in results],
                "analysis": {"transcription": "WhisperX", "scene_detection": "PySceneDetect",
                             "auto_editor_silence_analysis": all(
                                 x["silence_analysis"] == "Auto-Editor" for x in results
                             ), "selection": "explainable 0-100 heuristic score: speech density, hook strength, context completeness, ending strength, scene alignment, audio activity, and optional prompt relevance",
                             "clip_prompt": clip_prompt or None, "aspect_ratio": aspect_ratio,
                             "caption_style": caption_style, "caption_position": caption_position,
                             "audio_cleanup": "FFmpeg high-pass/low-pass filtering, spectral denoise and loudness normalization" if audio_cleanup else "disabled",
                             "rendering": "Remotion captions and FFmpeg encoding"},
            }, indent=2))
        _STORE.update(job_id, artifact_path=archive, state="COMPLETED",
                      metrics={"progress": 100, "stage": "Clips ready"})
    except Exception as exc:
        _set(job_id, state="FAILED", failure_reason=str(exc)[:1000],
             metrics={"progress": 100, "stage": "Processing failed"})


def _resume_interrupted_jobs() -> None:
    for job_id in _STORE.recover_interrupted(_WORKDIR):
        job_dir = _WORKDIR / job_id
        try:
            _EXECUTOR.submit(_run_job, job_id, None, job_dir, True)
        except Exception as exc:
            _STORE.update(
                job_id,
                state="FAILED",
                failure_reason=("Automatic recovery could not start: " + str(exc))[:1000],
                metrics={"progress": 100, "stage": "Recovery worker unavailable"},
            )


def _cleanup_expired_jobs() -> None:
    while True:
        try:
            removed = _STORE.cleanup_expired(_WORKDIR, _RETENTION_SECONDS)
            if removed:
                logging.info("Expired %s ClipForge job(s) after retention window", removed)
        except Exception:
            logging.exception("ClipForge job retention cleanup failed")
        time.sleep(_CLEANUP_INTERVAL_SECONDS)


_resume_interrupted_jobs()
threading.Thread(target=_cleanup_expired_jobs, name="clipforge-retention", daemon=True).start()
