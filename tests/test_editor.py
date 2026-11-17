from __future__ import annotations

import json

import pytest

from renderer import jobs


def _fixture_job(tmp_path, monkeypatch):
    job_id = "a" * 32
    job_dir = tmp_path / job_id
    source = job_dir / "clips" / "editor-source" / "clip-01.mp4"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"editable-source")
    manifest = {
        "job_id": job_id,
        "clips": [{
            "name": "clip-01.mp4",
            "duration": 12.0,
            "editor_source": "editor-source/clip-01.mp4",
            "captions": [
                {"start": 0.5, "end": 1.0, "text": "Original"},
                {"start": 1.0, "end": 1.5, "text": "caption"},
            ],
            "source_width": 1280,
            "source_height": 720,
            "focus_keyframes": [{"time": 1.0, "x": 0.5, "y": 0.4}],
            "broll_placements": [],
        }],
    }
    (job_dir / "clipforge_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(jobs, "get_job", lambda value: {"state": "COMPLETED"} if value == job_id else None)

    class FakeStore:
        def touch(self, value):
            assert value == job_id

        def count_active(self):
            return 0

    monkeypatch.setattr(jobs, "_STORE", FakeStore())
    return job_id, job_dir


def _settings():
    return {
        "trim_start": 1.0,
        "trim_end": 10.0,
        "captions": [
            {"start": 0.5, "end": 1.5, "text": "Edited"},
            {"start": 2.0, "end": 2.5, "text": "word"},
        ],
        "aspect_ratio": "9:16",
        "caption_style": "karaoke",
        "caption_position": "bottom",
        "transition": "fade",
        "transition_duration": 0.5,
        "brand_template": "creator",
    }


def test_create_clip_edit_persists_versioned_editor_settings(tmp_path, monkeypatch):
    job_id, job_dir = _fixture_job(tmp_path, monkeypatch)
    state = jobs.create_clip_edit(job_id, "clip-01.mp4", tmp_path, _settings())

    assert state["state"] == "SAVED"
    assert state["job_id"] == job_id
    saved = json.loads((job_dir / "edits" / state["edit_id"] / "edit.json").read_text())
    assert saved["settings"]["trim_start"] == 1.0
    assert saved["settings"]["captions"][0]["text"] == "Edited"
    assert jobs.get_clip_edit(job_id, state["edit_id"], tmp_path)["state"] == "SAVED"


def test_create_clip_edit_rejects_out_of_range_trim(tmp_path, monkeypatch):
    job_id, _ = _fixture_job(tmp_path, monkeypatch)
    settings = _settings()
    settings["trim_end"] = 13.0

    with pytest.raises(ValueError, match="Trim range"):
        jobs.create_clip_edit(job_id, "clip-01.mp4", tmp_path, settings)


def test_create_clip_edit_rejects_path_traversal_and_missing_source(tmp_path, monkeypatch):
    job_id, job_dir = _fixture_job(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="Invalid clip reference"):
        jobs.create_clip_edit(job_id, "../source.media", tmp_path, _settings())

    (job_dir / "clips" / "editor-source" / "clip-01.mp4").unlink()
    with pytest.raises(FileNotFoundError, match="Editable source"):
        jobs.create_clip_edit(job_id, "clip-01.mp4", tmp_path, _settings())


def test_edit_render_trims_shifts_captions_and_marks_complete(tmp_path, monkeypatch):
    job_id, job_dir = _fixture_job(tmp_path, monkeypatch)
    state = jobs.create_clip_edit(job_id, "clip-01.mp4", tmp_path, _settings())
    monkeypatch.setattr(jobs.shutil, "which", lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)

    def fake_ffmpeg(command, **kwargs):
        output = __import__("pathlib").Path(command[-1])
        output.write_bytes(b"trimmed-media")
        return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

    captured = {}

    def fake_render(source, output, duration, captions, *args):
        captured["duration"] = duration
        captured["captions"] = captions
        captured["focus"] = args[1]
        output.write_bytes(b"rendered-mp4")
        return output

    monkeypatch.setattr(jobs.subprocess, "run", fake_ffmpeg)
    monkeypatch.setattr(jobs, "_render_with_remotion", fake_render)
    jobs._run_clip_edit(job_id, state["edit_id"], job_dir)

    final_state = jobs.get_clip_edit(job_id, state["edit_id"], tmp_path)
    assert final_state["state"] == "COMPLETED"
    assert jobs.clip_edit_artifact(job_id, state["edit_id"], tmp_path).read_bytes() == b"rendered-mp4"
    assert captured["duration"] == 9.0
    assert captured["captions"] == [
        {"start": 0.0, "end": 0.5, "text": "Edited"},
        {"start": 1.0, "end": 1.5, "text": "word"},
    ]
    assert captured["focus"] == [{"time": 0.0, "x": 0.5, "y": 0.4}]


def test_interrupted_editor_render_is_requeued_when_source_survives(tmp_path, monkeypatch):
    job_id, job_dir = _fixture_job(tmp_path, monkeypatch)
    state = jobs.create_clip_edit(job_id, "clip-01.mp4", tmp_path, _settings())
    state["state"] = "RUNNING"
    jobs._write_edit_state(jobs._edit_state_path(job_dir, state["edit_id"]), state)
    submitted = []

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn.__name__, args))

    monkeypatch.setattr(jobs, "_WORKDIR", tmp_path)
    monkeypatch.setattr(jobs, "_EXECUTOR", FakeExecutor())
    jobs._resume_interrupted_edits()

    recovered = jobs.get_clip_edit(job_id, state["edit_id"], tmp_path)
    assert recovered["state"] == "QUEUED"
    assert submitted == [("_run_clip_edit", (job_id, state["edit_id"], job_dir))]


