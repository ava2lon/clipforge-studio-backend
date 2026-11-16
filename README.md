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
- Remotion — renders each extracted clip to a 1080×1920 composition with WhisperX-derived captions.
- FFmpeg / FFprobe — media validation, accurate clip extraction and encoding.

## Implemented

- Authenticated FastAPI endpoints matching the current frontend contract: `POST /clips`, `GET /status/{job_id}`, and `GET /download/{job_id}`.
- HTTPS Supabase signed-URL ingestion plus direct public HTTPS MP4 ingestion. Direct downloads use DNS/IP validation, pinned TLS connections, redirect revalidation, content-type and MP4 signature checks, streaming byte limits and a 15-minute download deadline.
- Social URL classification for YouTube, TikTok, Facebook and Instagram. Actual media import is intentionally gated on approved provider APIs and account authorization; generic scraping/downloader extraction is not enabled.
- WhisperX transcript-driven candidate selection, PySceneDetect scene-boundary features, overlap suppression, FFmpeg extraction and ZIP manifest packaging.
- Configurable worker concurrency and queue admission limit.
- Health and capability endpoints.
- SQLite-backed job metadata and automatic restart recovery when a complete source file was staged before interruption.
- Automatic workspace and database cleanup for completed/failed jobs after the retention window (default seven days); cleanup runs hourly.

Candidate scores are deterministic heuristics based on speech density, a small set of hook-language patterns, nearby scene boundaries, and Auto-Editor active-audio overlap. They are not a trained virality predictor and must not be marketed as such.

## Important deployment limitations

- Job metadata and artifact references are stored in SQLite at `CLIPFORGE_DB_PATH` (default `/data/clipforge.sqlite3`). Mount persistent Hugging Face Space storage at `/data` to retain completed jobs and ZIP files across container rebuilds. On restart, jobs with a fully staged source file are automatically requeued from that local file; jobs interrupted during download are marked failed and must be resubmitted. Signed source URLs are intentionally not stored. This remains a single-container design; multiple replicas are not supported.
- Completed and failed job records, source files, clips and ZIPs are removed after the retention period. Set `CLIPFORGE_JOB_RETENTION_SECONDS` to change it (minimum one hour); set `CLIPFORGE_CLEANUP_INTERVAL_SECONDS` to change the cleanup polling interval (minimum five minutes).
- The job pipeline invokes WhisperX, PySceneDetect, Auto-Editor, Remotion and FFmpeg. Auto-Editor silence intervals and Remotion caption overlays are integrated, but require an end-to-end test against real source media and the target Space hardware.
- No diarization, face tracking, speaker-aware reframing, or configurable platform-specific layouts are implemented.
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
- `POST /clips` — JSON body accepts either `{"media":"https://<project>.supabase.co/storage/v1/object/sign/..."}` or `{"media":"https://cdn.example.com/video.mp4"}`; returns `job_id` and `source_type`. Direct URLs must serve a valid MP4. Social-platform links return HTTP 422 until an authorized provider connector is configured.
- `GET /status/{job_id}` — returns `PENDING`, `RUNNING`, `COMPLETED`, or `FAILED`.
- `GET /download/{job_id}` — ZIP artifact after completion.

All job endpoints require `X-API-Key`.
