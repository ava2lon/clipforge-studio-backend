import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

import api
import renderer.jobs as jobs
from renderer.job_store import JobStore


class ImmediateExecutor:
    """Run the worker inline so this integration test has no timing races."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def test_submit_process_status_and_download_complete_lifecycle(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.setattr(api, "WORKDIR", workdir)
    monkeypatch.setattr(jobs, "_STORE", JobStore(tmp_path / "jobs.sqlite3"))
    monkeypatch.setattr(jobs, "_EXECUTOR", ImmediateExecutor())
    monkeypatch.setenv("AVA2LON_STUDIO_API_KEY", "integration-secret")

    def fake_download(_url, target):
        target.write_bytes(b"synthetic-source-media")

    def fake_probe(_source):
        return {
            "format": {"duration": "42.0"},
            "streams": [
                {"codec_type": "video", "width": 1280, "height": 720},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        }

    def fake_analyze(_source, output_dir, _media, progress=lambda *_: None, **_options):
        clip = output_dir / "clip-01.mp4"
        clip.write_bytes(b"synthetic-rendered-clip")
        progress(90, "Rendered test clip")
        return [{
            "start": 1.0,
            "end": 21.0,
            "duration": 20.0,
            "score": 0.8,
            "transcript": "This is a deterministic integration-test clip.",
            "name": clip.name,
            "path": str(clip),
            "scene_detection": "PySceneDetect",
            "silence_analysis": "Auto-Editor",
        }]

    monkeypatch.setattr(jobs, "_download", fake_download)
    monkeypatch.setattr(jobs, "probe_media", fake_probe)
    monkeypatch.setattr(jobs, "analyze_and_extract", fake_analyze)

    client = TestClient(api.app)
    response = client.post(
        "/clips",
        headers={"X-API-Key": "integration-secret"},
        json={
            "media": (
                "https://project.supabase.co/storage/v1/object/sign/"
                "videos/source.mp4?token=test-token"
            )
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    status = client.get(
        f"/status/{job_id}", headers={"X-API-Key": "integration-secret"}
    )
    assert status.status_code == 200
    assert status.json()["state"] == "COMPLETED"
    assert status.json()["metrics"]["progress"] == 100
    assert status.json()["metrics"]["source_duration_seconds"] == 42.0

    download = client.get(
        f"/download/{job_id}", headers={"X-API-Key": "integration-secret"}
    )
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/zip")

    archive_path = workdir / job_id / "clips.zip"
    assert archive_path.is_file()
    with zipfile.ZipFile(archive_path) as archive:
        assert "clip-01.mp4" in archive.namelist()
        assert "clipforge_manifest.json" in archive.namelist()
        manifest = json.loads(archive.read("clipforge_manifest.json"))
        assert manifest["job_id"] == job_id
        assert manifest["source_duration_seconds"] == 42.0
        assert isinstance(manifest["source_duration_seconds"], float)
        assert manifest["clips"][0]["name"] == "clip-01.mp4"
        assert archive.read("clip-01.mp4") == b"synthetic-rendered-clip"

    # Simulate a fresh application process by reopening the same SQLite database.
    # The persisted artifact path must still resolve and remain downloadable.
    monkeypatch.setattr(jobs, "_STORE", JobStore(tmp_path / "jobs.sqlite3"))
    restarted_status = client.get(
        f"/status/{job_id}", headers={"X-API-Key": "integration-secret"}
    )
    assert restarted_status.status_code == 200
    assert restarted_status.json()["state"] == "COMPLETED"

    restarted_download = client.get(
        f"/download/{job_id}", headers={"X-API-Key": "integration-secret"}
    )
    assert restarted_download.status_code == 200
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        assert archive.read("clip-01.mp4") == b"synthetic-rendered-clip"



def test_source_duration_ceiling_rejects_over_quota_media_before_analysis(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    store = JobStore(tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(jobs, "_STORE", store)
    monkeypatch.setattr(jobs, "_EXECUTOR", ImmediateExecutor())

    def fake_download(_url, target):
        target.write_bytes(b"synthetic-source-media")

    def fake_probe(_source):
        return {
            "format": {"duration": "42.0"},
            "streams": [{"codec_type": "video", "width": 1280, "height": 720}],
        }

    def analysis_must_not_run(*_args, **_kwargs):
        raise AssertionError("analysis must not run after the reserved duration ceiling is exceeded")

    monkeypatch.setattr(jobs, "_download", fake_download)
    monkeypatch.setattr(jobs, "probe_media", fake_probe)
    monkeypatch.setattr(jobs, "analyze_and_extract", analysis_must_not_run)

    job_id = jobs.create_job(
        "https://cdn.example.net/source.mp4",
        workdir,
        max_source_duration_seconds=30,
    )
    state = jobs.get_job(job_id)
    assert state["state"] == "FAILED"
    assert "exceeds the remaining plan allocation" in state["failure_reason"]
    assert state["metrics"]["source_duration_seconds"] == 42.0



def test_source_duration_guard_rejects_non_finite_probe_values(tmp_path, monkeypatch):
    import math

    workdir = tmp_path / "work"
    workdir.mkdir()
    store = JobStore(tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(jobs, "_STORE", store)
    monkeypatch.setattr(jobs, "_EXECUTOR", ImmediateExecutor())

    def fake_download(_url, target):
        target.write_bytes(b"synthetic-source-media")

    def fake_probe(_source):
        return {
            "format": {"duration": "NaN"},
            "streams": [{"codec_type": "video", "width": 1280, "height": 720}],
        }

    def analysis_must_not_run(*_args, **_kwargs):
        raise AssertionError("analysis must not run for a non-finite source duration")

    monkeypatch.setattr(jobs, "_download", fake_download)
    monkeypatch.setattr(jobs, "probe_media", fake_probe)
    monkeypatch.setattr(jobs, "analyze_and_extract", analysis_must_not_run)

    job_id = jobs.create_job("https://cdn.example.net/source.mp4", workdir)
    state = jobs.get_job(job_id)
    assert state["state"] == "FAILED"
    assert "finite positive duration" in state["failure_reason"]
    assert "source_duration_seconds" not in state["metrics"]


def test_source_duration_guard_rejects_non_finite_reserved_ceiling(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    store = JobStore(tmp_path / "jobs.sqlite3")
    monkeypatch.setattr(jobs, "_STORE", store)
    monkeypatch.setattr(jobs, "_EXECUTOR", ImmediateExecutor())

    monkeypatch.setattr(jobs, "_download", lambda _url, target: target.write_bytes(b"source"))
    monkeypatch.setattr(jobs, "probe_media", lambda _source: {
        "format": {"duration": "42.0"},
        "streams": [{"codec_type": "video", "width": 1280, "height": 720}],
    })
    monkeypatch.setattr(
        jobs, "analyze_and_extract",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("analysis must not run")),
    )

    job_id = jobs.create_job(
        "https://cdn.example.net/source.mp4",
        workdir,
        max_source_duration_seconds=float("inf"),
    )
    state = jobs.get_job(job_id)
    assert state["state"] == "FAILED"
    assert "Reserved source-duration ceiling is invalid" in state["failure_reason"]


def test_retry_rejects_quota_managed_job_without_new_reservation(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    job_id = "a" * 32
    job_dir = workdir / job_id
    job_dir.mkdir(parents=True)
    (job_dir / "source.media").write_bytes(b"staged-source")
    (job_dir / "clipforge-options.json").write_text(
        json.dumps({"quota_managed": True, "broll_assets": []}),
        encoding="utf-8",
    )
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create(job_id, f"/download/{job_id}", max_active=3)
    store.update(job_id, state="FAILED", failure_reason="temporary failure")

    monkeypatch.setattr(jobs, "_WORKDIR", workdir)
    monkeypatch.setattr(jobs, "_STORE", store)

    import pytest
    with pytest.raises(ValueError, match="resubmitted through ClipForge"):
        jobs.retry_job(job_id)
