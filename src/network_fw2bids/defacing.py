"""PyDeface execution, output validation, and defacing evidence."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from tempfile import NamedTemporaryFile
from typing import Any, Callable

import nibabel as nib
import numpy as np

from .errors import DefacingError


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SUBJECT = re.compile(r"[A-Za-z0-9]+\Z")
_ANATOMY_SUFFIXES = ("_T1w.nii", "_T1w.nii.gz", "_T2w.nii", "_T2w.nii.gz")
Runner = Callable[..., subprocess.CompletedProcess[Any]]


@dataclass(frozen=True)
class DefaceConfig:
    image: Path
    version: str
    sha256: str

    def validate(self) -> None:
        image = Path(self.image)
        if not image.is_absolute():
            raise DefacingError("PyDeface image path must be absolute")
        if image.is_symlink():
            raise DefacingError("PyDeface image path must not be a symbolic link")
        self.verify_image_checksum()

    def verify_image_checksum(self) -> None:
        if not self.version:
            raise DefacingError("PyDeface version is required")
        if not _SHA256.fullmatch(self.sha256):
            raise DefacingError("PyDeface image sha256 is invalid")
        image = Path(self.image)
        if image.is_symlink() or not image.is_file():
            raise DefacingError(f"PyDeface image is missing or unsafe: {image}")
        if _sha256(image) != self.sha256:
            raise DefacingError("PyDeface image checksum does not match configuration")


@dataclass(frozen=True)
class DefacedImage:
    path: str
    input_sha256: str
    output_sha256: str
    shape: tuple[int, ...]
    zooms: tuple[float, ...]
    affine_sha256: str


@dataclass(frozen=True)
class DefacingReceipt:
    schema_version: int
    subject: str
    status: str
    software: dict[str, str]
    images: tuple[DefacedImage, ...]

    @classmethod
    def current(
        cls, subject: str, config: DefaceConfig, images: Sequence[DefacedImage]
    ) -> DefacingReceipt:
        _require_subject(subject)
        return cls(
            schema_version=1,
            subject=subject,
            status="success",
            software={
                "name": "PyDeface",
                "version": config.version,
                "container": _container_identity(config.image),
                "sha256": config.sha256,
            },
            images=tuple(images),
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def receipt_path(dataset_root: Path, subject: str) -> Path:
    """Return the fixed BIDS-relative destination for a subject receipt."""
    _require_subject(subject)
    return Path(dataset_root) / "code" / "network_fw2bids" / "defacing" / f"sub-{subject}.json"


def write_receipt_atomic(dataset_root: Path, receipt: DefacingReceipt) -> Path:
    """Publish receipt JSON only after serializing its validated content."""
    _validate_receipt(receipt)
    path = receipt_path(dataset_root, receipt.subject)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = json.dumps(receipt.to_dict(), indent=2, sort_keys=True) + "\n"
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except UnboundLocalError:
            pass
        except OSError:
            pass
        raise DefacingError(f"could not write defacing receipt: {path}") from exc
    return path


def load_receipt(path: Path) -> DefacingReceipt:
    try:
        if Path(path).is_symlink() or not Path(path).is_file():
            raise DefacingError(f"defacing receipt is missing or unsafe: {path}")
        value = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DefacingError(f"could not read defacing receipt: {path}") from exc
    return parse_receipt(value)


def inventory_anatomy(subject_directory: Path) -> tuple[str, ...]:
    """Return the exact BIDS-relative T1w/T2w inventory for one subject tree."""
    subject_directory = Path(subject_directory)
    if not subject_directory.name.startswith("sub-"):
        raise DefacingError(f"defacing subject directory is invalid: {subject_directory}")
    subject = subject_directory.name.removeprefix("sub-")
    root = subject_directory.parent
    return tuple(
        image.relative_to(root).as_posix()
        for image in discover_anatomy(root, subject)
    )


def verify_published_image(dataset_root: Path, image: DefacedImage) -> None:
    """Verify a receipt entry against its published image and sidecar."""
    root = _validated_dataset_root(dataset_root)
    _require_safe_relative_path(image.path)
    published = root / PurePosixPath(image.path)
    try:
        if (
            published.is_symlink()
            or not published.is_file()
            or not published.resolve(strict=True).is_relative_to(root)
        ):
            raise DefacingError(f"defacing image is missing or unsafe: {published}")
        if _sha256(published) != image.output_sha256:
            raise DefacingError(f"defacing checksum does not match receipt: {published}")
        if _read_valid_sidecar(published).get("Defaced") is not True:
            raise DefacingError(f"defacing image sidecar is not marked Defaced: {published}")
    except OSError as exc:
        raise DefacingError(f"could not verify defacing image: {published}") from exc


def verify_subject_defacing(part: Path, subject: str) -> DefacingReceipt:
    """Verify that one subject part has complete, untampered defacing evidence."""
    root = _validated_dataset_root(part)
    _require_subject(subject)
    _require_safe_receipt_tree(root, subject)
    receipt = load_receipt(receipt_path(root, subject))
    if receipt.subject != subject:
        raise DefacingError("defacing receipt subject does not match subject part")
    anatomy = set(inventory_anatomy(root / f"sub-{subject}"))
    evidence = {image.path for image in receipt.images}
    if anatomy != evidence:
        raise DefacingError("defacing receipt does not match anatomical inventory")
    for image in receipt.images:
        verify_published_image(root, image)
    return receipt


def parse_receipt(value: object) -> DefacingReceipt:
    """Parse receipt JSON with strict types and safe relative image paths."""
    if not isinstance(value, dict):
        raise DefacingError("defacing receipt must be a JSON object")
    try:
        schema_version = value["schema_version"]
        subject = value["subject"]
        status = value["status"]
        software = value["software"]
        images = value["images"]
    except KeyError as exc:
        raise DefacingError("defacing receipt is missing required fields") from exc
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
        or not isinstance(subject, str)
        or status != "success"
        or not isinstance(software, dict)
        or not isinstance(images, list)
    ):
        raise DefacingError("defacing receipt has invalid fields")
    _require_subject(subject)
    _validate_software(software)
    try:
        parsed_images = tuple(_parse_defaced_image(image) for image in images)
    except (KeyError, TypeError, ValueError) as exc:
        raise DefacingError("defacing receipt has invalid image evidence") from exc
    receipt = DefacingReceipt(schema_version, subject, status, dict(software), parsed_images)
    _validate_receipt(receipt)
    return receipt


def deface_dataset(
    dataset_root: Path,
    subject: str,
    config: DefaceConfig,
    runner: Runner = subprocess.run,
) -> DefacingReceipt:
    """Deface every T1w/T2w in one staged BIDS subject tree."""
    root = _validated_dataset_root(dataset_root)
    _require_subject(subject)
    config.validate()
    if any(character in str(root) for character in (":", ",", "\0", "\n", "\r")):
        raise DefacingError("unsafe container bind path for anatomical staging")
    sources = discover_anatomy(root, subject)
    records: list[DefacedImage] = []
    if sources:
        with _private_runtime(root) as environment:
            for source in sources:
                relative = source.relative_to(root).as_posix()
                output = _temporary_output_for(source)
                try:
                    _read_valid_sidecar(source)
                    if output.exists() or output.is_symlink():
                        raise DefacingError("PyDeface output already exists")
                    runner(
                        _pydeface_command(config, root, source, output),
                        check=True,
                        capture_output=True,
                        env=environment,
                        cwd=root / ".network-fw2bids-tmp",
                    )
                    record = _validate_defaced_output(root, source, output)
                    _publish_defaced_image_and_sidecar(source, output, config)
                    records.append(record)
                except DefacingError as exc:
                    # Only our controlled validation message is exposed, without
                    # the exception chain that may include input paths or data.
                    raise DefacingError(f"{exc} [{relative}]") from None
                except (subprocess.CalledProcessError, OSError):
                    raise DefacingError(f"PyDeface failed for {relative}") from None
                finally:
                    try:
                        output.unlink(missing_ok=True)
                    except OSError:
                        raise DefacingError(f"could not remove defacing output for {relative}") from None
    return DefacingReceipt.current(subject, config, records)


@contextmanager
def _private_runtime(root: Path) -> Iterator[dict[str, str]]:
    """Confine tool scratch and runtime configuration to the sensitive tree."""
    home = root / ".network-fw2bids-home"
    temporary = root / ".network-fw2bids-tmp"
    created: list[Path] = []
    try:
        for directory in (home, temporary):
            # Never follow or reuse a pre-existing scratch directory.
            directory.mkdir(mode=0o700)
            created.append(directory)
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("APPTAINER", "SINGULARITY"))
            and key != "FLYWHEEL_API_TOKEN"
        }
        environment.update(HOME=str(home), TMPDIR=str(temporary), TMP=str(temporary), TEMP=str(temporary))
        yield environment
    except OSError:
        raise DefacingError("could not prepare private PyDeface runtime for publishable staging") from None
    finally:
        for directory in reversed(created):
            try:
                shutil.rmtree(directory)
                if directory.exists() or directory.is_symlink():
                    raise OSError("cleanup incomplete")
            except OSError:
                raise DefacingError("could not remove private PyDeface runtime") from None


def discover_anatomy(dataset_root: Path, subject: str) -> tuple[Path, ...]:
    subject_directory = dataset_root / f"sub-{subject}"
    if subject_directory.is_symlink() or not subject_directory.is_dir():
        raise DefacingError(f"subject directory is missing or unsafe: {subject_directory}")
    _require_no_symlinked_directories(subject_directory)
    try:
        candidates = tuple(
            path
            for path in subject_directory.rglob("*")
            if path.name.endswith(_ANATOMY_SUFFIXES)
        )
    except OSError as exc:
        raise DefacingError(f"could not discover anatomical images for {subject}") from exc
    for path in candidates:
        parts = path.relative_to(subject_directory).parts
        valid_location = (
            len(parts) == 2 and parts[0] == "anat"
            or len(parts) == 3 and re.fullmatch(r"ses-[A-Za-z0-9]+", parts[0]) and parts[1] == "anat"
        )
        if not valid_location:
            raise DefacingError(f"misplaced anatomical image: {path.relative_to(dataset_root).as_posix()}")
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(dataset_root):
            raise DefacingError(f"anatomical image is missing or unsafe: {path}")
    return tuple(sorted(candidates, key=lambda path: path.relative_to(dataset_root).as_posix()))


def _pydeface_command(config: DefaceConfig, dataset_root: Path, source: Path, output: Path) -> list[str]:
    source_path = "/work/" + source.relative_to(dataset_root).as_posix()
    output_path = "/work/" + output.relative_to(dataset_root).as_posix()
    return [
        "apptainer",
        "exec",
        "--cleanenv",
        "--containall",
        "--no-mount",
        "home,cwd,hostfs,bind-paths,tmp",
        "--pwd",
        "/work/.network-fw2bids-tmp",
        "--bind",
        f"{dataset_root}:/work:rw",
        "--env",
        "HOME=/work/.network-fw2bids-home",
        "--env",
        "TMPDIR=/work/.network-fw2bids-tmp",
        str(config.image),
        "pydeface",
        source_path,
        "--outfile",
        output_path,
        "--force",
    ]


def _temporary_output_for(source: Path) -> Path:
    extension = ".nii.gz" if source.name.endswith(".nii.gz") else ".nii"
    stem = source.name[: -len(extension)]
    return source.with_name(f".{stem}.defaced{extension}")


def _validate_defaced_output(dataset_root: Path, source: Path, output: Path) -> DefacedImage:
    if output.is_symlink() or not output.is_file() or not output.resolve().is_relative_to(dataset_root):
        raise DefacingError(f"PyDeface did not create a safe output for {source.name!r}")
    try:
        input_image = nib.load(source)
        output_image = nib.load(output)
        input_data = np.asanyarray(input_image.dataobj)
        output_data = np.asanyarray(output_image.dataobj)
    except (OSError, ValueError, nib.filebasedimages.ImageFileError) as exc:
        raise DefacingError(f"PyDeface output is not a readable NIfTI: {source.name!r}") from exc
    if (
        input_image.shape != output_image.shape
        or input_image.header.get_zooms()[: len(input_image.shape)]
        != output_image.header.get_zooms()[: len(output_image.shape)]
        or not np.array_equal(input_image.affine, output_image.affine)
    ):
        raise DefacingError(f"PyDeface output changed image geometry: {source.name!r}")
    if output_data.size == 0 or not np.isfinite(output_data).all():
        raise DefacingError(f"PyDeface output contains invalid voxel data: {source.name!r}")
    input_bytes = _sha256(source)
    output_bytes = _sha256(output)
    if input_bytes == output_bytes or np.array_equal(input_data, output_data):
        raise DefacingError(f"PyDeface output is unchanged: {source.name!r}")
    relative_path = source.relative_to(dataset_root).as_posix()
    return DefacedImage(
        path=relative_path,
        input_sha256=input_bytes,
        output_sha256=output_bytes,
        shape=tuple(int(size) for size in output_image.shape),
        zooms=tuple(float(zoom) for zoom in output_image.header.get_zooms()[: len(output_image.shape)]),
        affine_sha256=_affine_sha256(output_image.affine),
    )


def _publish_defaced_image_and_sidecar(source: Path, output: Path, config: DefaceConfig) -> None:
    metadata = _read_valid_sidecar(source)
    metadata["Defaced"] = True
    metadata["DefacingSoftware"] = {"Name": "PyDeface", "Version": config.version}
    sidecar = _sidecar_path(source)
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=sidecar.parent, prefix=f".{sidecar.name}.", delete=False
        ) as temporary:
            json.dump(metadata, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(output, source)
        os.replace(temporary_path, sidecar)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except UnboundLocalError:
            pass
        except OSError:
            pass
        raise DefacingError(f"could not publish defaced image: {source.name!r}") from exc


def _read_valid_sidecar(source: Path) -> dict[str, object]:
    sidecar = _sidecar_path(source)
    try:
        if sidecar.is_symlink() or not sidecar.is_file():
            raise DefacingError(f"anatomical sidecar is missing or unsafe: {sidecar.name}")
        metadata = json.loads(sidecar.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DefacingError(f"could not read anatomical sidecar: {sidecar.name}") from exc
    if not isinstance(metadata, dict):
        raise DefacingError(f"anatomical sidecar must be a JSON object: {sidecar.name}")
    return metadata


def _sidecar_path(image: Path) -> Path:
    if image.name.endswith(".nii.gz"):
        return image.with_name(image.name[:-7] + ".json")
    return image.with_suffix(".json")


def _parse_defaced_image(value: object) -> DefacedImage:
    if not isinstance(value, dict):
        raise ValueError("image is not an object")
    path = value["path"]
    input_sha256 = value["input_sha256"]
    output_sha256 = value["output_sha256"]
    shape = value["shape"]
    zooms = value["zooms"]
    affine_sha256 = value["affine_sha256"]
    if (
        not isinstance(path, str)
        or not isinstance(input_sha256, str)
        or not isinstance(output_sha256, str)
        or not isinstance(affine_sha256, str)
        or not isinstance(shape, list)
        or not isinstance(zooms, list)
        or any(not isinstance(size, int) or isinstance(size, bool) or size <= 0 for size in shape)
        or any(not isinstance(zoom, (int, float)) or isinstance(zoom, bool) or not np.isfinite(zoom) or zoom <= 0 for zoom in zooms)
    ):
        raise ValueError("invalid image fields")
    return DefacedImage(
        path=path,
        input_sha256=input_sha256,
        output_sha256=output_sha256,
        shape=tuple(shape),
        zooms=tuple(float(zoom) for zoom in zooms),
        affine_sha256=affine_sha256,
    )


def _validate_receipt(receipt: DefacingReceipt) -> None:
    if receipt.schema_version != 1 or receipt.status != "success":
        raise DefacingError("defacing receipt has an unsupported schema or status")
    _require_subject(receipt.subject)
    _validate_software(receipt.software)
    seen: set[str] = set()
    for image in receipt.images:
        _require_safe_relative_path(image.path)
        if image.path in seen:
            raise DefacingError("defacing receipt contains duplicate anatomical paths")
        seen.add(image.path)
        if not all(_SHA256.fullmatch(value) for value in (image.input_sha256, image.output_sha256, image.affine_sha256)):
            raise DefacingError("defacing receipt has invalid checksums")
        if not image.shape or len(image.shape) != len(image.zooms) or any(size <= 0 for size in image.shape):
            raise DefacingError("defacing receipt has invalid image geometry")
        if any(not np.isfinite(zoom) or zoom <= 0 for zoom in image.zooms):
            raise DefacingError("defacing receipt has invalid image geometry")


def _validated_dataset_root(dataset_root: Path) -> Path:
    root = Path(dataset_root)
    try:
        if root.is_symlink() or not root.is_dir():
            raise DefacingError(f"BIDS dataset root is missing or unsafe: {root}")
        return root.resolve(strict=True)
    except OSError as exc:
        raise DefacingError(f"BIDS dataset root is missing or unsafe: {root}") from exc


def _require_subject(subject: str) -> None:
    if not _SUBJECT.fullmatch(subject):
        raise DefacingError(f"subject label is unsafe: {subject!r}")


def _require_safe_relative_path(value: str) -> None:
    path = PurePosixPath(value)
    if not value or "\0" in value or path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise DefacingError("defacing receipt has an unsafe anatomical path")


def _require_no_symlinked_directories(subject_directory: Path) -> None:
    try:
        for directory, names, _files in os.walk(subject_directory, followlinks=False):
            for name in names:
                candidate = Path(directory) / name
                if candidate.is_symlink():
                    raise DefacingError(f"subject directory contains an unsafe symbolic link: {candidate}")
    except OSError as exc:
        raise DefacingError(f"could not inspect subject directory: {subject_directory}") from exc


def _require_safe_receipt_tree(dataset_root: Path, subject: str) -> None:
    receipt = receipt_path(dataset_root, subject)
    expected_directories = (
        dataset_root / "code",
        dataset_root / "code" / "network_fw2bids",
        dataset_root / "code" / "network_fw2bids" / "defacing",
    )
    try:
        for directory in expected_directories:
            if directory.is_symlink() or not directory.is_dir():
                raise DefacingError(f"defacing receipt directory is missing or unsafe: {directory}")
        for path in (dataset_root / "code").rglob("*"):
            if path.is_symlink():
                raise DefacingError(f"defacing receipt tree contains a symbolic link: {path}")
            if not path.is_file() and not path.is_dir():
                raise DefacingError(f"defacing receipt tree contains an unsafe entry: {path}")
    except OSError as exc:
        raise DefacingError(f"could not inspect defacing receipt tree: {receipt}") from exc


def _container_identity(image: Path) -> str:
    identity = Path(image).name
    if not identity:
        raise DefacingError("PyDeface image has no safe container identity")
    return identity


def _validate_software(software: object) -> None:
    required = {"name", "version", "container", "sha256"}
    if not isinstance(software, dict) or set(software) != required:
        raise DefacingError("defacing receipt has invalid software evidence")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in software.items()):
        raise DefacingError("defacing receipt has invalid software evidence")
    if software["name"] != "PyDeface" or not software["version"]:
        raise DefacingError("defacing receipt has invalid software evidence")
    container = software["container"]
    if (
        not container
        or container in {".", ".."}
        or "/" in container
        or "\\" in container
        or any(character in container for character in ("\0", "\n", "\r"))
        or not _SHA256.fullmatch(software["sha256"])
    ):
        raise DefacingError("defacing receipt has invalid software evidence")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise DefacingError(f"could not checksum file: {path}") from exc
    return digest.hexdigest()


def _affine_sha256(affine: np.ndarray) -> str:
    return hashlib.sha256(np.asarray(affine, dtype="<f8").tobytes()).hexdigest()
