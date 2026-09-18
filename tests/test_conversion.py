from pathlib import Path
from subprocess import CalledProcessError
import stat
from tempfile import TemporaryDirectory
from unittest.mock import Mock
import json
import unittest
import zipfile

from network_fw2bids.conversion import DicomConverter
from network_fw2bids.errors import ConversionError
from network_fw2bids.planning import ArchivePlan


class FakeDicomFile:
    name = "scan.dicom.zip"


class FakeAcquisition:
    def __init__(self, label: str, archive_writer) -> None:
        self.label = label
        self._archive_writer = archive_writer

    def download_file(self, name: str, destination: str) -> None:
        if name != FakeDicomFile.name:
            raise AssertionError(f"unexpected DICOM file {name!r}")
        self._archive_writer(Path(destination))


class TestDicomConverter(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.destination = self.root / "bids"
        self.archive_writer = lambda path: self.write_zip(path, {"scan.dcm": b"dicom"})
        self.runner = Mock(side_effect=self.write_functional_output)
        self.converter = DicomConverter(runner=self.runner)
        self.functional_plan = self.plan(
            "func",
            "sub-s03/ses-01/func/sub-s03_ses-01_task-flanker_run-1_bold",
            task="flanker",
        )
        self.fieldmap_plan = self.plan(
            "fmap", "sub-s03/ses-01/fmap/sub-s03_ses-01_run-1"
        )
        self.dwi_plan = self.plan(
            "dwi",
            "sub-s03/ses-01/dwi/sub-s03_ses-01_acq-g105_dir-AP_run-1_dwi",
        )

    def tearDown(self) -> None:
        self.assertFalse(list(self.root.glob(".network-fw2bids-*")))

    def plan(self, modality: str, relative_prefix: str, task: str | None = None) -> ArchivePlan:
        return ArchivePlan(
            acquisition=FakeAcquisition("test acquisition", lambda path: self.archive_writer(path)),
            dicom_file=FakeDicomFile(),
            relative_prefix=Path(relative_prefix),
            modality=modality,
            task=task,
        )

    @staticmethod
    def write_zip(path: Path, contents: dict[str, bytes]) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in contents.items():
                archive.writestr(name, content)

    @staticmethod
    def write_symlink_zip(path: Path) -> None:
        member = zipfile.ZipInfo("linked.dcm")
        member.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(member, b"target.dcm")

    @staticmethod
    def output_directory(command: list[str]) -> Path:
        return Path(command[command.index("-o") + 1])

    @staticmethod
    def write_output(
        directory: Path,
        stem: str,
        metadata: dict | None = None,
        companions: dict[str, bytes] | None = None,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{stem}.nii.gz").write_bytes(b"nifti")
        (directory / f"{stem}.json").write_text(json.dumps(metadata or {}))
        for extension, content in (companions or {}).items():
            (directory / f"{stem}{extension}").write_bytes(content)

    def write_functional_output(self, command: list[str], **kwargs) -> None:
        self.assertTrue(kwargs["check"])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.write_output(self.output_directory(command), "converted")

    def test_converts_functional_archive_and_adds_task_name(self) -> None:
        self.converter.convert([self.functional_plan], self.destination, "r01network")

        prefix = self.destination / self.functional_plan.relative_prefix
        self.assertEqual(Path(f"{prefix}.nii.gz").read_bytes(), b"nifti")
        self.assertEqual(
            json.loads(prefix.with_suffix(".json").read_text())["TaskName"], "flanker"
        )
        self.assertEqual(
            json.loads((self.destination / "dataset_description.json").read_text()),
            {"Name": "r01network export", "BIDSVersion": "1.10.1"},
        )

    def test_invokes_dcm2niix_with_required_arguments(self) -> None:
        self.converter.convert([self.functional_plan], self.destination, "r01network")

        command = self.runner.call_args.args[0]
        converted_directory = self.output_directory(command)
        dicom_directory = Path(command[-1])
        self.assertEqual(
            command,
            [
                "dcm2niix",
                "-b",
                "y",
                "-ba",
                "y",
                "-z",
                "y",
                "-f",
                "converted",
                "-o",
                str(converted_directory),
                str(dicom_directory),
            ],
        )
        self.assertEqual(converted_directory.name, "converted")
        self.assertEqual(dicom_directory.name, "dicoms")

    def test_places_fieldmap_outputs_and_units(self) -> None:
        def write_fieldmap_outputs(command: list[str], **kwargs) -> None:
            directory = self.output_directory(command)
            self.write_output(directory, "converted_mag")
            self.write_output(directory, "converted_ph", {"ComplexImageComponent": "PHASE"})

        self.converter = DicomConverter(runner=write_fieldmap_outputs)
        self.converter.convert([self.fieldmap_plan], self.destination, "r01network")

        fieldmap = self.destination / "sub-s03/ses-01/fmap/sub-s03_ses-01_run-1_fieldmap.json"
        magnitude = self.destination / "sub-s03/ses-01/fmap/sub-s03_ses-01_run-1_magnitude.nii.gz"
        self.assertEqual(json.loads(fieldmap.read_text())["Units"], "Hz")
        self.assertTrue(magnitude.exists())

    def test_copies_dwi_gradient_companions(self) -> None:
        def write_dwi_output(command: list[str], **kwargs) -> None:
            self.write_output(
                self.output_directory(command),
                "converted",
                companions={".bval": b"0 1000", ".bvec": b"1 0 0"},
            )

        self.converter = DicomConverter(runner=write_dwi_output)
        self.converter.convert([self.dwi_plan], self.destination, "r01network")

        prefix = self.destination / self.dwi_plan.relative_prefix
        self.assertEqual(prefix.with_suffix(".bval").read_bytes(), b"0 1000")
        self.assertEqual(prefix.with_suffix(".bvec").read_bytes(), b"1 0 0")

    def test_rejects_zip_path_traversal(self) -> None:
        self.archive_writer = lambda path: self.write_zip(path, {"../escape.dcm": b"bad"})

        with self.assertRaisesRegex(ConversionError, "unsafe path"):
            self.converter.convert([self.functional_plan], self.destination, "r01network")
        self.assertFalse(self.destination.exists())

    def test_rejects_absolute_zip_path(self) -> None:
        self.archive_writer = lambda path: self.write_zip(path, {"/escape.dcm": b"bad"})

        with self.assertRaisesRegex(ConversionError, "unsafe path"):
            self.converter.convert([self.functional_plan], self.destination, "r01network")
        self.assertFalse(self.destination.exists())

    def test_rejects_zip_symbolic_link(self) -> None:
        self.archive_writer = self.write_symlink_zip

        with self.assertRaisesRegex(ConversionError, "symbolic link"):
            self.converter.convert([self.functional_plan], self.destination, "r01network")
        self.assertFalse(self.destination.exists())

    def test_failure_leaves_no_destination(self) -> None:
        self.converter = DicomConverter(
            runner=Mock(side_effect=CalledProcessError(1, ["dcm2niix"]))
        )

        with self.assertRaises(ConversionError):
            self.converter.convert([self.functional_plan], self.destination, "r01network")

        self.assertFalse(self.destination.exists())

    def test_wraps_failed_archive_download(self) -> None:
        def fail_download(path: Path) -> None:
            raise OSError("download failed")

        self.archive_writer = fail_download

        with self.assertRaises(ConversionError) as raised:
            self.converter.convert([self.functional_plan], self.destination, "r01network")

        self.assertIsInstance(raised.exception.__cause__, OSError)
        self.assertFalse(self.destination.exists())

    def test_wraps_missing_archive_download(self) -> None:
        self.archive_writer = lambda path: None

        with self.assertRaises(ConversionError) as raised:
            self.converter.convert([self.functional_plan], self.destination, "r01network")

        self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)
        self.assertFalse(self.destination.exists())

    def test_rejects_missing_json_sidecar(self) -> None:
        def write_image_without_sidecar(command: list[str], **kwargs) -> None:
            directory = self.output_directory(command)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "converted.nii.gz").write_bytes(b"nifti")

        self.converter = DicomConverter(runner=write_image_without_sidecar)

        with self.assertRaisesRegex(ConversionError, "JSON sidecar"):
            self.converter.convert([self.functional_plan], self.destination, "r01network")

    def test_wraps_malformed_json_sidecar_with_its_cause(self) -> None:
        def write_malformed_sidecar(command: list[str], **kwargs) -> None:
            directory = self.output_directory(command)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "converted.nii.gz").write_bytes(b"nifti")
            (directory / "converted.json").write_text("{")

        self.converter = DicomConverter(runner=write_malformed_sidecar)

        with self.assertRaises(ConversionError) as raised:
            self.converter.convert([self.functional_plan], self.destination, "r01network")

        self.assertIsInstance(raised.exception.__cause__, json.JSONDecodeError)
        self.assertFalse(self.destination.exists())

    def test_rejects_multiple_non_fieldmap_images(self) -> None:
        def write_two_images(command: list[str], **kwargs) -> None:
            directory = self.output_directory(command)
            self.write_output(directory, "first")
            self.write_output(directory, "second")

        self.converter = DicomConverter(runner=write_two_images)

        with self.assertRaisesRegex(ConversionError, "expected one converted image"):
            self.converter.convert([self.functional_plan], self.destination, "r01network")

    def test_rejects_missing_fieldmap_role(self) -> None:
        for stem, metadata in (
            ("converted_mag", {}),
            ("converted_ph", {"ComplexImageComponent": "PHASE"}),
        ):
            with self.subTest(stem=stem):
                def write_one_image(command: list[str], **kwargs) -> None:
                    self.write_output(self.output_directory(command), stem, metadata)

                self.converter = DicomConverter(runner=write_one_image)

                with self.assertRaisesRegex(ConversionError, "magnitude and phase"):
                    self.converter.convert(
                        [self.fieldmap_plan], self.destination, "r01network"
                    )

    def test_rejects_duplicate_fieldmap_roles(self) -> None:
        def write_duplicate_magnitudes(command: list[str], **kwargs) -> None:
            directory = self.output_directory(command)
            self.write_output(directory, "first")
            self.write_output(directory, "second")
            self.write_output(directory, "phase_ph", {"ImageType": ["P"]})

        self.converter = DicomConverter(runner=write_duplicate_magnitudes)

        with self.assertRaisesRegex(ConversionError, "multiple magnitude"):
            self.converter.convert([self.fieldmap_plan], self.destination, "r01network")

    def test_rejects_existing_destination(self) -> None:
        self.destination.mkdir()

        with self.assertRaisesRegex(ConversionError, "already exists"):
            self.converter.convert([self.functional_plan], self.destination, "r01network")
        self.runner.assert_not_called()

    def test_rejects_plans_with_colliding_staged_outputs(self) -> None:
        def write_distinct_output(command: list[str], **kwargs) -> None:
            self.write_output(
                self.output_directory(command),
                "converted",
                {"call": self.runner.call_count},
            )

        self.runner = Mock(side_effect=write_distinct_output)
        self.converter = DicomConverter(runner=self.runner)
        duplicate = self.plan(
            "func",
            "sub-s03/ses-01/func/sub-s03_ses-01_task-flanker_run-1_bold",
            task="flanker",
        )

        with self.assertRaisesRegex(ConversionError, "already exists"):
            self.converter.convert(
                [self.functional_plan, duplicate], self.destination, "r01network"
            )

        self.assertEqual(self.runner.call_count, 2)
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
