import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from network_fw2bids._assembly import assemble_subject_parts, load_subjects
from network_fw2bids.errors import ConversionError


FINAL_SAMPLE = (
    "s03", "s10", "s19", "s29", "s43",
    "s76", "s180", "s216", "s247", "s286", "s295", "s300", "s320",
    "s321", "s336", "s373", "s394", "s415", "s480", "s599", "s645",
    "s874", "s956", "s1035", "s1057", "s1058", "s1127", "s1134",
    "s1175", "s1189", "s1258", "s1267", "s1270", "s1273", "s1292",
    "s1314", "s1326", "s1338", "s1351", "s1391", "s1399", "s1402",
    "s1408", "s1445", "s1481", "s1486",
)


class TestFinalSampleRoster(unittest.TestCase):
    def test_roster_contains_the_exact_46_final_sample_subjects(self) -> None:
        project = Path(__file__).resolve().parents[1]
        subjects = load_subjects(project / "final_sample_subjects.txt")
        self.assertEqual(subjects, FINAL_SAMPLE)
        self.assertEqual(len(subjects), 46)
        self.assertEqual(len(set(subjects)), 46)

    def test_rejects_duplicate_or_unsafe_subjects(self) -> None:
        with TemporaryDirectory() as scratch:
            roster = Path(scratch) / "subjects.txt"
            for body in ("s03\ns03\n", "s03\n../s10\n", "s03\n\n"):
                with self.subTest(body=body):
                    roster.write_text(body)
                    with self.assertRaises(ConversionError):
                        load_subjects(roster)


