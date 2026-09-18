"""DICOM archive conversion and atomic BIDS dataset publication."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable, Sequence
import zipfile

from .errors import ConversionError
from .planning import ArchivePlan


class DicomConverter:
    def __init__(self, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> None:
        self._runner = runner

    def convert(
        self,
        plans: Sequence[ArchivePlan],
        destination: Path,
        dataset_name: str,
    ) -> None:
        destination = Path(destination)
        self._require_new_destination(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".network-fw2bids-", dir=destination.parent) as scratch_name:
            scratch = Path(scratch_name)
            staged = scratch / "bids"
            staged.mkdir()
            self._write_dataset_description(staged, dataset_name)
            for index, plan in enumerate(plans):
                self._convert_archive(plan, staged, scratch / f"archive-{index}")
            os.replace(staged, destination)

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
        self, plan: ArchivePlan, dataset_root: Path, workspace: Path
    ) -> None:
        workspace.mkdir()
        archive_path = workspace / "dicom.zip"
        self._download(plan, archive_path)

        dicom_directory = workspace / "dicoms"
        dicom_directory.mkdir()
        self._extract(archive_path, dicom_directory)

        converted_directory = workspace / "converted"
        converted_directory.mkdir()
        self._run_dcm2niix(dicom_directory, converted_directory)
        try:
            images = sorted(
                path
                for path in converted_directory.iterdir()
                if path.name.endswith((".nii", ".nii.gz"))
            )
        except OSError as exc:
            raise ConversionError("could not inspect dcm2niix output") from exc
        self._place_converted_images(images, plan, dataset_root)

    @staticmethod
    def _download(plan: ArchivePlan, archive_path: Path) -> None:
        try:
            plan.acquisition.download_file(plan.dicom_file.name, str(archive_path))
        except Exception as exc:
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
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            raise ConversionError(f"could not safely extract DICOM archive: {exc}") from exc

    def _run_dcm2niix(self, dicom_directory: Path, converted_directory: Path) -> None:
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
            self._runner(command, check=True, capture_output=True, text=True)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
            raise ConversionError("dcm2niix conversion failed") from exc

    @classmethod
    def _place_converted_images(
        cls, images: list[Path], plan: ArchivePlan, dataset_root: Path
    ) -> None:
        if plan.modality != "fmap":
            if len(images) != 1:
                cls._raise_output_shape_error(
                    f"expected one converted image for {plan.acquisition.label!r}, "
                    f"found {len(images)}"
                )
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
    def _classify_fieldmap(cls, image: Path) -> str:
        source_stem = cls._source_stem(image)
        metadata = cls._read_metadata(image)
        image_type = {str(value).upper() for value in metadata.get("ImageType", [])}
        component = str(metadata.get("ComplexImageComponent", "")).upper()
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
            if destination.exists() or destination.is_symlink():
                raise ConversionError(f"BIDS output already exists: {destination}")

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
