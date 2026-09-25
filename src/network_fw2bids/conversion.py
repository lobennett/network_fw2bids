"""DICOM archive conversion and atomic BIDS dataset publication."""

import json
import re
from pathlib import Path, PureWindowsPath
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable, Sequence
import zipfile

from flywheel.rest import ApiException

from ._publication import publish_directory
from .defacing import DefaceConfig, DefacingReceipt, inventory_anatomy, load_receipt, receipt_path, write_receipt_atomic
from .errors import ConversionError, DefacingError
from .planning import ArchivePlan
from .fieldmaps import FieldmapPlan, import_fieldmap
from .sensitive_workspace import sensitive_workspace


class DicomConverter:
    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        deface_config: DefaceConfig | None = None,
    ) -> None:
        self._runner = runner
        self._deface_config = deface_config

    def convert(
        self,
        plans: Sequence[ArchivePlan],
        destination: Path,
        dataset_name: str,
        *,
        selection: dict | None = None,
    ) -> None:
        destination = Path(destination)
        for plan in plans:
            self._require_safe_relative_prefix(plan.relative_prefix)
        subject = self._subject_from_plans(plans)
        if self._deface_config is None:
            raise ConversionError("PyDeface configuration is required for conversion")
        self._deface_config.validate()
        try:
            self._require_new_destination(destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(
                prefix=".network-fw2bids-safe-", dir=destination.parent
            ) as safe_name:
                safe_stage = Path(safe_name) / "bids"
                with sensitive_workspace() as sensitive:
                    staged = sensitive / "bids"
                    staged.mkdir()
                    self._write_dataset_description(staged, dataset_name)
                    archives = [self._convert_archive(plan, staged, sensitive / f"archive-{index}")
                                for index, plan in enumerate(plans)]
                    provenance = staged / f"code/network_fw2bids/conversion/sub-{subject}.json"
                    provenance.parent.mkdir(parents=True, exist_ok=True)
                    record = {"schema_version": 1, "subject": subject, "archives": archives}
                    if selection is not None:
                        record["selection"] = {**selection, "snapshot_kind": "conversion_selection"}
                    provenance.write_text(json.dumps(record, indent=2) + "\n")
                    from .defacing import deface_dataset

                    receipt = deface_dataset(staged, subject, self._deface_config, self._runner)
                    write_receipt_atomic(staged, receipt)
                    self._verify_publishable_inventory(staged, plans, receipt)
                    shutil.copytree(staged, safe_stage, symlinks=False)
                    self._verify_safe_copy(safe_stage, plans, receipt)
                publish_directory(safe_stage, destination)
        except OSError as exc:
            raise ConversionError(f"could not prepare or write BIDS dataset at {destination}") from exc

    @staticmethod
    def _subject_from_plans(plans: Sequence[ArchivePlan]) -> str:
        subjects: set[str] = set()
        for plan in plans:
            parts = Path(plan.relative_prefix).parts
            if not parts or not parts[0].startswith("sub-"):
                raise ConversionError(f"could not determine subject from BIDS prefix: {plan.relative_prefix}")
            subject = parts[0].removeprefix("sub-")
            if not subject or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" for character in subject):
                raise ConversionError(f"could not determine subject from BIDS prefix: {plan.relative_prefix}")
            subjects.add(subject)
        if len(subjects) != 1:
            raise ConversionError("conversion plans must contain exactly one subject")
        return subjects.pop()

    @classmethod
    def _verify_publishable_inventory(
        cls,
        dataset_root: Path,
        plans: Sequence[ArchivePlan],
        receipt: DefacingReceipt,
    ) -> set[str]:
        if dataset_root.is_symlink() or not dataset_root.is_dir():
            raise ConversionError("publishable BIDS staging is missing or unsafe")
        expected_files = {
            "dataset_description.json",
            receipt_path(dataset_root, receipt.subject).relative_to(dataset_root).as_posix(),
        }
        provenance = dataset_root / f"code/network_fw2bids/conversion/sub-{receipt.subject}.json"
        if provenance.exists():
            expected_files.add(provenance.relative_to(dataset_root).as_posix())
        for plan in plans:
            prefixes = cls._published_prefixes(dataset_root, plan)
            for prefix in prefixes:
                candidates = (Path(f"{prefix}.nii"), Path(f"{prefix}.nii.gz"))
                images = [candidate for candidate in candidates if (dataset_root / candidate).exists()]
                if len(images) != 1:
                    raise ConversionError("publishable BIDS staging has an unexpected image inventory")
                image = dataset_root / images[0]
                sidecar = dataset_root / prefix.with_suffix(".json")
                if image.is_symlink() or sidecar.is_symlink() or not image.is_file() or not sidecar.is_file():
                    raise ConversionError("publishable BIDS staging contains an unsafe expected output")
                expected_files.update((images[0].as_posix(), prefix.with_suffix(".json").as_posix()))
            if plan.modality == "dwi":
                for extension in (".bval", ".bvec"):
                    companion = dataset_root / plan.relative_prefix.with_suffix(extension)
                    if companion.is_symlink() or not companion.is_file():
                        raise ConversionError("publishable BIDS staging contains an unsafe expected output")
                    expected_files.add(plan.relative_prefix.with_suffix(extension).as_posix())

        actual_files: set[str] = set()
        actual_directories: set[str] = set()
        try:
            for path in dataset_root.rglob("*"):
                if path.is_symlink():
                    raise ConversionError("publishable BIDS staging contains a symbolic link")
                relative = path.relative_to(dataset_root).as_posix()
                if path.is_file():
                    actual_files.add(relative)
                elif path.is_dir():
                    actual_directories.add(relative)
                else:
                    raise ConversionError("publishable BIDS staging contains an unsafe entry")
        except OSError as exc:
            raise ConversionError("could not inspect publishable BIDS staging") from exc

        expected_directories: set[str] = set()
        for filename in expected_files:
            parent = Path(filename).parent
            while parent != Path("."):
                expected_directories.add(parent.as_posix())
                parent = parent.parent
        if actual_files != expected_files or actual_directories != expected_directories:
            raise ConversionError("publishable BIDS staging has unexpected files or directories")
        return expected_files

    @classmethod
    def _verify_safe_copy(
        cls, dataset_root: Path, plans: Sequence[ArchivePlan], expected: DefacingReceipt
    ) -> None:
        try:
            cls._verify_publishable_inventory(dataset_root, plans, expected)
            copied = load_receipt(receipt_path(dataset_root, expected.subject))
            if copied != expected:
                raise ConversionError("safe publication receipt does not match defacing evidence")
            anatomy: set[str] = set()
            for relative in inventory_anatomy(dataset_root / f"sub-{copied.subject}"):
                path = dataset_root / relative
                if not path.is_file():
                    raise ConversionError("safe publication staging contains unsafe anatomy")
                anatomy.add(relative)
                record = next((item for item in copied.images if item.path == relative), None)
                if record is None or cls._sha256(path) != record.output_sha256:
                    raise ConversionError("safe publication checksum does not match defacing receipt")
                sidecar = path.with_name(path.name[:-7] + ".json") if path.name.endswith(".nii.gz") else path.with_suffix(".json")
                metadata = json.loads(sidecar.read_text())
                if not isinstance(metadata, dict) or metadata.get("Defaced") is not True:
                    raise ConversionError("safe publication anatomy is missing defacing metadata")
            if anatomy != {item.path for item in copied.images}:
                raise ConversionError("safe publication anatomy does not match defacing receipt")
        except (OSError, ValueError, DefacingError) as exc:
            raise ConversionError("could not verify safe publication staging") from exc

    @staticmethod
    def _sha256(path: Path) -> str:
        import hashlib

        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def _require_safe_relative_prefix(cls, relative_prefix: Path) -> None:
        prefix = Path(relative_prefix)
        if (
            not prefix.parts or prefix.is_absolute() or ".." in prefix.parts
            or PureWindowsPath(prefix).drive or "\\" in str(prefix) or "\0" in str(prefix)
        ):
            cls._raise_output_shape_error(f"unsafe BIDS output prefix: {relative_prefix!s}")

    @staticmethod
    def _require_new_destination(destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise ConversionError(f"BIDS destination already exists: {destination}")

    @staticmethod
    def _write_dataset_description(dataset_root: Path, dataset_name: str) -> None:
        description = {"Name": f"{dataset_name} export", "BIDSVersion": "1.10.1"}
        try:
            (dataset_root / "dataset_description.json").write_text(
                json.dumps(description, indent=2) + "\n"
            )
        except (OSError, TypeError, ValueError) as exc:
            raise ConversionError("could not write dataset description") from exc

    def _convert_archive(
        self, plan: ArchivePlan | FieldmapPlan, dataset_root: Path, workspace: Path
    ) -> dict:
        workspace.mkdir()
        if isinstance(plan, FieldmapPlan):
            images, record = import_fieldmap(plan, workspace)
            self._place_converted_images(images, plan, dataset_root)
            record['outputs'] = self._output_records(dataset_root, plan)
            return record
        archive_path = workspace / "dicom.zip"
        self._download(plan, archive_path)

        dicom_directory = workspace / "dicoms"
        dicom_directory.mkdir()
        self._extract(archive_path, dicom_directory)

        converted_directory = workspace / "converted"
        converted_directory.mkdir()
        version = self._run_dcm2niix(dicom_directory, converted_directory)
        try:
            images = sorted(
                path
                for path in converted_directory.iterdir()
                if path.name.endswith((".nii", ".nii.gz"))
            )
        except OSError as exc:
            raise ConversionError("could not inspect dcm2niix output") from exc
        self._place_converted_images(images, plan, dataset_root)
        def identifier(obj, key):
            value = getattr(obj, key, None)
            return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None
        return {"acquisition_id": identifier(plan.acquisition, "id"),
                "file_id": identifier(plan.dicom_file, "file_id"),
                "archive_sha256": self._sha256(archive_path),
                "dcm2niix_version": version, "outputs": self._output_records(dataset_root, plan)}

    def _output_records(self, dataset_root, plan):
        outputs = []
        for prefix in self._published_prefixes(dataset_root, plan):
            for extension in (".nii", ".nii.gz", ".json", ".bval", ".bvec"):
                relative = Path(str(prefix) + extension)
                path = dataset_root / relative
                if path.is_file():
                    outputs.append({"path": relative.as_posix(), "sha256": self._sha256(path)})
        return outputs

    @staticmethod
    def _download(plan: ArchivePlan, archive_path: Path) -> None:
        try:
            plan.acquisition.download_file(plan.dicom_file.name, str(archive_path))
        except (ApiException, OSError) as exc:
            raise ConversionError(
                f"could not download DICOM archive for {plan.acquisition.label!r}"
            ) from exc

    @staticmethod
    def _extract(archive_path: Path, destination: Path) -> None:
        root = destination.resolve()
        try:
            with zipfile.ZipFile(archive_path) as archive:
                for member in archive.infolist():
                    if stat.S_ISLNK(member.external_attr >> 16):
                        raise ValueError(
                            f"unsafe symbolic link in DICOM archive: {member.filename!r}"
                        )
                    target = (destination / member.filename).resolve()
                    if target != root and root not in target.parents:
                        raise ValueError(
                            f"unsafe path in DICOM archive: {member.filename!r}"
                        )
                archive.extractall(destination)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ConversionError(f"could not safely extract DICOM archive: {exc}") from exc

    def _run_dcm2niix(self, dicom_directory: Path, converted_directory: Path) -> str | None:
        command = [
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
        ]
        try:
            result = self._runner(command, check=True, capture_output=True, text=True)
            output = getattr(result, "stdout", "")
            version = re.search(r"(?:version|v)\s*(v?\d+\.\d+\.\d+)", output) if isinstance(output, str) else None
            return version.group(1) if version else None
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
            raise ConversionError("dcm2niix conversion failed") from exc

    @classmethod
    def _place_converted_images(
        cls, images: list[Path], plan: ArchivePlan, dataset_root: Path
    ) -> None:
        if plan.modality != "fmap":
            if len(images) != 1:
                if plan.modality == "func":
                    cls._place_multi_echo_images(images, plan, dataset_root)
                    return
                cls._raise_output_shape_error(
                    f"expected one converted image for {plan.acquisition.label!r}, "
                    f"found {len(images)}"
                )
            if plan.modality == "dwi":
                cls._require_dwi_gradients(images[0])
            cls._require_new_image_outputs(images[0], plan.relative_prefix, dataset_root)
            cls._place_image(images[0], plan, dataset_root)
            return

        roles: dict[str, Path] = {}
        for image in images:
            role = cls._classify_fieldmap(image)
            if role in roles:
                cls._raise_output_shape_error(
                    f"multiple {role} images produced for {plan.acquisition.label!r}"
                )
            roles[role] = image
        if set(roles) != {"fieldmap", "magnitude"}:
            cls._raise_output_shape_error(
                f"expected magnitude and phase outputs for {plan.acquisition.label!r}; "
                f"found {sorted(roles)}"
            )
        placements = [
            (roles["magnitude"], Path(f"{plan.relative_prefix}_magnitude"), None),
            (roles["fieldmap"], Path(f"{plan.relative_prefix}_fieldmap"), {"Units": "Hz"}),
        ]
        for image, relative_prefix, _ in placements:
            cls._require_new_image_outputs(image, relative_prefix, dataset_root)
        for image, relative_prefix, metadata_updates in placements:
            cls._place_image(
                image,
                plan,
                dataset_root,
                relative_prefix=relative_prefix,
                metadata_updates=metadata_updates,
            )

    @classmethod
    def _place_multi_echo_images(
        cls, images: list[Path], plan: ArchivePlan, dataset_root: Path
    ) -> None:
        if len(images) < 2:
            cls._raise_output_shape_error("multi-echo conversion requires at least two images")
        placements: list[tuple[Path, Path]] = []
        echoes: set[int] = set()
        for image in images:
            echo = cls._read_metadata(image).get("EchoNumber")
            if isinstance(echo, bool) or not isinstance(echo, int) or echo < 1:
                cls._raise_output_shape_error(
                    f"multi-echo output {image.name!r} has an invalid EchoNumber"
                )
            if echo in echoes:
                cls._raise_output_shape_error(
                    f"multi-echo outputs have duplicate EchoNumber {echo}"
                )
            echoes.add(echo)
            placements.append((image, cls._echo_prefix(plan.relative_prefix, echo)))
        for image, prefix in placements:
            cls._require_new_image_outputs(image, prefix, dataset_root)
        for image, prefix in placements:
            cls._place_image(image, plan, dataset_root, relative_prefix=prefix)

    @classmethod
    def _echo_prefix(cls, prefix: Path, echo: int) -> Path:
        name = prefix.name
        if not name.endswith("_bold"):
            cls._raise_output_shape_error(
                f"multi-echo functional prefix does not end in '_bold': {prefix}"
            )
        return prefix.with_name(f"{name[:-5]}_echo-{echo}_bold")

    @classmethod
    def _published_prefixes(cls, dataset_root: Path, plan: ArchivePlan) -> tuple[Path, ...]:
        if plan.modality == "fmap":
            return (
                Path(f"{plan.relative_prefix}_magnitude"),
                Path(f"{plan.relative_prefix}_fieldmap"),
            )
        if plan.modality != "func":
            return (plan.relative_prefix,)
        ordinary_images = tuple(
            candidate
            for candidate in (
                Path(f"{plan.relative_prefix}.nii"),
                Path(f"{plan.relative_prefix}.nii.gz"),
            )
            if (dataset_root / candidate).exists()
        )
        parent = dataset_root / plan.relative_prefix.parent
        echo_pattern = f"{plan.relative_prefix.name[:-5]}_echo-*_bold.nii*"
        echo_images = tuple(parent.glob(echo_pattern)) if parent.is_dir() else ()
        if ordinary_images and echo_images:
            raise ConversionError("publishable BIDS staging mixes single- and multi-echo outputs")
        if ordinary_images:
            return (plan.relative_prefix,)
        prefixes = {
            image.relative_to(dataset_root).with_suffix("")
            for image in echo_images
        }
        prefixes = {
            prefix.with_suffix("") if prefix.suffix == ".nii" else prefix
            for prefix in prefixes
        }
        if len(prefixes) < 2:
            raise ConversionError("publishable BIDS staging has an unexpected functional inventory")
        return tuple(sorted(prefixes))

    @classmethod
    def _classify_fieldmap(cls, image: Path) -> str:
        source_stem = cls._source_stem(image)
        metadata = cls._read_metadata(image)
        image_type = metadata.get("ImageType", [])
        component = metadata.get("ComplexImageComponent", "")
        if (
            not isinstance(image_type, list)
            or any(not isinstance(value, str) for value in image_type)
            or not isinstance(component, str)
        ):
            cls._raise_output_shape_error(f"invalid fieldmap metadata in {image.name!r}")
        image_type = {value.upper() for value in image_type}
        component = component.upper()
        if source_stem == 'fieldmap' and metadata.get('Units') == 'Hz':
            return 'fieldmap'
        if source_stem.lower().endswith("_ph") or "P" in image_type or component == "PHASE":
            return "fieldmap"
        return "magnitude"

    @classmethod
    def _place_image(
        cls,
        image: Path,
        plan: ArchivePlan,
        dataset_root: Path,
        relative_prefix: Path | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        destination_relative_prefix = relative_prefix or plan.relative_prefix
        cls._require_new_image_outputs(image, destination_relative_prefix, dataset_root)
        destination_prefix = dataset_root / destination_relative_prefix
        destination_prefix.parent.mkdir(parents=True, exist_ok=True)
        extension = ".nii.gz" if image.name.endswith(".nii.gz") else ".nii"
        try:
            shutil.copy2(image, Path(f"{destination_prefix}{extension}"))
            metadata = cls._read_metadata(image)
            if plan.task:
                metadata["TaskName"] = plan.task
            metadata.update(metadata_updates or {})
            destination_prefix.with_suffix(".json").write_text(
                json.dumps(metadata, indent=2, sort_keys=True) + "\n"
            )
            source_prefix = image.with_name(cls._source_stem(image))
            for extension in (".bval", ".bvec"):
                source = source_prefix.with_suffix(extension)
                if source.exists():
                    shutil.copy2(source, destination_prefix.with_suffix(extension))
        except (OSError, TypeError, ValueError) as exc:
            raise ConversionError(f"could not place converted image {image.name!r}") from exc

    @classmethod
    def _require_new_image_outputs(
        cls, image: Path, relative_prefix: Path, dataset_root: Path
    ) -> None:
        cls._require_safe_relative_prefix(relative_prefix)
        destination_prefix = dataset_root / relative_prefix
        extension = ".nii.gz" if image.name.endswith(".nii.gz") else ".nii"
        source_prefix = image.with_name(cls._source_stem(image))
        destinations = [
            Path(f"{destination_prefix}{extension}"),
            destination_prefix.with_suffix(".json"),
        ]
        for companion_extension in (".bval", ".bvec"):
            if source_prefix.with_suffix(companion_extension).exists():
                destinations.append(destination_prefix.with_suffix(companion_extension))
        for destination in destinations:
            if not destination.resolve().is_relative_to(dataset_root.resolve()):
                cls._raise_output_shape_error(
                    f"BIDS output resolves outside the staging directory: {destination}"
                )
            if destination.exists() or destination.is_symlink():
                raise ConversionError(f"BIDS output already exists: {destination}")

    @classmethod
    def _require_dwi_gradients(cls, image: Path) -> None:
        prefix = image.with_name(cls._source_stem(image))
        for extension in (".bval", ".bvec"):
            companion = prefix.with_suffix(extension)
            try:
                if not companion.is_file() or not companion.read_bytes().strip():
                    cls._raise_output_shape_error(
                        f"dcm2niix did not produce a usable DWI gradient companion: {companion.name}"
                    )
            except OSError as exc:
                raise ConversionError(f"could not read DWI gradient companion: {companion.name}") from exc

    @classmethod
    def _read_metadata(cls, image: Path) -> dict[str, Any]:
        sidecar = image.with_name(cls._source_stem(image)).with_suffix(".json")
        if not sidecar.exists():
            cls._raise_output_shape_error(
                f"dcm2niix did not produce a JSON sidecar for {image.name}"
            )
        try:
            metadata = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ConversionError(
                f"could not read JSON sidecar for {image.name!r}"
            ) from exc
        if not isinstance(metadata, dict):
            cls._raise_output_shape_error(
                f"dcm2niix JSON sidecar for {image.name!r} is not an object"
            )
        return metadata

    @staticmethod
    def _source_stem(image: Path) -> str:
        return image.name.removesuffix(".nii.gz").removesuffix(".nii")

    @staticmethod
    def _raise_output_shape_error(message: str) -> None:
        try:
            raise ValueError(message)
        except ValueError as exc:
            raise ConversionError(message) from exc
