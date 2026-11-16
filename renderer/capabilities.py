from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _version(binary: str) -> str | None:
    path = shutil.which(binary)
    if not path:
        return None
    try:
        result = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=5)
        return (result.stdout or result.stderr).splitlines()[0][:200]
    except Exception:
        return "installed; version unavailable"


def _storage_diagnostics(
    workdir: str | Path | None = None,
    database_path: str | Path | None = None,
) -> dict[str, bool]:
    """Report mount topology only; a mountpoint is not proof of durable storage."""
    root = Path(workdir or os.getenv("CLIPFORGE_WORKDIR", "/tmp/clipforge")).resolve()
    database = Path(
        database_path
        or os.getenv("CLIPFORGE_DB_PATH", str(root / "clipforge.sqlite3"))
    ).resolve()
    db_parent = database.parent
    try:
        same_filesystem = root.stat().st_dev == db_parent.stat().st_dev
    except OSError:
        same_filesystem = False
    return {
        "workdir_mountpoint_detected": root.is_mount(),
        "database_parent_mountpoint_detected": db_parent.is_mount(),
        "workdir_and_database_same_filesystem": same_filesystem,
    }


def collect_capabilities() -> dict[str, Any]:
    modules = {
        "pyscenedetect": "scenedetect",
        "whisperx": "whisperx",
    }
    return {
        "ffmpeg": {"available": shutil.which("ffmpeg") is not None, "version": _version("ffmpeg")},
        "ffprobe": {"available": shutil.which("ffprobe") is not None, "version": _version("ffprobe")},
        "auto_editor": {"available": shutil.which("auto-editor") is not None},
        "remotion": {"available": shutil.which("node") is not None and Path("remotion/package.json").exists()},
        "python_modules": {name: importlib.util.find_spec(module) is not None for name, module in modules.items()},
        "storage": _storage_diagnostics(),
    }
