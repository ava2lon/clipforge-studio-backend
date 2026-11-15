from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from renderer.capabilities import collect_capabilities

APP_VERSION = "0.1.0"
WORKDIR = Path(os.getenv("CLIPFORGE_WORKDIR", "/tmp/clipforge")).resolve()
WORKDIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="ClipForge Studio Backend", version=APP_VERSION)
allowed_origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()]
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )


class ProbeRequest(BaseModel):
    media_path: str = Field(..., description="Path to a file staged inside the server job workspace")


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
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
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


@app.post("/media/probe")
def media_probe(request: ProbeRequest) -> dict[str, Any]:
    path = Path(request.media_path)
    if not _inside_workdir(path) or not path.is_file():
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="media_path must reference an existing file inside CLIPFORGE_WORKDIR")
    try:
        return {"media": _probe(path)}
    except (ValueError, RuntimeError) as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=str(exc)) from exc
