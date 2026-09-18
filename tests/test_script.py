import inspect
import io
import json
import os
import runpy
import sys
import tempfile
import types
import unittest
import zipfile
from datetime import UTC, datetime
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "test.py"


def load_script() -> dict:
    return runpy.run_path(SCRIPT, run_name="network_fw2bids_test_script")


class Finder:
    def __init__(self, items: list) -> None:
        self.items = items

    def find_first(self, query: str):
        label = query.split('"')[1]
        return next((item for item in self.items if item.label == label), None)


def dicom(name: str = "scan.dicom.zip"):
    return types.SimpleNamespace(name=name, type="dicom")


def acquisition(label: str, timestamp: datetime, files: list | None = None):
    return types.SimpleNamespace(
        id=f"acq-{label}",
        label=label,
        timestamp=timestamp,
        files=files if files is not None else [dicom()],
    )


def session(label: str, timestamp: datetime, acquisitions: list):
    return types.SimpleNamespace(
        label=label,
        timestamp=timestamp,
        acquisitions=lambda: acquisitions,
    )


class TestScriptImport(unittest.TestCase):
    def test_import_has_no_flywheel_side_effects(self) -> None:
        client_calls: list[str] = []
        project = types.SimpleNamespace(label="r01network", id="project-id", subjects=lambda: [])
        group = types.SimpleNamespace(projects=lambda: [project])
        client = types.SimpleNamespace(
            get_current_user=lambda: types.SimpleNamespace(
                firstname="Test", lastname="User", email="test@example.com"
            ),
            lookup=lambda path: {"russpold": group, "russpold/r01network": project}[path],
        )
        flywheel = types.ModuleType("flywheel")
        flywheel.Subject = object

        def make_client(token: str):
            client_calls.append(token)
            return client

        flywheel.Client = make_client

        with (
            patch.dict(os.environ, {"FLYWHEEL_API_TOKEN": "test-token"}),
            patch.dict(sys.modules, {"flywheel": flywheel}),
        ):
            runpy.run_path(SCRIPT, run_name="network_fw2bids_test_script")

        self.assertEqual(client_calls, [])


class TestPlanning(unittest.TestCase):
    def test_plans_known_acquisitions_in_chronological_sessions(self) -> None:
        early = session(
            "accession-early",
            datetime(2024, 1, 1, tzinfo=UTC),
            [
                acquisition(
                    "NEW Sag_MPRAGE_T1", datetime(2024, 1, 1, 1, tzinfo=UTC)
                )
            ],
        )
        late = session(
            "accession-late",
            datetime(2024, 1, 2, tzinfo=UTC),
            [
                acquisition("3Plane Loc SSFSE", datetime(2024, 1, 2, 1, tzinfo=UTC)),
                acquisition("task-flanker_bold", datetime(2024, 1, 2, 2, tzinfo=UTC)),
            ],
        )
        subject = types.SimpleNamespace(
            label="s03", sessions=lambda: [late, early]
        )
        project = types.SimpleNamespace(subjects=Finder([subject]))
        client = types.SimpleNamespace(
            lookup=lambda path: project if path == "russpold/r01network" else None
        )

        namespace = load_script()
        self.assertIn("FlywheelBIDS", namespace)
        converter = namespace["FlywheelBIDS"](client)

        plans = converter.plan_subject("s03")

        self.assertEqual(
            [str(plan.relative_prefix) for plan in plans],
            [
                "sub-s03/ses-01/anat/sub-s03_ses-01_acq-SagMPRAGE_run-1_T1w",
                "sub-s03/ses-02/func/sub-s03_ses-02_task-flanker_run-1_bold",
            ],
        )

    def test_reassigns_mislabeled_session_to_canonical_subject(self) -> None:
        s03 = types.SimpleNamespace(
            label="s03",
            sessions=lambda: [
                session(
                    "22461",
                    datetime(2024, 1, 1, tzinfo=UTC),
                    [
                        acquisition(
                            "task-flanker_bold",
                            datetime(2024, 1, 1, 1, tzinfo=UTC),
                        )
                    ],
                ),
                session(
                    "22752",
                    datetime(2024, 1, 2, tzinfo=UTC),
                    [
                        acquisition(
                            "task-stopSignal_bold",
                            datetime(2024, 1, 2, 1, tzinfo=UTC),
                        )
                    ],
                ),
            ],
        )
        s10 = types.SimpleNamespace(
            label="s10",
            sessions=lambda: [
                session(
                    "23000",
                    datetime(2024, 1, 3, tzinfo=UTC),
                    [
                        acquisition(
                            "task-rest_bold",
                            datetime(2024, 1, 3, 1, tzinfo=UTC),
                        )
                    ],
                )
            ],
        )
        project = types.SimpleNamespace(subjects=Finder([s03, s10]))
        client = types.SimpleNamespace(
            lookup=lambda path: project if path == "russpold/r01network" else None
        )
        converter = load_script()["FlywheelBIDS"](client)

        s03_plans = converter.plan_subject("s03")
        s10_plans = converter.plan_subject("s10")

        self.assertEqual(
            [plan.acquisition.label for plan in s03_plans], ["task-flanker_bold"]
        )
        self.assertEqual(
            [plan.acquisition.label for plan in s10_plans],
            ["task-stopSignal_bold", "task-rest_bold"],
        )
        self.assertTrue(
            all(str(plan.relative_prefix).startswith("sub-s10/") for plan in s10_plans)
        )


