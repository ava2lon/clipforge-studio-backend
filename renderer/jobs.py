"""In-process job runner for a single-container Space deployment.

The registry is intentionally isolated. Multi-replica and restart-safe operation requires
a durable queue plus shared artifact storage before production traffic is enabled.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.request
import uuid
from urllib.parse import urlsplit
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from renderer.pipeline import PipelineError, analyze_and_extract, probe_media

_MAX_WORKERS = max(1, int(os.getenv("CLIPFORGE_MAX_WORKERS", "1")))
_MAX_DOWNLOAD_BYTES = int(os.getenv("CLIPFORGE_MAX_SOURCE_BYTES", str(500 * 1024 * 1024)))
_EXECUTOR = ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="clipforge")
_LOCK = threading.RLock()
_JOBS: dict[str, dict[str, Any]] = {}
_ARTIFACTS: dict[str, Path] = {}


def _set(job_id: str, **values: Any) -> None:
    with _LOCK:
        _JOBS[job_id].update(values)


def get_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def job_artifact(job_id: str) -> Path | None:
    with _LOCK:
        return _ARTIFACTS.get(job_id)


def create_job(url: str, workdir: Path) -> str:
    with _LOCK:
        active = sum(1 for job in _JOBS.values() if job["state"] in {"PENDING", "RUNNING"})
        if active >= _MAX_WORKERS * 3:
            raise RuntimeError("Studio queue is full; retry later")
        job_id = uuid.uuid4().hex
        job_dir = workdir / job_id
        job_dir.mkdir(parents=True, exist_ok=False)
        _JOBS[job_id] = {
            "job_id": job_id, "state": "PENDING",
            "metrics": {"progress": 0, "stage": "Queued"},
            "failure_reason": None, "download_url": f"/download/{job_id}",
        }
    _EXECUTOR.submit(_run_job, job_id, url, job_dir)
    return job_id


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


def _run_job(job_id: str, url: str, job_dir: Path) -> None:
    try:
        _set(job_id, state="RUNNING", metrics={"progress": 3, "stage": "Downloading source"})
        source = job_dir / "source.media"
        _download(url, source)
        media = probe_media(source)
        _set(job_id, metrics={"progress": 12, "stage": "Validating media"})
        clips_dir = job_dir / "clips"
        clips_dir.mkdir()
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
                             "auto_editor_silence_analysis": all(x["silence_analysis"] == "Auto-Editor" for x in results),
                             "selection": "transcript density, hook terms, and overlap suppression"},
            }, indent=2))
        with _LOCK:
            _ARTIFACTS[job_id] = archive
            _JOBS[job_id].update(state="COMPLETED", metrics={"progress": 100, "stage": "Clips ready"})
    except Exception as exc:
        _set(job_id, state="FAILED", failure_reason=str(exc)[:1000],
             metrics={"progress": 100, "stage": "Processing failed"})
