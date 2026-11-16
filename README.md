---
title: ClipForge Studio Backend
emoji: 🎬
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# ClipForge Studio Backend

Docker-based video processing API for the ClipForge frontend. This repository is the source of truth; it is not mirrored from the previously empty Hugging Face Space.

## Processing stack

- WhisperX — speech transcription and optional forced alignment.
- PySceneDetect — content-based scene boundary detection.
- Auto-Editor — exports its stable v1 cut list using audio activity detection; retained ranges contribute to candidate ranking.
- Remotion — renders each extracted clip in 9:16, 1:1 or 16:9 with WhisperX-derived animated word captions, optional fades and muted B-roll overlays.
- FFmpeg / FFprobe — media validation, accurate clip extraction and encoding.

## Implemented

- Authenticated FastAPI endpoints matching the current frontend contract: `POST /clips`, `GET /status/{job_id}`, and `GET /download/{job_id}`.
- HTTPS Supabase signed-URL ingestion plus direct public HTTPS MP4 ingestion. Direct downloads use DNS/IP validation, pinned TLS connections, redirect revalidation, content-type and MP4 signature checks, streaming byte limits and a 15-minute download deadline.
- Social URL ingestion for YouTube, TikTok, Facebook and Instagram using yt-dlp without requiring users to connect an account, provide OAuth credentials or supply browser cookies. URL acceptance does not guarantee retrieval: platform-side sign-in, age, bot-verification, privacy, regional or rate-limit controls can block an otherwise public link. ClipForge reports those failures rather than bypassing access controls. Direct public HTTPS MP4 remains the most reliable URL-ingestion path.
- WhisperX transcript-driven candidate selection, explainable heuristic scoring, prompt keyword matching, PySceneDetect scene-boundary features, overlap suppression, FFmpeg extraction and ZIP manifest packaging.\n- Optional user-supplied B-roll: up to three MP4/MOV/MKV/WebM assets, 50 MB each and 100 MB combined. The frontend uploads assets to the authenticated user’s project storage; the backend accepts only signed Supabase Storage URLs, stages and probes each video, and inserts a muted overlay when a supplied keyword matches a transcript caption. Unmatched assets are not inserted. Placement is deterministic lexical matching, not semantic AI or an editable timeline.
- Configurable worker concurrency and queue admission limit.
- Editor source retention: each generated clip keeps its clean pre-caption source and face-focus metadata inside the parent job workspace; these files are excluded from the user ZIP.
- Versioned editor state and asynchronous Remotion exports: validated trim ranges, editable word-level captions, aspect ratio, caption style/position, fade settings and four accent-color brand templates (Classic, Creator, Clean, Neon).
- Per-clip edited MP4 rendering reuses the same Remotion composition and FFmpeg pipeline; the original generated clip remains unchanged.
- Health and capability endpoints.
- SQLite-backed job metadata and automatic restart recovery when a complete source file was staged before interruption.
- Automatic workspace and database cleanup for completed/failed jobs after the retention window (default seven days); cleanup runs hourly.

Candidate scores are deterministic heuristics based on speech density, a small set of hook-language patterns, nearby scene boundaries, and Auto-Editor active-audio overlap. They are not a trained virality predictor and must not be marketed as such.

## Important deployment limitations

- Job metadata and artifact references are stored in SQLite at `CLIPFORGE_DB_PATH` (default `/data/clipforge.sqlite3`). Mount persistent Hugging Face Space storage at `/data` to retain completed jobs and ZIP files across container rebuilds. On restart, jobs with a fully staged source file are automatically requeued from that local file; jobs interrupted during download are marked failed and must be resubmitted. Signed source URLs are intentionally not stored. This remains a single-container design; multiple replicas are not supported.
- Completed and failed job records, source files, generated clips, retained editor-source media, saved edit versions and edited MP4 exports are removed after the parent job retention period. Editor state is currently persisted in the same job workspace, not yet in a separate durable user-owned editor-project table. Set `CLIPFORGE_JOB_RETENTION_SECONDS` to change it (minimum one hour); set `CLIPFORGE_CLEANUP_INTERVAL_SECONDS` to change the cleanup polling interval (minimum five minutes).
- The job pipeline invokes WhisperX, PySceneDetect, Auto-Editor, Remotion and FFmpeg. Auto-Editor silence intervals and Remotion caption overlays are integrated, but require an end-to-end test against real source media and the target Space hardware.
- Face-aware reframing uses sampled Haar face detection and falls back to center crop when detection is unavailable; it is not active-speaker detection or diarization. B-roll currently has automatic keyword-based placement only; manual timeline editing, source-trim controls, transitions between separate clips and stock-footage search are not implemented. Audio cleanup and clip-boundary fade are optional render settings.
- Protect the service with a strong `AVA2LON_STUDIO_API_KEY`. Do not expose it publicly without authentication, rate limiting, durable job storage and operational monitoring.

