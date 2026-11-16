from __future__ import annotations

import hmac
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, HttpUrl

from renderer.capabilities import collect_capabilities
from renderer.jobs import create_job, get_job, job_artifact
from renderer.ingestion import SourceAccessRequired, ensure_supported_ingestion_source
from renderer.security import is_allowed_supabase_storage_url

APP_VERSION = "0.2.0"
_JOB_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
WORKDIR = Path(os.getenv("CLIPFORGE_WORKDIR", "/tmp/clipforge")).resolve()
WORKDIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="ClipForge Studio Backend", version=APP_VERSION)


class BrollAssetRequest(BaseModel):
    url: HttpUrl
    keywords: list[str] = Field(default_factory=list, min_length=1, max_length=8)


class ClipRequest(BaseModel):
    media: HttpUrl = Field(..., description="HTTPS Supabase Storage signed URL or public direct MP4 URL")
    clip_prompt: str | None = Field(default=None, max_length=500, description="Optional natural-language topic or moment to find in the source video")
    aspect_ratio: Literal["9:16", "1:1", "16:9"] = Field(default="9:16", description="Rendered output aspect ratio")
    caption_style: Literal["karaoke", "bold", "minimal"] = Field(default="karaoke", description="Caption visual style")
    caption_position: Literal["top", "center", "bottom"] = Field(default="bottom", description="Caption vertical position")
    audio_cleanup: bool = Field(default=True, description="Apply FFmpeg voice cleanup and loudness normalization")
    transition: Literal["none", "fade"] = Field(default="none", description="Fade video and audio at clip boundaries")
    transition_duration: float = Field(default=0.5, ge=0.0, le=1.5, description="Fade duration in seconds; maximum 1.5")
    broll_assets: list[BrollAssetRequest] = Field(default_factory=list, max_length=3)


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
    for asset in request.broll_assets:
        asset_url = str(asset.url)
        if not is_allowed_supabase_storage_url(asset_url):
            raise HTTPException(status_code=400, detail="B-roll assets must use signed Supabase Storage URLs.")
        if any(not keyword.strip() or len(keyword.strip()) > 40 for keyword in asset.keywords):
            raise HTTPException(status_code=422, detail="Each B-roll keyword must contain 1 to 40 characters.")
    try:
        source_type = ensure_supported_ingestion_source(url)
    except SourceAccessRequired as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        job_id = create_job(url, WORKDIR, request.clip_prompt, request.aspect_ratio, request.caption_style, request.caption_position, request.audio_cleanup, request.transition, request.transition_duration,
                              [{"url": str(asset.url), "keywords": [keyword.strip() for keyword in asset.keywords]} for asset in request.broll_assets])
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    return {"job_id": job_id, "source_type": source_type, "status_url": f"/status/{job_id}", "download_url": f"/download/{job_id}"}


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


@app.get("/manifest/{job_id}")
def manifest(job_id: str, x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    _authorize(x_api_key)
    _validate_job_id(job_id)
    job = get_job(job_id)
    if not job or job.get("state") != "COMPLETED":
        raise HTTPException(status_code=409, detail="Completed job manifest is not available")
    manifest_path = (WORKDIR / job_id / "clipforge_manifest.json").resolve()
    if not _inside_workdir(manifest_path) or not manifest_path.is_file():
        raise HTTPException(status_code=404, detail="Job manifest is unavailable")
    import json
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="Job manifest could not be read") from exc
    return payload


@app.get("/clip/{job_id}/{filename}")
def clip_file(job_id: str, filename: str, x_api_key: str | None = Header(default=None)):
    _authorize(x_api_key)
    _validate_job_id(job_id)
    if not re.fullmatch(r"clip-[0-9]{2}\.mp4", filename):
        raise HTTPException(status_code=400, detail="Invalid clip filename")
    job = get_job(job_id)
    if not job or job.get("state") != "COMPLETED":
        raise HTTPException(status_code=409, detail="Completed clip is not available")
    clip_path = (WORKDIR / job_id / "clips" / filename).resolve()
    if not _inside_workdir(clip_path) or not clip_path.is_file():
        raise HTTPException(status_code=404, detail="Clip asset not found")
    return FileResponse(clip_path, media_type="video/mp4", filename=filename,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


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