class DownloadingAcquisition:
    def __init__(self, label: str, timestamp: datetime) -> None:
        self.id = "acquisition-id"
        self.label = label
        self.timestamp = timestamp
        self.files = [dicom()]
        self.downloads: list[str] = []

    def download_file(self, name: str, destination: str) -> None:
        self.downloads.append(name)
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("dicoms/image.dcm", b"synthetic dicom")


class TestConversion(unittest.TestCase):
    def test_downloads_converts_and_places_one_functional_archive(self) -> None:
        source = DownloadingAcquisition(
            "task-flanker_bold", datetime(2024, 1, 1, tzinfo=UTC)
        )
        subject = types.SimpleNamespace(
            label="s03",
            sessions=lambda: [
                session(
                    "accession",
                    datetime(2024, 1, 1, tzinfo=UTC),
                    [source],
                )
            ],
        )
        project = types.SimpleNamespace(subjects=Finder([subject]))
        client = types.SimpleNamespace(
            lookup=lambda path: project if path == "russpold/r01network" else None
        )
        commands: list[list[str]] = []

        def run_converter(command: list[str], **_kwargs):
            commands.append(command)
            converted = Path(command[command.index("-o") + 1])
            converted.mkdir(parents=True, exist_ok=True)
            (converted / "converted.nii.gz").write_bytes(b"nifti")
            (converted / "converted.json").write_text('{"RepetitionTime": 1.49}')
            return types.SimpleNamespace(returncode=0)

        namespace = load_script()
        self.assertTrue(hasattr(namespace["FlywheelBIDS"], "convert_subject"))
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "bids"
            converter = namespace["FlywheelBIDS"](client, runner=run_converter)

            converter.convert_subject("s03", destination)

            prefix = (
                destination
                / "sub-s03/ses-01/func/sub-s03_ses-01_task-flanker_run-1_bold"
            )
            self.assertEqual((prefix.with_suffix(".json")).exists(), True)
            self.assertEqual(
                Path(f"{prefix}.nii.gz").read_bytes(),
                b"nifti",
            )
            self.assertIn('"TaskName": "flanker"', prefix.with_suffix(".json").read_text())

        self.assertEqual(source.downloads, ["scan.dicom.zip"])
        self.assertEqual(commands[0][0], "dcm2niix")

    def test_places_fieldmap_magnitude_and_phase_outputs(self) -> None:
        source = DownloadingAcquisition(
            "fmap-fieldmap", datetime(2024, 1, 1, tzinfo=UTC)
        )
        subject = types.SimpleNamespace(
            label="s03",
            sessions=lambda: [
                session(
                    "accession",
                    datetime(2024, 1, 1, tzinfo=UTC),
                    [source],
                )
            ],
        )
        project = types.SimpleNamespace(subjects=Finder([subject]))
        client = types.SimpleNamespace(
            lookup=lambda path: project if path == "russpold/r01network" else None
        )

        def run_converter(command: list[str], **_kwargs):
            converted = Path(command[command.index("-o") + 1])
            converted.mkdir(parents=True, exist_ok=True)
            for stem, image_type in (("converted", "M"), ("converted_ph", "P")):
                (converted / f"{stem}.nii.gz").write_bytes(stem.encode())
                (converted / f"{stem}.json").write_text(
                    json.dumps({"ImageType": ["ORIGINAL", "PRIMARY", image_type]})
                )
            return types.SimpleNamespace(returncode=0)

        namespace = load_script()
        self.assertTrue(
            hasattr(namespace["FlywheelBIDS"], "_place_converted_images")
        )
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "bids"
            converter = namespace["FlywheelBIDS"](client, runner=run_converter)

            converter.convert_subject("s03", destination)

            fmap = destination / "sub-s03/ses-01/fmap"
            self.assertEqual(
                sorted(path.name for path in fmap.glob("*.nii.gz")),
                [
                    "sub-s03_ses-01_run-1_fieldmap.nii.gz",
                    "sub-s03_ses-01_run-1_magnitude.nii.gz",
                ],
            )
            metadata = json.loads(
                (fmap / "sub-s03_ses-01_run-1_fieldmap.json").read_text()
            )
            self.assertEqual(metadata["Units"], "Hz")


class TestCommandLine(unittest.TestCase):
    def test_dry_run_prints_plan_without_downloading(self) -> None:
        source = DownloadingAcquisition(
            "task-flanker_bold", datetime(2024, 1, 1, tzinfo=UTC)
        )
        subject = types.SimpleNamespace(
            label="s03",
            sessions=lambda: [
                session(
                    "accession",
                    datetime(2024, 1, 1, tzinfo=UTC),
                    [source],
                )
            ],
        )
        project = types.SimpleNamespace(subjects=Finder([subject]))
        client = types.SimpleNamespace(
            lookup=lambda path: project if path == "russpold/r01network" else None
        )
        namespace = load_script()
        self.assertIn("argv", inspect.signature(namespace["main"]).parameters)
        output = io.StringIO()

        with (
            patch.dict(os.environ, {"FLYWHEEL_API_TOKEN": "test-token"}),
            redirect_stdout(output),
        ):
            result = namespace["main"](
                ["--subject", "s03"], client_factory=lambda _token: client
            )

        self.assertEqual(result, 0)
        self.assertIn(
            "sub-s03/ses-01/func/sub-s03_ses-01_task-flanker_run-1_bold",
            output.getvalue(),
        )
        self.assertEqual(source.downloads, [])


if __name__ == "__main__":
    unittest.main()