## Configuration

- `AVA2LON_STUDIO_API_KEY` — required API key, sent by the frontend as `X-API-Key`.
- `CLIPFORGE_WORKDIR` — job workspace; defaults to `/data`.
- `CLIPFORGE_DB_PATH` — SQLite metadata database; defaults to `/data/clipforge.sqlite3`.
- `CLIPFORGE_MAX_WORKERS` — concurrent jobs; defaults to `1`.
- `CLIPFORGE_MAX_SOURCE_BYTES` — maximum downloaded source; defaults to 500 MiB.
- Direct MP4 ingestion has a fixed 3-redirect maximum, 12-second connection timeout, 30-second read timeout and 15-minute total download deadline.
- `CLIPFORGE_JOB_RETENTION_SECONDS` — terminal-job workspace/database retention; defaults to seven days, minimum one hour.
- `CLIPFORGE_CLEANUP_INTERVAL_SECONDS` — cleanup polling interval; defaults to one hour, minimum five minutes.
- `WHISPER_MODEL` — WhisperX model; defaults to `small`.
- `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`, `WHISPER_BATCH_SIZE` — inference tuning.
- `CLIP_MIN_SECONDS`, `CLIP_MAX_SECONDS`, `CLIP_MAX_COUNT` — clip selection limits.

## Run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export AVA2LON_STUDIO_API_KEY='replace-with-a-long-random-secret'
uvicorn api:app --host 0.0.0.0 --port 7860
```

## API contract

- `GET /health`, `GET /healthz` — liveness.
- `GET /capabilities` — installed runtime tools.
- `GET /metrics` — API-key-protected aggregate job states, active job/edit render counts, worker/queue limits, workspace disk capacity and retention configuration. It does not expose source URLs, job IDs, user data or filesystem paths.
- `POST /clips` — JSON body accepts a Supabase signed URL, direct public HTTPS MP4 URL, or recognized YouTube, TikTok, Facebook and Instagram URL. Optional `broll_assets` is an array of up to three objects: `{"url":"<signed Supabase Storage URL>","keywords":["office","team"]}`. B-roll URLs must use the signed-object endpoint on a Supabase project host; each asset is limited to 50 MB, combined assets to 100 MB, and each item accepts 1–8 keywords (40 characters maximum). Returns `job_id` and `source_type`. Social extraction is attempted without user OAuth or cookies, but platform restrictions can prevent retrieval; no login, CAPTCHA, anti-bot or DRM bypass is performed.
- `GET /status/{job_id}` — returns `PENDING`, `RUNNING`, `COMPLETED`, or `FAILED`.
- `GET /download/{job_id}` — ZIP artifact after completion.
- `GET /manifest/{job_id}` — completed-job manifest including editor-source references and per-clip rendering metadata.
- `GET /clip/{job_id}/{filename}` — individual generated MP4.
- `POST /edit/{job_id}/{filename}` — save a versioned edit. Body includes `trim_start`, `trim_end`, word-level `captions`, `aspect_ratio`, `caption_style`, `caption_position`, `transition`, `transition_duration` and `brand_template`.
- `POST /edit/{job_id}/{edit_id}/render` — queue the edited MP4 render.
- `GET /edit/{job_id}/{edit_id}` — saved edit/render status.
- `GET /edit/{job_id}/{edit_id}/download` — edited MP4 after completion.

All job and edit endpoints require `X-API-Key`.
