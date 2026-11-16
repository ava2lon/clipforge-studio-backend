from __future__ import annotations

import hmac
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, HttpUrl

from renderer.capabilities import collect_capabilities
from renderer.jobs import create_job, get_job, job_artifact
from renderer.security import is_allowed_supabase_storage_url

APP_VERSION = "0.2.0"
_JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
WORKDIR = Path(os.getenv("CLIPFORGE_WORKDIR", "/tmp/clipforge")).resolve()
WORKDIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="ClipForge Studio Backend", version=APP_VERSION)


class ClipRequest(BaseModel):
    media: HttpUrl = Field(..., description="HTTPS Supabase Storage signed URL")


class ProbeRequest(BaseModel):
    media_path: str = Field(..., description="Path staged inside the server job workspace")


def _authorize(x_api_key: str | None) -> None:
    expected = os.getenv("AVA2LON_STUDIO_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Studio API key is not configured")
    if not x_api_key or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _validate_job_id(job_id: str) -> None:
    if not _JOB_ID_PATTERN.fullmatch(job_id):
        raise HTTPException(status_code=400, detail="Invalid job ID")


def _inside_workdir(candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(WORKDIR)
        return True
    except ValueError:
        return False


def _probe(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is not installed")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=45, check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr[-2000:] or "ffprobe could not read this media")
    import json
    return json.loads(result.stdout)


@app.get("/")
def root() -> dict[str, str]:
    return {"name": "ClipForge Studio Backend", "version": APP_VERSION, "status": "running"}


@app.get("/health")
@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok", "version": APP_VERSION}


@app.get("/capabilities")
def capabilities() -> dict[str, Any]:
    return collect_capabilities()


@app.post("/clips", status_code=202)
def submit_clip(request: ClipRequest, x_api_key: str | None = Header(default=None)) -> dict[str, str]:
    _authorize(x_api_key)
    url = str(request.media)
    if not is_allowed_supabase_storage_url(url):
        raise HTTPException(status_code=400, detail="media must be a valid HTTPS Supabase Storage signed URL on port 443")
    try:
        job_id = create_job(url, WORKDIR)
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"job_id": job_id, "status_url": f"/status/{job_id}", "download_url": f"/download/{job_id}"}


@app.get("/status/{job_id}")
def status(job_id: str, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(x_api_key)
    _validate_job_id(job_id)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/download/{job_id}")
def download(job_id: str, x_api_key: str | None = Header(default=None)):
    _authorize(x_api_key)
    _validate_job_id(job_id)
    artifact = job_artifact(job_id)
    if not artifact or not artifact.is_file():
        raise HTTPException(status_code=409, detail="Job artifact is not ready")
    from fastapi.responses import FileResponse
    return FileResponse(artifact, media_type="application/zip", filename=f"clipforge-{job_id}.zip")


@app.post("/media/probe")
def media_probe(request: ProbeRequest, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(x_api_key)
    path = Path(request.media_path)
    if not _inside_workdir(path) or not path.is_file():
        raise HTTPException(status_code=400, detail="media_path must reference an existing file inside CLIPFORGE_WORKDIR")
    try:
        return {"media": _probe(path)}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
