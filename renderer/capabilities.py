from __future__ import annotations

import importlib.util
import shutil
import subprocess
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


def collect_capabilities() -> dict[str, Any]:
    modules = {
        "pyscenedetect": "scenedetect",
        "whisperx": "whisperx",
    }
    return {
        "ffmpeg": {"available": shutil.which("ffmpeg") is not None, "version": _version("ffmpeg")},
        "ffprobe": {"available": shutil.which("ffprobe") is not None, "version": _version("ffprobe")},
        "auto_editor": {"available": shutil.which("auto-editor") is not None},
        "remotion": {"available": shutil.which("node") is not None and __import__("pathlib").Path("remotion/package.json").exists()},
        "python_modules": {name: importlib.util.find_spec(module) is not None for name, module in modules.items()},
    }
