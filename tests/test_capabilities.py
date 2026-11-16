from pathlib import Path

from renderer.capabilities import _storage_diagnostics


def test_storage_diagnostics_report_same_filesystem_without_claiming_durability(tmp_path: Path):
    workdir = tmp_path / "data"
    workdir.mkdir()
    database = workdir / "clipforge.sqlite3"

    result = _storage_diagnostics(workdir, database)

    assert result["workdir_and_database_same_filesystem"] is True
    assert "persistent" not in result
    assert isinstance(result["workdir_mountpoint_detected"], bool)
    assert isinstance(result["database_parent_mountpoint_detected"], bool)


def test_storage_diagnostics_handles_missing_paths(tmp_path: Path):
    result = _storage_diagnostics(tmp_path / "missing-workdir", tmp_path / "missing-db" / "db.sqlite3")

    assert result["workdir_and_database_same_filesystem"] is False
