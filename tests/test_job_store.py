from pathlib import Path
from contextlib import closing

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


def test_restart_marks_inflight_jobs_failed_without_persisting_source_url(tmp_path: Path):
    database = tmp_path / "clipforge.sqlite3"
    store = JobStore(database)
    store.create("job-pending", "/download/job-pending", max_active=3)
    store.create("job-running", "/download/job-running", max_active=3)
    store.update("job-running", state="RUNNING")

    restarted = JobStore(database)
    assert restarted.fail_interrupted() == 2
    assert restarted.get("job-pending")["state"] == "FAILED"
    assert restarted.get("job-running")["state"] == "FAILED"
    with closing(restarted._connect()) as conn, conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "source_url" not in columns


def test_queue_capacity_is_atomic_and_enforced(tmp_path: Path):
    store = JobStore(tmp_path / "clipforge.sqlite3")
    store.create("job-1", "/download/job-1", max_active=1)
    with pytest.raises(RuntimeError, match="queue is full"):
        store.create("job-2", "/download/job-2", max_active=1)
    assert store.count_active() == 1
