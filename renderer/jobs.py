"""Persistent job runner for a single-container Space deployment."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from renderer.job_store import JobStore
from renderer.pipeline import PipelineError, _clean_audio, _render_with_remotion, analyze_and_extract, probe_media
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
_EDIT_LOCK = threading.RLock()


def get_job(job_id: str) -> dict[str, Any] | None:
    return _STORE.get(job_id)


def job_artifact(job_id: str) -> Path | None:
    return _STORE.artifact(job_id)


def create_job(url: str, workdir: Path, clip_prompt: str | None = None, aspect_ratio: str = "9:16",
               caption_style: str = "karaoke", caption_position: str = "bottom", audio_cleanup: bool = True,
               transition: str = "none", transition_duration: float = 0.5,
               broll_assets: list[dict[str, Any]] | None = None) -> str:
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
            "transition": transition,
            "transition_duration": transition_duration,
            "broll_assets": [
                {"filename": f"asset-{index:02d}.mp4", "keywords": asset.get("keywords", [])}
                for index, asset in enumerate(broll_assets or [], 1)
            ],
        }), encoding="utf-8")
        os.replace(options_tmp, job_dir / "clipforge-options.json")
        _STORE.create(job_id, f"/download/{job_id}", _MAX_ACTIVE_JOBS)
    except Exception:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    try:
        _EXECUTOR.submit(_run_job, job_id, url, job_dir, broll_assets=broll_assets or [])
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


def _download(url: str, target: Path, max_bytes: int = _MAX_DOWNLOAD_BYTES) -> None:
    if not is_allowed_supabase_storage_url(url):
        source_type = ensure_supported_ingestion_source(url)
        if source_type in {"youtube", "tiktok", "facebook", "instagram"}:
            download_social_video(url, target, max_bytes)
        else:
            download_direct_mp4(url, target, max_bytes)
        return
    request = urllib.request.Request(url, headers={"User-Agent": "ClipForge-Studio/0.2"})
    total = 0
    opener = urllib.request.build_opener(_SupabaseRedirectHandler())
    with opener.open(request, timeout=60) as response, target.open("wb") as output:
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_bytes:
            raise PipelineError("Source exceeds configured maximum source size")
        content_type = response.headers.get("Content-Type", "")
        if not (content_type.startswith("video/") or content_type.startswith("application/octet-stream")):
            raise PipelineError(f"Unsupported source content type: {content_type or 'unknown'}")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise PipelineError("Source exceeds configured maximum source size")
            output.write(chunk)
    if total == 0:
        raise PipelineError("Downloaded source is empty")


def _run_job(job_id: str, url: str | None, job_dir: Path, resume_staged: bool = False,
             clip_prompt: str | None = None, aspect_ratio: str | None = None,
             caption_style: str | None = None, caption_position: str | None = None,
             audio_cleanup: bool | None = None, transition: str | None = None,
             transition_duration: float | None = None,
             broll_assets: list[dict[str, Any]] | None = None) -> None:
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
        if clip_prompt is None or aspect_ratio is None or caption_style is None or caption_position is None or audio_cleanup is None or transition is None or transition_duration is None or broll_assets is None:
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
                    if transition is None:
                        transition = str(options.get("transition") or "none")
                    if transition_duration is None:
                        transition_duration = float(options.get("transition_duration", 0.5))
                    if broll_assets is None:
                        broll_assets = options.get("broll_assets") or []
                except (OSError, ValueError, TypeError):
                    clip_prompt = None
        aspect_ratio = aspect_ratio or "9:16"
        caption_style = caption_style or "karaoke"
        caption_position = caption_position or "bottom"
        audio_cleanup = True if audio_cleanup is None else bool(audio_cleanup)
        transition = transition or "none"
        transition_duration = 0.5 if transition_duration is None else float(transition_duration)
        broll_assets = broll_assets or []
        staged_broll: list[dict[str, Any]] = []
        if broll_assets:
            broll_dir = job_dir / "broll"
            broll_dir.mkdir(parents=True, exist_ok=True)
            total_broll_bytes = 0
            for asset_index, asset in enumerate(broll_assets[:3], 1):
                target = broll_dir / f"asset-{asset_index:02d}.mp4"
                if not target.is_file() or target.stat().st_size == 0:
                    asset_url = asset.get("url")
                    if not asset_url:
                        raise PipelineError("A B-roll file was not fully staged before worker recovery; resubmit this job")
                    partial = target.with_suffix(".part")
                    _download(str(asset_url), partial, 50_000_000)
                    if partial.stat().st_size > 50_000_000:
                        partial.unlink(missing_ok=True)
                        raise PipelineError("Each B-roll asset must be 50MB or smaller")
                    os.replace(partial, target)
                total_broll_bytes += target.stat().st_size
                if total_broll_bytes > 100_000_000:
                    raise PipelineError("Combined B-roll assets must be 100MB or smaller")
                asset_media = probe_media(target)
                if not any(stream.get("codec_type") == "video" for stream in asset_media.get("streams", [])):
                    raise PipelineError("B-roll assets must contain a video stream")
                staged_broll.append({"path": str(target), "keywords": asset.get("keywords", []),
                                     "duration": float(asset_media.get("format", {}).get("duration", 0))})
        media = probe_media(source)
        _set(job_id, metrics={"progress": 12, "stage": "Validating media"})
        clips_dir = job_dir / "clips"
        import shutil
        shutil.rmtree(clips_dir, ignore_errors=True)
        clips_dir.mkdir(parents=True, exist_ok=True)
        results = analyze_and_extract(source, clips_dir, media, progress=lambda p, s: _set(
            job_id, metrics={"progress": p, "stage": s}
        ), clip_prompt=clip_prompt, aspect_ratio=aspect_ratio,
            caption_style=caption_style, caption_position=caption_position, audio_cleanup=audio_cleanup,
            transition=transition, transition_duration=transition_duration,
            broll_assets=staged_broll)
        if not results:
            raise PipelineError("No suitable speech-led clips were found in this source")
        _set(job_id, metrics={"progress": 92, "stage": "Packaging clips"})
        archive = job_dir / "clips.zip"
        manifest = {
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
        }
        manifest_json = json.dumps(manifest, indent=2)
        (job_dir / "clipforge_manifest.json").write_text(manifest_json, encoding="utf-8")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for item in results:
                bundle.write(item["path"], arcname=Path(item["path"]).name)
            bundle.writestr("clipforge_manifest.json", manifest_json)
        _STORE.update(job_id, artifact_path=archive, state="COMPLETED",
                      metrics={"progress": 100, "stage": "Clips ready"})
    except Exception as exc:
        _set(job_id, state="FAILED", failure_reason=str(exc)[:1000],
             metrics={"progress": 100, "stage": "Processing failed"})



def _edit_state_path(job_dir: Path, edit_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", edit_id):
        raise ValueError("Invalid edit ID")
    path = (job_dir / "edits" / edit_id / "edit.json").resolve()
    path.relative_to(job_dir.resolve())
    return path


def _write_edit_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".json.part")
    partial.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(partial, path)


def create_clip_edit(job_id: str, filename: str, workdir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or not re.fullmatch(r"clip-[0-9]{2}\.mp4", filename):
        raise ValueError("Invalid clip reference")
    job = get_job(job_id)
    if not job or job.get("state") != "COMPLETED":
        raise ValueError("A completed Studio job is required")
    job_dir = (workdir / job_id).resolve()
    job_dir.relative_to(workdir.resolve())
    manifest_path = job_dir / "clipforge_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("Clip manifest is unavailable")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    clip = next((item for item in manifest.get("clips", []) if item.get("name") == filename), None)
    if not clip:
        raise FileNotFoundError("Clip is not part of this job")
    source_relative = clip.get("editor_source")
    if not isinstance(source_relative, str) or not source_relative:
        raise ValueError("This clip does not have an editable source. Reprocess the original job.")
    source = (job_dir / "clips" / source_relative).resolve()
    source.relative_to(job_dir.resolve())
    if not source.is_file():
        raise FileNotFoundError("Editable source media has expired. Reprocess the original job.")
    duration = float(clip.get("duration", 0))
    trim_start = float(settings["trim_start"])
    trim_end = float(settings["trim_end"])
    if duration <= 0 or trim_start < 0 or trim_end <= trim_start or trim_end > duration:
        raise ValueError("Trim range must be inside the source clip and at least 0.25 seconds long")
    if trim_end - trim_start < 0.25:
        raise ValueError("Edited clips must be at least 0.25 seconds long")
    captions = settings.get("captions", [])
    if len(captions) > 300:
        raise ValueError("A maximum of 300 caption words is allowed")
    for caption in captions:
        start, end = float(caption["start"]), float(caption["end"])
        if not str(caption.get("text", "")).strip():
            raise ValueError("Caption text cannot be blank")
        if start < 0 or end <= start or end > duration:
            raise ValueError("Caption timestamps must fall within the original clip")
    settings["captions"] = sorted(captions, key=lambda item: (float(item["start"]), float(item["end"])))
    edits_root = job_dir / "edits"
    if edits_root.exists() and sum(1 for item in edits_root.iterdir() if item.is_dir()) >= 20:
        raise ValueError("This job has reached its 20 saved edit versions limit")
    edit_id = uuid.uuid4().hex
    state = {
        "edit_id": edit_id,
        "job_id": job_id,
        "clip_name": filename,
        "state": "SAVED",
        "failure_reason": None,
        "created_at": time.time(),
        "updated_at": time.time(),
        "settings": settings,
    }
    _write_edit_state(_edit_state_path(job_dir, edit_id), state)
    return state


def get_clip_edit(job_id: str, edit_id: str, workdir: Path) -> dict[str, Any] | None:
    job_dir = (workdir / job_id).resolve()
    try:
        job_dir.relative_to(workdir.resolve())
        path = _edit_state_path(job_dir, edit_id)
    except (ValueError, OSError):
        return None
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_clip_edits(job_id: str, workdir: Path) -> list[dict[str, Any]]:
    job_dir = (workdir / job_id).resolve()
    job_dir.relative_to(workdir.resolve())
    edits_root = job_dir / "edits"
    if not edits_root.is_dir():
        return []
    edits = []
    for path in edits_root.glob("*/edit.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            edits.append(state)
        except (OSError, ValueError):
            continue
    return sorted(edits, key=lambda item: float(item.get("created_at", 0)), reverse=True)


def render_clip_edit(job_id: str, edit_id: str, workdir: Path) -> dict[str, Any]:
    job_dir = (workdir / job_id).resolve()
    job_dir.relative_to(workdir.resolve())
    with _EDIT_LOCK:
        state = get_clip_edit(job_id, edit_id, workdir)
        if not state:
            raise FileNotFoundError("Saved edit not found")
        if state.get("state") in {"QUEUED", "RUNNING"}:
            return state
        active_edits = 0
        for saved_path in (job_dir / "edits").glob("*/edit.json"):
            try:
                saved_state = json.loads(saved_path.read_text(encoding="utf-8"))
                active_edits += saved_state.get("state") in {"QUEUED", "RUNNING"}
            except (OSError, ValueError):
                continue
        if active_edits >= 3:
            raise RuntimeError("This job already has three edits queued or rendering")
        state["state"] = "QUEUED"
        state["failure_reason"] = None
        state["updated_at"] = time.time()
        _write_edit_state(_edit_state_path(job_dir, edit_id), state)
        try:
            _EXECUTOR.submit(_run_clip_edit, job_id, edit_id, job_dir)
        except Exception as exc:
            state["state"] = "FAILED"
            state["failure_reason"] = "Edit render worker is unavailable"
            state["updated_at"] = time.time()
            _write_edit_state(_edit_state_path(job_dir, edit_id), state)
            raise RuntimeError("Edit render worker is unavailable") from exc
        return state


def _run_clip_edit(job_id: str, edit_id: str, job_dir: Path) -> None:
    state_path = _edit_state_path(job_dir, edit_id)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["state"] = "RUNNING"
        state["updated_at"] = time.time()
        _write_edit_state(state_path, state)
        manifest = json.loads((job_dir / "clipforge_manifest.json").read_text(encoding="utf-8"))
        clip = next(item for item in manifest.get("clips", []) if item.get("name") == state["clip_name"])
        settings = state["settings"]
        trim_start = float(settings["trim_start"])
        trim_end = float(settings["trim_end"])
        duration = trim_end - trim_start
        source = (job_dir / "clips" / clip["editor_source"]).resolve()
        source.relative_to(job_dir.resolve())
        if not source.is_file():
            raise PipelineError("Editable source media has expired; reprocess the original job")
        edit_dir = state_path.parent
        trimmed = edit_dir / "trimmed-source.mp4"
        output = edit_dir / "rendered.mp4"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise PipelineError("FFmpeg is not installed")
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{trim_start:.3f}",
             "-i", str(source), "-t", f"{duration:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(trimmed)],
            capture_output=True, text=True, timeout=900,
        )
        if proc.returncode or not trimmed.is_file() or trimmed.stat().st_size == 0:
            raise PipelineError("FFmpeg trim failed: " + (proc.stderr or proc.stdout)[-1200:])
        captions = []
        for item in settings.get("captions", []):
            start, end = float(item["start"]), float(item["end"])
            if end <= trim_start or start >= trim_end:
                continue
            captions.append({
                "start": round(max(0.0, start - trim_start), 3),
                "end": round(min(trim_end, end) - trim_start, 3),
                "text": str(item["text"]).strip(),
            })
        focus = []
        for item in clip.get("focus_keyframes", []):
            point_time = float(item.get("time", 0))
            if trim_start <= point_time <= trim_end:
                focus.append({**item, "time": round(point_time - trim_start, 3)})
        placements = []
        for item in clip.get("broll_placements", []):
            original_start = float(item.get("start", 0))
            original_end = original_start + float(item.get("duration", 0))
            overlap_start = max(trim_start, original_start)
            overlap_end = min(trim_end, original_end)
            if overlap_end - overlap_start < 0.25:
                continue
            asset_index = int(item.get("asset_index", -1))
            asset_path = (job_dir / "broll" / f"asset-{asset_index + 1:02d}.mp4").resolve()
            asset_path.relative_to(job_dir.resolve())
            if asset_path.is_file():
                placements.append({
                    "path": str(asset_path),
                    "start": overlap_start - trim_start,
                    "duration": overlap_end - overlap_start,
                })
        render_source = trimmed
        cleaned = edit_dir / "cleaned-source.mp4"
        try:
            if clip.get("audio_cleanup") == "applied":
                try:
                    render_source = _clean_audio(trimmed, cleaned)
                except PipelineError:
                    cleaned.unlink(missing_ok=True)
                    render_source = trimmed
            _render_with_remotion(
                render_source, output, duration, captions,
                settings["aspect_ratio"], focus,
                int(clip.get("source_width", 0)), int(clip.get("source_height", 0)),
                settings["caption_style"], settings["caption_position"],
                settings["transition"], float(settings["transition_duration"]),
                placements, settings["brand_template"],
            )
        finally:
            trimmed.unlink(missing_ok=True)
            cleaned.unlink(missing_ok=True)
        state["state"] = "COMPLETED"
        state["failure_reason"] = None
        state["output_filename"] = "rendered.mp4"
        state["updated_at"] = time.time()
        _write_edit_state(state_path, state)
    except Exception as exc:
        for temporary_name in ("trimmed-source.mp4", "cleaned-source.mp4"):
            (state_path.parent / temporary_name).unlink(missing_ok=True)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["state"] = "FAILED"
            state["failure_reason"] = str(exc)[:1000]
            state["updated_at"] = time.time()
            _write_edit_state(state_path, state)
        except Exception:
            logging.exception("Could not persist failed edit state for %s", edit_id)
        logging.exception("Clip edit render failed for job %s edit %s", job_id, edit_id)


def clip_edit_artifact(job_id: str, edit_id: str, workdir: Path) -> Path | None:
    state = get_clip_edit(job_id, edit_id, workdir)
    if not state or state.get("state") != "COMPLETED":
        return None
    path = (_edit_state_path((workdir / job_id).resolve(), edit_id).parent / "rendered.mp4").resolve()
    try:
        path.relative_to((workdir / job_id).resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


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
