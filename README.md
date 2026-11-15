# ClipForge Studio Backend

A Docker-based video processing API for ClipForge. This repository is the new source of truth; it is not copied from the empty Hugging Face Space.

## Required processing stack

- WhisperX (`m-bain/whisperX`) — transcription and word alignment.
- PySceneDetect (`Breakthrough/PySceneDetect`) — scene boundary detection.
- Auto-Editor (`WyattBlue/auto-editor`) — silence-aware edit analysis.
- Remotion (`remotion-dev/remotion`) — programmatic caption/layout rendering.
- FFmpeg (`FFmpeg/FFmpeg`) — media probing, extraction, encoding and muxing.

## Current status

This is the clean backend foundation: FastAPI service, health/capability reporting, secure local job workspace, FFmpeg validation, and an explicit processing adapter boundary. The heavy AI/rendering stages are wired as separately testable adapters and must be enabled only after their runtime/model requirements are satisfied. Do not report intelligent highlight ranking or Remotion rendering as complete until those stages are implemented and tested.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 7860
```

Set `CLIPFORGE_WORKDIR` to a persistent writable directory in production. Configure model/cache storage and GPU access according to the target Hugging Face Space hardware.

## API

- `GET /health` and `GET /healthz` — liveness.
- `GET /capabilities` — installed tool availability.
- `GET /` — service metadata.

The initial API deliberately does not expose unauthenticated arbitrary URL fetching or arbitrary filesystem paths. Add authenticated job submission and storage integration against the existing frontend contract before enabling public production traffic.

## Container

The Docker image installs FFmpeg and Node.js, then installs the Python API dependencies. Remotion's project/runtime is kept in `remotion/` and is built separately from the Python service.

## Development

```bash
pytest -q
```
