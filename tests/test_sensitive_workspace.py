import os
from pathlib import Path
import stat
from tempfile import mkdtemp

import pytest

from network_fw2bids.errors import ConversionError
from network_fw2bids.sensitive_workspace import sensitive_workspace


def test_sensitive_workspace_requires_slurm_tmpdir(monkeypatch):
    monkeypatch.delenv("SLURM_TMPDIR", raising=False)

    with pytest.raises(ConversionError, match="SLURM_TMPDIR"):
        with sensitive_workspace():
            pass


def test_sensitive_workspace_is_private_and_removed(tmp_path, monkeypatch):
    monkeypatch.setenv("SLURM_TMPDIR", str(tmp_path))

    with sensitive_workspace() as workspace:
        assert workspace.parent == tmp_path.resolve()
        assert stat.S_IMODE(workspace.stat().st_mode) == 0o700
        marker = workspace / "undefaced.nii.gz"
        marker.write_bytes(b"sensitive")

    assert not workspace.exists()


@pytest.mark.parametrize("value", ["unsafe\npath", "unsafe\x00path"])
def test_sensitive_workspace_rejects_control_characters_in_scratch_path(value):
    with pytest.raises(ConversionError, match="unsafe"):
        with sensitive_workspace({"SLURM_TMPDIR": value}):
            pass


def test_sensitive_workspace_rejects_symlinked_scratch_root(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ConversionError, match="unsafe"):
        with sensitive_workspace({"SLURM_TMPDIR": str(link)}):
            pass


def test_sensitive_workspace_uses_only_validated_job_identifiers(tmp_path):
    environ = {
        "SLURM_TMPDIR": str(tmp_path),
        "SLURM_JOB_ID": "12345",
        "SLURM_ARRAY_TASK_ID": "7",
    }

    with sensitive_workspace(environ) as workspace:
        assert workspace.name.startswith("network-fw2bids-sensitive-12345-7-")
        assert workspace.resolve().is_relative_to(tmp_path.resolve())

    invalid = dict(environ, SLURM_JOB_ID="123/45")
    with pytest.raises(ConversionError, match="SLURM_JOB_ID"):
        with sensitive_workspace(invalid):
            pass


def test_sensitive_workspace_removes_orphan_with_the_production_job_name_pattern(tmp_path):
    stale = Path(mkdtemp(prefix="network-fw2bids-sensitive-12345-7-", dir=tmp_path))
    (stale / "sensitive").write_text("data")
    unrelated = tmp_path / "network-fw2bids-sensitive-unrelated"
    unrelated.mkdir()
    environ = {
        "SLURM_TMPDIR": str(tmp_path),
        "SLURM_JOB_ID": "12345",
        "SLURM_ARRAY_TASK_ID": "7",
    }

    with sensitive_workspace(environ):
        assert not stale.exists()
        assert unrelated.is_dir()


def test_sensitive_workspace_never_removes_unrelated_tempfile_shaped_directories(tmp_path):
    unrelated = [tmp_path / "abcdefgh", tmp_path / "datasets"]
    for directory in unrelated:
        directory.mkdir()
    environ = {
        "SLURM_TMPDIR": str(tmp_path),
        "SLURM_JOB_ID": "12345",
        "SLURM_ARRAY_TASK_ID": "7",
    }

    with sensitive_workspace(environ):
        assert all(directory.is_dir() for directory in unrelated)


def test_sensitive_workspace_restores_signal_handlers(tmp_path):
    previous = __import__("signal").getsignal(__import__("signal").SIGTERM)

    with sensitive_workspace({"SLURM_TMPDIR": str(tmp_path)}):
        assert __import__("signal").getsignal(__import__("signal").SIGTERM) != previous

    assert __import__("signal").getsignal(__import__("signal").SIGTERM) == previous
