from pathlib import Path
from subprocess import CalledProcessError
import stat
import hashlib
import os
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
import json
import unittest
import zipfile

from network_fw2bids.conversion import DicomConverter
from network_fw2bids.defacing import DefaceConfig
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
        self.node_tmp = self.root / "node-tmp"
        self.node_tmp.mkdir()
        self.environment = patch.dict(os.environ, {"SLURM_TMPDIR": str(self.node_tmp)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        image = self.root / "pydeface.sif"
        image.write_bytes(b"pinned test image")
        self.deface_config = DefaceConfig(
            image=image,
            version="2.1.0",
            sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
        )
        self.destination = self.root / "bids"
        self.archive_writer = lambda path: self.write_zip(path, {"scan.dcm": b"dicom"})
        self.runner = Mock(side_effect=self.write_functional_output)
        self.converter = DicomConverter(runner=self.runner, deface_config=self.deface_config)
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

        self.converter = DicomConverter(runner=write_fieldmap_outputs, deface_config=self.deface_config)
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

        self.converter = DicomConverter(runner=write_dwi_output, deface_config=self.deface_config)
        self.converter.convert([self.dwi_plan], self.destination, "r01network")

        prefix = self.destination / self.dwi_plan.relative_prefix
        self.assertEqual(prefix.with_suffix(".bval").read_bytes(), b"0 1000")
        self.assertEqual(prefix.with_suffix(".bvec").read_bytes(), b"1 0 0")

    def test_rejects_each_missing_dwi_gradient_combination(self) -> None:
        for missing in ((".bval",), (".bvec",), (".bval", ".bvec")):
            with self.subTest(missing=missing):
                def write_incomplete_dwi(command: list[str], **kwargs) -> None:
                    self.write_output(
                        self.output_directory(command),
                        "converted",
                        companions={
                            extension: b"0 1000"
                            for extension in (".bval", ".bvec")
                            if extension not in missing
                        },
                    )

                destination = self.root / ("missing" + "".join(missing))
                with self.assertRaisesRegex(ConversionError, "gradient"):
                    DicomConverter(write_incomplete_dwi, self.deface_config).convert(
                        [self.dwi_plan], destination, "r01network"
                    )
                self.assertFalse(destination.exists())

    def test_rejects_unusable_dwi_gradients(self) -> None:
        for extension in (".bval", ".bvec"):
            for invalid in (b"", b" \n", "directory"):
                with self.subTest(extension=extension, invalid=invalid):
                    def write_unusable_dwi(command: list[str], **kwargs) -> None:
                        directory = self.output_directory(command)
                        self.write_output(directory, "converted")
                        for companion in (".bval", ".bvec"):
                            path = directory / f"converted{companion}"
                            if companion == extension and invalid == "directory":
                                path.mkdir()
                            else:
                                path.write_bytes(invalid if companion == extension else b"0 1")

                    destination = self.root / f"unusable-{extension}-{invalid!r}"
                    with self.assertRaisesRegex(ConversionError, "gradient"):
                        DicomConverter(write_unusable_dwi, self.deface_config).convert(
                            [self.dwi_plan], destination, "r01network"
                        )
                    self.assertFalse(destination.exists())

    def test_rejects_all_unsafe_plan_prefixes_before_download_or_write(self) -> None:
        prefixes = (
            self.root / "escaped/scan", Path("../../escaped/scan"),
            Path("sub-s03/../scan"), Path("."), Path("C:/escaped/scan"),
            Path(r"..\escaped\scan"), Path("scan\0suffix"),
        )
        for prefix in prefixes:
            for modality in ("func", "fmap"):
                with self.subTest(prefix=prefix, modality=modality):
                    self.archive_writer = Mock()
                    destination = self.root / "new-parent/bids"
                    unsafe = self.plan(modality, str(prefix))
                    with self.assertRaisesRegex(ConversionError, "unsafe.*prefix"):
                        self.converter.convert(
                            [self.functional_plan, unsafe], destination, "r01network"
                        )
                    self.archive_writer.assert_not_called()
                    self.runner.assert_not_called()
                    self.assertFalse((self.root / "new-parent").exists())

    def test_rejects_ordinary_and_fieldmap_outputs_resolving_outside_staging(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        for plan in (self.functional_plan, self.fieldmap_plan):
            with self.subTest(modality=plan.modality):
                def write_outputs_and_redirect_subject(command: list[str], **kwargs) -> None:
                    directory = self.output_directory(command)
                    staged = directory.parent.parent / "bids"
                    (staged / "sub-s03").symlink_to(outside, target_is_directory=True)
                    self.write_output(directory, "converted_mag")
                    if plan.modality == "fmap":
                        self.write_output(directory, "converted_ph", {"ImageType": ["P"]})

                with self.assertRaisesRegex(ConversionError, "outside.*staging"):
                    DicomConverter(write_outputs_and_redirect_subject, self.deface_config).convert(
                        [plan], self.root / plan.modality, "r01network"
                    )
                self.assertEqual(list(outside.iterdir()), [])
                self.assertFalse((self.root / plan.modality).exists())

    def test_publication_preserves_destination_created_during_conversion(self) -> None:
        created = []

        def write_output_and_create_destination(command: list[str], **kwargs) -> None:
            self.write_functional_output(command, **kwargs)
            self.destination.mkdir(mode=0o700)
            created.append(self.destination.stat())

        real_exists = Path.exists

        def stale_exists(path: Path) -> bool:
            # A userspace existence check can miss a concurrent mkdir.
            return False if path == self.destination else real_exists(path)

        with patch.object(Path, "exists", stale_exists):
            with self.assertRaises(ConversionError) as raised:
                DicomConverter(write_output_and_create_destination, self.deface_config).convert(
                    [self.functional_plan], self.destination, "r01network"
                )

        self.assertIsInstance(raised.exception.__cause__, FileExistsError)
        self.assertEqual(self.destination.stat().st_ino, created[0].st_ino)
        self.assertEqual(self.destination.stat().st_mode, created[0].st_mode)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_wraps_encrypted_zip_failure(self) -> None:
        def write_encrypted_zip(path: Path) -> None:
            self.write_zip(path, {"scan.dcm": b"dicom"})
            data = bytearray(path.read_bytes())
            data[6] |= 1  # Encryption flag in the local header.
            data[data.index(b"PK\x01\x02") + 8] |= 1  # Central directory flag.
            path.write_bytes(data)

        self.archive_writer = write_encrypted_zip
        with self.assertRaises(ConversionError) as raised:
            self.converter.convert([self.functional_plan], self.destination, "r01network")
        self.assertIsInstance(raised.exception.__cause__, RuntimeError)
        self.assertFalse(self.destination.exists())

    def test_rejects_malformed_fieldmap_metadata(self) -> None:
        for metadata in (
            {"ImageType": None}, {"ImageType": "P"}, {"ImageType": [None]},
            {"ComplexImageComponent": None}, {"ComplexImageComponent": ["PHASE"]},
        ):
            with self.subTest(metadata=metadata):
                def write_invalid_metadata(command: list[str], **kwargs) -> None:
                    directory = self.output_directory(command)
                    self.write_output(directory, "converted_mag")
                    self.write_output(directory, "converted_ph", metadata)

                destination = self.root / str(len(list(self.root.iterdir())))
                with self.assertRaisesRegex(ConversionError, "fieldmap metadata") as raised:
                    DicomConverter(write_invalid_metadata, self.deface_config).convert(
                        [self.fieldmap_plan], destination, "r01network"
                    )
                self.assertIsInstance(raised.exception.__cause__, ValueError)
                self.assertFalse(destination.exists())

    def test_wraps_output_parent_that_is_a_file(self) -> None:
        parent = self.root / "file"
        parent.write_text("keep me")
        self.archive_writer = Mock()
        with self.assertRaises(ConversionError) as raised:
            self.converter.convert([self.functional_plan], parent / "bids", "r01network")
        self.assertIsInstance(raised.exception.__cause__, FileExistsError)
        self.assertEqual(parent.read_text(), "keep me")
        self.archive_writer.assert_not_called()

    def test_does_not_hide_programming_errors_in_download(self) -> None:
        defect = TypeError("incorrect download implementation")
        self.archive_writer = Mock(side_effect=defect)
        with self.assertRaises(TypeError) as raised:
            self.converter.convert([self.functional_plan], self.destination, "r01network")
        self.assertIs(raised.exception, defect)
        self.assertFalse(self.destination.exists())

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
            runner=Mock(side_effect=CalledProcessError(1, ["dcm2niix"])),
            deface_config=self.deface_config,
        )

        with self.assertRaises(ConversionError):
            self.converter.convert([self.functional_plan], self.destination, "r01network")

        self.assertFalse(self.destination.exists())

    def test_rejects_unpinned_config_before_destination_or_download(self) -> None:
        self.archive_writer = Mock()
        destination = self.root / "new-parent/bids"
        unpinned = DefaceConfig(Path("relative-pydeface.sif"), "2.1.0", "0" * 64)

        with self.assertRaisesRegex(Exception, "absolute"):
            DicomConverter(self.runner, unpinned).convert(
                [self.functional_plan], destination, "r01network"
            )

        self.archive_writer.assert_not_called()
        self.runner.assert_not_called()
        self.assertFalse(destination.parent.exists())

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

        self.converter = DicomConverter(runner=write_image_without_sidecar, deface_config=self.deface_config)

        with self.assertRaisesRegex(ConversionError, "JSON sidecar"):
            self.converter.convert([self.functional_plan], self.destination, "r01network")

    def test_wraps_malformed_json_sidecar_with_its_cause(self) -> None:
        def write_malformed_sidecar(command: list[str], **kwargs) -> None:
            directory = self.output_directory(command)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "converted.nii.gz").write_bytes(b"nifti")
            (directory / "converted.json").write_text("{")

        self.converter = DicomConverter(runner=write_malformed_sidecar, deface_config=self.deface_config)

        with self.assertRaises(ConversionError) as raised:
            self.converter.convert([self.functional_plan], self.destination, "r01network")

        self.assertIsInstance(raised.exception.__cause__, json.JSONDecodeError)
        self.assertFalse(self.destination.exists())

    def test_rejects_multiple_non_fieldmap_images(self) -> None:
        def write_two_images(command: list[str], **kwargs) -> None:
            directory = self.output_directory(command)
            self.write_output(directory, "first")
            self.write_output(directory, "second")

        self.converter = DicomConverter(runner=write_two_images, deface_config=self.deface_config)

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

                self.converter = DicomConverter(runner=write_one_image, deface_config=self.deface_config)

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

        self.converter = DicomConverter(runner=write_duplicate_magnitudes, deface_config=self.deface_config)

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
        self.converter = DicomConverter(runner=self.runner, deface_config=self.deface_config)
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