def test_interrupted_editor_render_respects_global_queue_capacity(tmp_path, monkeypatch):
    job_id, job_dir = _fixture_job(tmp_path, monkeypatch)
    state = jobs.create_clip_edit(job_id, "clip-01.mp4", tmp_path, _settings())
    state["state"] = "RUNNING"
    jobs._write_edit_state(jobs._edit_state_path(job_dir, state["edit_id"]), state)

    class FullQueueStore:
        def count_active(self):
            return 3

        def touch(self, value):
            assert value == job_id

    monkeypatch.setattr(jobs, "_STORE", FullQueueStore())
    monkeypatch.setattr(jobs, "_MAX_ACTIVE_JOBS", 3)
    monkeypatch.setattr(jobs, "_WORKDIR", tmp_path)
    jobs._resume_interrupted_edits()

    recovered = jobs.get_clip_edit(job_id, state["edit_id"], tmp_path)
    assert recovered["state"] == "FAILED"
    assert "queue is full" in recovered["failure_reason"]


def test_retry_job_reuses_staged_source_and_saved_options(tmp_path, monkeypatch):
    job_id, job_dir = _fixture_job(tmp_path, monkeypatch)
    (job_dir / "source.media").write_bytes(b"fully-staged-source")
    (job_dir / "clipforge-options.json").write_text(json.dumps({"broll_assets": []}), encoding="utf-8")
    submitted = []

    class RetryStore:
        def get(self, value):
            return {"state": "FAILED"} if value == job_id else None

        def retry_failed(self, value, max_active, max_attempts):
            assert value == job_id
            assert max_active > 0
            assert max_attempts == 3
            return 2

    class FakeExecutor:
        def submit(self, fn, *args):
            submitted.append((fn.__name__, args))

    monkeypatch.setattr(jobs, "_STORE", RetryStore())
    monkeypatch.setattr(jobs, "_WORKDIR", tmp_path)
    monkeypatch.setattr(jobs, "_EXECUTOR", FakeExecutor())
    result = jobs.retry_job(job_id)

    assert result == {"job_id": job_id, "state": "PENDING", "attempt_count": 2}
    assert submitted == [("_run_job", (job_id, None, job_dir, True))]


def test_retry_job_requires_fully_staged_source(tmp_path, monkeypatch):
    job_id, _ = _fixture_job(tmp_path, monkeypatch)

    class RetryStore:
        def get(self, value):
            return {"state": "FAILED"}

    monkeypatch.setattr(jobs, "_STORE", RetryStore())
    monkeypatch.setattr(jobs, "_WORKDIR", tmp_path)
    with pytest.raises(ValueError, match="complete staged source"):
        jobs.retry_job(job_id)


def test_new_job_admission_reserves_capacity_for_active_edit_renders(tmp_path, monkeypatch):
    captured = {}

    class AdmissionStore:
        def get(self, job_id):
            return None

        def create(self, job_id, download_url, max_active):
            captured["job_id"] = job_id
            captured["max_active"] = max_active

    class FakeExecutor:
        def submit(self, fn, *args, **kwargs):
            captured["worker"] = fn.__name__

    monkeypatch.setattr(jobs, "_STORE", AdmissionStore())
    monkeypatch.setattr(jobs, "_EXECUTOR", FakeExecutor())
    monkeypatch.setattr(jobs, "_MAX_ACTIVE_JOBS", 3)
    monkeypatch.setattr(jobs, "_count_active_edit_renders", lambda workdir: 2)

    job_id = jobs.create_job("https://example.com/video.mp4", tmp_path)

    assert captured == {"job_id": job_id, "max_active": 1, "worker": "_run_job"}
