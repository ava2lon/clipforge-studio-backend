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
- HTTPS Supabase signed-URL ingestion with a source-size limit.
- WhisperX transcript-driven candidate selection, PySceneDetect scene-boundary features, overlap suppression, FFmpeg extraction and ZIP manifest packaging.
- Configurable worker concurrency and queue admission limit.
- Health and capability endpoints.

Candidate scores are deterministic heuristics based on speech density, a small set of hook-language patterns, nearby scene boundaries, and Auto-Editor active-audio overlap. They are not a trained virality predictor and must not be marketed as such.

## Important deployment limitations

- Job state and artifact paths are currently in memory and local disk. A container restart loses job tracking; multiple replicas are not supported. Use a durable queue and shared object storage before production scale.
- The job pipeline invokes WhisperX, PySceneDetect, Auto-Editor, Remotion and FFmpeg. Auto-Editor silence intervals and Remotion caption overlays are integrated, but require an end-to-end test against real source media and the target Space hardware.
- No diarization, face tracking, speaker-aware reframing, or configurable platform-specific layouts are implemented.
- Protect the service with a strong `AVA2LON_STUDIO_API_KEY`. Do not expose it publicly without authentication, rate limiting, durable job storage and operational monitoring.

## Configuration

- `AVA2LON_STUDIO_API_KEY` — required API key, sent by the frontend as `X-API-Key`.
- `CLIPFORGE_WORKDIR` — job workspace; defaults to `/tmp/clipforge`.
- `CLIPFORGE_MAX_WORKERS` — concurrent jobs; defaults to `1`.
- `CLIPFORGE_MAX_SOURCE_BYTES` — maximum downloaded source; defaults to 500 MiB.
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
- `POST /clips` — JSON body `{"media":"https://<project>.supabase.co/storage/v1/object/sign/..."}`; returns `job_id`.
- `GET /status/{job_id}` — returns `PENDING`, `RUNNING`, `COMPLETED`, or `FAILED`.
- `GET /download/{job_id}` — ZIP artifact after completion.

All job endpoints require `X-API-Key`.