class TestSubjectPartAssembly(unittest.TestCase):
    def _write_part(self, parts: Path, subject: str, description: dict | None = None) -> None:
        part = parts / subject
        (part / f"sub-{subject}" / "anat").mkdir(parents=True)
        (part / f"sub-{subject}" / "anat" / f"sub-{subject}_T1w.nii.gz").write_bytes(b"nii")
        (part / f"sub-{subject}" / "anat" / f"sub-{subject}_T1w.json").write_text("{}")
        (part / "dataset_description.json").write_text(
            json.dumps(description or {"Name": "r01network export", "BIDSVersion": "1.10.1"})
        )

    def test_assembles_complete_parts_into_one_atomic_dataset(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            roster = root / "subjects.txt"
            roster.write_text("s03\ns10\n")
            parts = root / "parts"
            self._write_part(parts, "s03")
            self._write_part(parts, "s10")

            destination = root / "bids"
            assemble_subject_parts(roster, parts, destination)

            self.assertTrue((destination / "sub-s03/anat/sub-s03_T1w.nii.gz").is_file())
            self.assertTrue((destination / "sub-s10/anat/sub-s10_T1w.nii.gz").is_file())
            self.assertEqual(
                json.loads((destination / "dataset_description.json").read_text())["Name"],
                "r01network export",
            )
            self.assertTrue((parts / "s03").is_dir(), "parts remain available for audit")

    def test_missing_or_inconsistent_part_publishes_nothing(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            roster = root / "subjects.txt"
            roster.write_text("s03\ns10\n")
            parts = root / "parts"
            self._write_part(parts, "s03")
            destination = root / "bids"

            with self.assertRaises(ConversionError):
                assemble_subject_parts(roster, parts, destination)
            self.assertFalse(destination.exists())

    def test_empty_malformed_or_symlinked_subject_tree_publishes_nothing(self) -> None:
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            roster = root / "subjects.txt"
            roster.write_text("s03\n")
            parts = root / "parts"
            destination = root / "bids"
            part = parts / "s03"
            (part / "sub-s03").mkdir(parents=True)
            (part / "dataset_description.json").write_text(
                json.dumps({"Name": "r01network export", "BIDSVersion": "1.10.1"})
            )

            with self.assertRaises(ConversionError):
                assemble_subject_parts(roster, parts, destination)
            self.assertFalse(destination.exists())

            image = part / "sub-s03/sub-s03_T1w.nii.gz"
            image.write_bytes(b"nii")
            with self.assertRaises(ConversionError):
                assemble_subject_parts(roster, parts, destination)
            self.assertFalse(destination.exists())

            sidecar = part / "sub-s03/sub-s03_T1w.json"
            external = root / "external.json"
            external.write_text("{}")
            sidecar.symlink_to(external)
            with self.assertRaises(ConversionError):
                assemble_subject_parts(roster, parts, destination)
            self.assertFalse(destination.exists())

            self._write_part(parts, "s10", {"Name": "wrong", "BIDSVersion": "1.10.1"})
            with self.assertRaises(ConversionError):
                assemble_subject_parts(roster, parts, destination)
            self.assertFalse(destination.exists())


class TestSherlockSubmission(unittest.TestCase):
    def _fake_submission_environment(self, root: Path) -> dict[str, str]:
        fake_bin = root / "bin"
        fake_bin.mkdir()
        call_log = root / "calls.log"
        state = root / "sbatch-state"
        (fake_bin / "uv").write_text(
            "#!/bin/bash\necho \"uv $*\" >> \"$CALL_LOG\"\n"
        )
        (fake_bin / "sbatch").write_text(
            "#!/bin/bash\n"
            "echo \"sbatch $*\" >> \"$CALL_LOG\"\n"
            "if [[ -e \"$SBATCH_STATE\" ]]; then echo 222; else touch \"$SBATCH_STATE\"; echo 111; fi\n"
        )
        (fake_bin / "uv").chmod(0o755)
        (fake_bin / "sbatch").chmod(0o755)
        env_file = root / ".env"
        env_file.write_text("FLYWHEEL_API_TOKEN=secret\n")
        return {
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "CALL_LOG": str(call_log),
            "SBATCH_STATE": str(state),
            "BIDS_DIR": str(root / "final-bids"),
            "FLYWHEEL_ENV_FILE": str(env_file),
        }

    def test_submitter_chains_finalizer_after_the_array(self) -> None:
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            env = self._fake_submission_environment(root)
            env["THROTTLE"] = "4"

            result = subprocess.run(
                ["bash", str(project / "scripts/submit_all_subjects.sh")],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            calls = Path(env["CALL_LOG"]).read_text()
            self.assertIn("uv sync --frozen", calls)
            self.assertIn("--array=0-45%4", calls)
            self.assertIn("--dependency=afterok:111", calls)
            self.assertIn("array job: 111", result.stdout)
            self.assertIn("finalizer job: 222", result.stdout)

    def test_submitter_rejects_symlinked_or_overlapping_work_paths(self) -> None:
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            env = self._fake_submission_environment(root)
            real_parts = root / "real-parts"
            real_parts.mkdir()
            linked_parts = root / "linked-parts"
            linked_parts.symlink_to(real_parts, target_is_directory=True)
            env["PARTS_DIR"] = str(linked_parts)

            result = subprocess.run(
                ["bash", str(project / "scripts/submit_all_subjects.sh")],
                cwd=project, env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(Path(env["CALL_LOG"]).exists())

            env["PARTS_DIR"] = str(root / "parts")
            env["LOG_DIR"] = str(Path(env["BIDS_DIR"]) / "logs")
            result = subprocess.run(
                ["bash", str(project / "scripts/submit_all_subjects.sh")],
                cwd=project, env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(Path(env["CALL_LOG"]).exists())

    def test_array_task_selects_one_subject_and_one_part_directory(self) -> None:
        project = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as scratch:
            root = Path(scratch)
            roster = root / "subjects.txt"
            roster.write_text("s03\ns10\n")
            env_file = root / ".env"
            env_file.write_text("FLYWHEEL_API_TOKEN=secret\n")
            calls = root / "converter.log"
            converter = root / "network-fw2bids"
            converter.write_text(
                "#!/bin/bash\n"
                "[[ -n \"${FLYWHEEL_API_TOKEN:-}\" ]] || exit 9\n"
                "printf '%s\\n' \"$*\" > \"$CALL_LOG\"\n"
            )
            converter.chmod(0o755)
            parts = root / "parts"
            parts.mkdir()
            env = {
                **os.environ,
                "SLURM_ARRAY_TASK_ID": "1",
                "SUBJECTS_FILE": str(roster),
                "PARTS_DIR": str(parts),
                "FLYWHEEL_ENV_FILE": str(env_file),
                "NETWORK_FW2BIDS_BIN": str(converter),
                "CALL_LOG": str(calls),
                "PROJECT_PATH": "russpold/r01network",
            }

            result = subprocess.run(
                ["bash", str(project / "scripts/convert_subject_array.sbatch")],
                cwd=project,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            invocation = calls.read_text()
            self.assertIn("--subject s10", invocation)
            self.assertIn(f"--output {parts / 's10'}", invocation)
            self.assertIn("--project russpold/r01network", invocation)


if __name__ == "__main__":
    unittest.main()
