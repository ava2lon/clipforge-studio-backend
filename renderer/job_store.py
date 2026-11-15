"""SQLite-backed job metadata store for single-container deployments."""
from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class JobStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _initialize(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    failure_reason TEXT,
                    download_url TEXT NOT NULL,
                    artifact_path TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at)")

    def create(self, job_id: str, download_url: str, max_active: int) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE state IN ('PENDING','RUNNING')"
            ).fetchone()["n"]
            if int(active) >= max_active:
                raise RuntimeError("Studio queue is full; retry later")
            conn.execute(
                "INSERT INTO jobs(job_id,state,metrics_json,download_url) VALUES(?,?,?,?)",
                (job_id, "PENDING", json.dumps({"progress": 0, "stage": "Queued"}), download_url),
            )

    def update(self, job_id: str, **values: Any) -> None:
        allowed = {"state", "metrics", "failure_reason", "artifact_path"}
        if set(values) - allowed:
            raise ValueError("Unsupported job fields")
        fields = []
        params: list[Any] = []
        for key, value in values.items():
            if key == "metrics":
                fields.append("metrics_json=?")
                params.append(json.dumps(value))
            else:
                fields.append(f"{key}=?")
                params.append(str(value) if key == "artifact_path" and value is not None else value)
        if not fields:
            return
        fields.append("updated_at=CURRENT_TIMESTAMP")
        params.append(job_id)
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(f"UPDATE jobs SET {','.join(fields)} WHERE job_id=?", params)
            if cursor.rowcount != 1:
                raise KeyError(f"Unknown job: {job_id}")

    def get(self, job_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        return {
            "job_id": row["job_id"],
            "state": row["state"],
            "metrics": json.loads(row["metrics_json"]),
            "failure_reason": row["failure_reason"],
            "download_url": row["download_url"],
        }

    def artifact(self, job_id: str) -> Path | None:
        with closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT artifact_path FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None or not row["artifact_path"]:
            return None
        return Path(row["artifact_path"])

    def count_active(self) -> int:
        with closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE state IN ('PENDING','RUNNING')"
            ).fetchone()
        return int(row["n"])

    def recover_interrupted(self, workdir: str | Path) -> list[str]:
        """Requeue interrupted jobs only when their fully staged source still exists."""
        root = Path(workdir).resolve()
        resumed: list[str] = []
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT job_id FROM jobs WHERE state IN ('PENDING','RUNNING')"
            ).fetchall()
            for row in rows:
                job_id = row["job_id"]
                source = root / job_id / "source.media"
                if source.is_file() and source.stat().st_size > 0:
                    conn.execute(
                        """UPDATE jobs SET state='PENDING', failure_reason=NULL,
                           metrics_json=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=?""",
                        (json.dumps({"progress": 5, "stage": "Recovering staged source"}), job_id),
                    )
                    resumed.append(job_id)
                else:
                    conn.execute(
                        """UPDATE jobs SET state='FAILED',
                           failure_reason='Processing was interrupted before the source was fully staged; resubmit the source.',
                           metrics_json=?, updated_at=CURRENT_TIMESTAMP WHERE job_id=?""",
                        (json.dumps({"progress": 100, "stage": "Interrupted during source staging"}), job_id),
                    )
        return resumed

    def cleanup_expired(self, workdir: str | Path, retention_seconds: int) -> int:
        """Delete terminal jobs and their workspaces after the configured retention period."""
        if retention_seconds < 3600:
            raise ValueError("retention_seconds must be at least one hour")
        root = Path(workdir).resolve()
        modifier = f"-{int(retention_seconds)} seconds"
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """SELECT job_id FROM jobs
                   WHERE state IN ('COMPLETED','FAILED')
                     AND updated_at < datetime('now', ?)""",
                (modifier,),
            ).fetchall()

        removed = 0
        for row in rows:
            job_id = row["job_id"]
            job_dir = (root / job_id).resolve()
            try:
                job_dir.relative_to(root)
            except ValueError:
                continue
            try:
                shutil.rmtree(job_dir, ignore_errors=False)
            except FileNotFoundError:
                pass
            except OSError:
                continue

            with closing(self._connect()) as conn, conn:
                cursor = conn.execute(
                    """DELETE FROM jobs
                       WHERE job_id=? AND state IN ('COMPLETED','FAILED')
                         AND updated_at < datetime('now', ?)""",
                    (job_id, modifier),
                )
                removed += cursor.rowcount
        return removed

    def fail_interrupted(self) -> int:
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute(
                """UPDATE jobs SET state='FAILED',
                   failure_reason='Processing was interrupted by a backend restart; resubmit the source.',
                   metrics_json=?, updated_at=CURRENT_TIMESTAMP
                   WHERE state IN ('PENDING','RUNNING')""",
                (json.dumps({"progress": 100, "stage": "Interrupted by restart"}),),
            )
            return cursor.rowcount
