from contextlib import closing
from pathlib import Path

import pytest

from renderer.job_store import JobStore


def test_job_state_and_artifact_survive_store_reinitialization(tmp_path: Path):
    database = tmp_path / "persistent" / "clipforge.sqlite3"
    artifact = tmp_path / "job-1" / "clips.zip"
    artifact.parent.mkdir()
    artifact.write_bytes(b"zip-placeholder")

    first = JobStore(database)
    first.create("job-1", "/download/job-1", max_active=3)
    first.update("job-1", state="COMPLETED",
                 metrics={"progress": 100, "stage": "Clips ready"},
                 artifact_path=artifact)

    restarted = JobStore(database)
    assert restarted.get("job-1") == {
        "job_id": "job-1",
        "state": "COMPLETED",
        "metrics": {"progress": 100, "stage": "Clips ready"},
        "failure_reason": None,
        "download_url": "/download/job-1",
    }
    assert restarted.artifact("job-1") == artifact


def test_restart_resumes_only_fully_staged_source(tmp_path: Path):
    database = tmp_path / "clipforge.sqlite3"
    workdir = tmp_path / "jobs"
    staged = workdir / "job-staged" / "source.media"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"complete-media")
    partial = workdir / "job-partial" / "source.media.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"incomplete-media")

    store = JobStore(database)
    store.create("job-staged", "/download/job-staged", max_active=3)
    store.create("job-partial", "/download/job-partial", max_active=3)
    store.update("job-staged", state="RUNNING")
    store.update("job-partial", state="RUNNING")

    restarted = JobStore(database)
    assert restarted.recover_interrupted(workdir) == ["job-staged"]
    assert restarted.get("job-staged")["state"] == "PENDING"
    assert restarted.get("job-partial")["state"] == "FAILED"
    with closing(restarted._connect()) as conn, conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "source_url" not in columns


def test_queue_capacity_is_atomic_and_enforced(tmp_path: Path):
    store = JobStore(tmp_path / "clipforge.sqlite3")
    store.create("job-1", "/download/job-1", max_active=1)
    with pytest.raises(RuntimeError, match="queue is full"):
        store.create("job-2", "/download/job-2", max_active=1)
    assert store.count_active() == 1


def test_cleanup_removes_expired_terminal_jobs_but_keeps_active_and_recent(tmp_path: Path):
    database = tmp_path / "clipforge.sqlite3"
    workdir = tmp_path / "jobs"
    store = JobStore(database)

    for job_id in ("expired-complete", "expired-failed", "active", "recent"):
        job_dir = workdir / job_id
        job_dir.mkdir(parents=True)
        (job_dir / "payload.bin").write_bytes(b"data")
        store.create(job_id, f"/download/{job_id}", max_active=10)

    store.update("expired-complete", state="COMPLETED")
    store.update("expired-failed", state="FAILED")
    store.update("recent", state="COMPLETED")
    store.update("active", state="RUNNING")

    with closing(store._connect()) as conn, conn:
        conn.execute(
            """UPDATE jobs SET updated_at=datetime('now', '-8 days')
               WHERE job_id IN ('expired-complete','expired-failed')"""
        )

    assert store.cleanup_expired(workdir, 7 * 24 * 60 * 60) == 2
    assert store.get("expired-complete") is None
    assert store.get("expired-failed") is None
    assert store.get("active")["state"] == "RUNNING"
    assert store.get("recent")["state"] == "COMPLETED"
    assert not (workdir / "expired-complete").exists()
    assert not (workdir / "expired-failed").exists()
    assert (workdir / "active").exists()
    assert (workdir / "recent").exists()


def test_cleanup_rejects_retention_shorter_than_one_hour(tmp_path: Path):
    store = JobStore(tmp_path / "clipforge.sqlite3")
    with pytest.raises(ValueError, match="at least one hour"):
        store.cleanup_expired(tmp_path / "jobs", 30)
