"""Persistent job runner for a single-container Space deployment."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from urllib.parse import urlsplit
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from renderer.job_store import JobStore
from renderer.pipeline import PipelineError, analyze_and_extract, probe_media

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


def create_job(url: str, workdir: Path) -> str:
    job_id = uuid.uuid4().hex
    job_dir = workdir / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    try:
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
        parsed = urlsplit(newurl)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".supabase.co"):
            raise PipelineError("Source URL redirected outside the allowed Supabase host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _download(url: str, target: Path) -> None:
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


def _run_job(job_id: str, url: str | None, job_dir: Path, resume_staged: bool = False) -> None:
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
        media = probe_media(source)
        _set(job_id, metrics={"progress": 12, "stage": "Validating media"})
        clips_dir = job_dir / "clips"
        import shutil
        shutil.rmtree(clips_dir, ignore_errors=True)
        clips_dir.mkdir(parents=True, exist_ok=True)
        results = analyze_and_extract(source, clips_dir, media, progress=lambda p, s: _set(
            job_id, metrics={"progress": p, "stage": s}
        ))
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
                             ), "selection": "transcript density, hook terms, audio activity, and overlap suppression",
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
