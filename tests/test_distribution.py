from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import unittest
import zipfile


class TestDistribution(unittest.TestCase):
    def test_built_archives_exclude_local_workflow_artifacts(self) -> None:
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as scratch:
            source = Path(scratch) / "source"
            source.mkdir()
            for name in ("src", "tests", "scripts"):
                shutil.copytree(project / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
            if (project / "docs").is_dir():
                shutil.copytree(project / "docs", source / "docs")
            for name in ("README.md", "pyproject.toml", "uv.lock", "final_sample_subjects.txt"):
                shutil.copy2(project / name, source / name)
            workflow = source / ".superpowers/sdd"
            workflow.mkdir(parents=True)
            (workflow / "local-review.md").write_text("Internal review fixture")
            output = Path(scratch) / "dist"
            result = subprocess.run(
                ["uv", "build", "--out-dir", str(output)],
                cwd=source, capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
                source_files = {
                    str(PurePosixPath(member.name).relative_to(PurePosixPath(member.name).parts[0]))
                    for member in archive.getmembers()
                }
            with zipfile.ZipFile(next(output.glob("*.whl"))) as archive:
                wheel_files = set(archive.namelist())
            for files in (source_files, wheel_files):
                self.assertFalse(
                    [name for name in files if ".superpowers" in PurePosixPath(name).parts],
                    "release archives must not contain local workflow artifacts",
                )
            self.assertTrue({
                "README.md", "pyproject.toml", "uv.lock", "final_sample_subjects.txt",
                "scripts/run_subject_conversion.py", "scripts/submit_all_subjects.sh",
                "src/network_fw2bids/api.py", "tests/test_conversion.py",
            }.issubset(source_files))
            self.assertIn("network_fw2bids/api.py", wheel_files)
