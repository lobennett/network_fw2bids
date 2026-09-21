import hashlib
import json
from pathlib import Path
from contextlib import contextmanager
from subprocess import CalledProcessError
import zipfile

import nibabel as nib
import numpy as np
import pytest

import network_fw2bids.conversion as conversion_module
from network_fw2bids.conversion import DicomConverter
from network_fw2bids.defacing import DefaceConfig
from network_fw2bids.errors import ConversionError
from network_fw2bids.planning import ArchivePlan


class Acquisition:
    label = "Sag_MPRAGE_T1"

    def download_file(self, name: str, destination: str) -> None:
        assert name == "scan.dicom.zip"
        with zipfile.ZipFile(destination, "w") as archive:
            archive.writestr("scan.dcm", b"dicom")


class DicomFile:
    name = "scan.dicom.zip"


class ConversionRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], **_kwargs: object) -> None:
        self.commands.append(command)
        if command[0] == "dcm2niix":
            output = Path(command[command.index("-o") + 1])
            output.mkdir(parents=True, exist_ok=True)
            image = nib.Nifti1Image(np.ones((3, 4, 5), dtype=np.float32), np.eye(4))
            nib.save(image, output / "converted.nii.gz")
            (output / "converted.json").write_text("{}")
            return
        assert command[:2] == ["apptainer", "exec"]
        root = Path(command[command.index("--bind") + 1].split(":", 1)[0])
        source = root / command[command.index("pydeface") + 1].removeprefix("/work/")
        output = root / command[command.index("--outfile") + 1].removeprefix("/work/")
        image = nib.load(source)
        data = np.asanyarray(image.dataobj).copy()
        data[0, 0, 0] = 0
        nib.save(nib.Nifti1Image(data, image.affine, image.header), output)


def make_config(tmp_path: Path) -> DefaceConfig:
    image = tmp_path / "pydeface.sif"
    image.write_bytes(b"pinned pydeface image")
    return DefaceConfig(image, "2.1.0", hashlib.sha256(image.read_bytes()).hexdigest())


def plan(modality: str = "anat") -> ArchivePlan:
    suffix = "T1w" if modality == "anat" else "task-rest_bold"
    directory = "anat" if modality == "anat" else "func"
    return ArchivePlan(
        acquisition=Acquisition(),
        dicom_file=DicomFile(),
        relative_prefix=Path(f"sub-s03/ses-01/{directory}/sub-s03_ses-01_{suffix}"),
        modality=modality,
    )


def convert(tmp_path: Path, monkeypatch, runner: ConversionRunner, plans: list[ArchivePlan] | None = None) -> Path:
    node_tmp = tmp_path / "node-tmp"
    persistent = tmp_path / "persistent"
    node_tmp.mkdir()
    persistent.mkdir()
    monkeypatch.setenv("SLURM_TMPDIR", str(node_tmp))
    destination = persistent / "s03"
    DicomConverter(runner=runner, deface_config=make_config(tmp_path)).convert(
        plans or [plan()], destination, "russpold/r01network"
    )
    return destination


def test_conversion_publishes_only_after_sensitive_workspace_is_gone(tmp_path, monkeypatch):
    node_tmp = tmp_path / "node-tmp"
    persistent = tmp_path / "persistent"
    node_tmp.mkdir()
    persistent.mkdir()
    monkeypatch.setenv("SLURM_TMPDIR", str(node_tmp))
    config = make_config(tmp_path)

    DicomConverter(runner=ConversionRunner(), deface_config=config).convert(
        [plan()], persistent / "s03", "russpold/r01network"
    )

    assert not list(node_tmp.iterdir())
    output = persistent / "s03/sub-s03/ses-01/anat/sub-s03_ses-01_T1w.nii.gz"
    assert output.is_file()
    assert json.loads(output.with_name(output.name[:-7] + ".json").read_text())["Defaced"] is True
    assert np.asanyarray(nib.load(output).dataobj)[0, 0, 0] == 0


class FailingDefaceRunner(ConversionRunner):
    def __call__(self, command: list[str], **kwargs: object) -> None:
        if command[0] == "apptainer":
            raise CalledProcessError(1, command)
        super().__call__(command, **kwargs)


class GeometryChangingRunner(ConversionRunner):
    def __call__(self, command: list[str], **kwargs: object) -> None:
        super().__call__(command, **kwargs)
        if command[0] == "apptainer":
            root = Path(command[command.index("--bind") + 1].split(":", 1)[0])
            output = root / command[command.index("--outfile") + 1].removeprefix("/work/")
            nib.save(nib.Nifti1Image(np.zeros((2, 2, 2)), np.eye(4)), output)


class MalformedDefaceRunner(ConversionRunner):
    def __call__(self, command: list[str], **kwargs: object) -> None:
        super().__call__(command, **kwargs)
        if command[0] == "apptainer":
            root = Path(command[command.index("--bind") + 1].split(":", 1)[0])
            output = root / command[command.index("--outfile") + 1].removeprefix("/work/")
            output.write_bytes(b"not a nifti")


class MissingSidecarRunner(ConversionRunner):
    def __call__(self, command: list[str], **kwargs: object) -> None:
        super().__call__(command, **kwargs)
        if command[0] == "dcm2niix":
            output = Path(command[command.index("-o") + 1])
            (output / "converted.json").unlink()


class PersistentArtifactRunner(ConversionRunner):
    def __init__(self, artifact: str, outside: Path) -> None:
        super().__init__()
        self.artifact = artifact
        self.outside = outside

    def __call__(self, command: list[str], **kwargs: object) -> None:
        super().__call__(command, **kwargs)
        if command[0] != "dcm2niix":
            return
        converted = Path(command[command.index("-o") + 1])
        sensitive = converted.parent.parent
        staged = sensitive / "bids"
        if self.artifact == "container-scratch":
            scratch = staged / ".network-fw2bids-tmp"
            scratch.mkdir()
            (scratch / "undefaced.nii.gz").write_bytes(b"sensitive")
        elif self.artifact == "unexpected-nifti":
            (staged / "sub-s03/ses-01/func").mkdir(parents=True)
            (staged / "sub-s03/ses-01/func/unplanned.nii.gz").write_bytes(b"artifact")
        else:
            (staged / "source-link").symlink_to(self.outside)


def test_pydeface_failure_publishes_nothing(tmp_path, monkeypatch):
    with pytest.raises(Exception, match="PyDeface"):
        convert(tmp_path, monkeypatch, FailingDefaceRunner())
    assert not (tmp_path / "persistent/s03").exists()


def test_geometry_changing_deface_output_publishes_nothing(tmp_path, monkeypatch):
    with pytest.raises(Exception, match="geometry"):
        convert(tmp_path, monkeypatch, GeometryChangingRunner())
    assert not (tmp_path / "persistent/s03").exists()


def test_malformed_deface_output_publishes_nothing(tmp_path, monkeypatch):
    with pytest.raises(Exception, match="readable NIfTI"):
        convert(tmp_path, monkeypatch, MalformedDefaceRunner())
    assert not (tmp_path / "persistent/s03").exists()


def test_missing_anatomical_sidecar_publishes_nothing(tmp_path, monkeypatch):
    with pytest.raises(ConversionError, match="JSON sidecar"):
        convert(tmp_path, monkeypatch, MissingSidecarRunner())
    assert not (tmp_path / "persistent/s03").exists()


@pytest.mark.parametrize("artifact", ["container-scratch", "unexpected-nifti", "source-symlink"])
def test_unpublishable_sensitive_artifacts_publish_nothing(tmp_path, monkeypatch, artifact):
    outside = tmp_path / "outside"
    outside.write_text("not BIDS")

    with pytest.raises(ConversionError, match="publishable|symbolic link"):
        convert(tmp_path, monkeypatch, PersistentArtifactRunner(artifact, outside))

    assert not (tmp_path / "persistent/s03").exists()


def test_sensitive_cleanup_failure_publishes_nothing(tmp_path, monkeypatch):
    real_workspace = conversion_module.sensitive_workspace

    @contextmanager
    def cleanup_failure():
        with real_workspace() as workspace:
            yield workspace
        raise ConversionError("sensitive workspace cleanup did not complete")

    monkeypatch.setattr(conversion_module, "sensitive_workspace", cleanup_failure)
    with pytest.raises(ConversionError, match="cleanup"):
        convert(tmp_path, monkeypatch, ConversionRunner())
    assert not (tmp_path / "persistent/s03").exists()


def test_safe_copy_checksum_mismatch_publishes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(DicomConverter, "_sha256", staticmethod(lambda _path: "0" * 64))
    with pytest.raises(ConversionError, match="checksum"):
        convert(tmp_path, monkeypatch, ConversionRunner())
    assert not (tmp_path / "persistent/s03").exists()


def test_existing_destination_is_never_replaced(tmp_path, monkeypatch):
    node_tmp = tmp_path / "node-tmp"
    destination = tmp_path / "persistent/s03"
    node_tmp.mkdir()
    destination.mkdir(parents=True)
    marker = destination / "keep"
    marker.write_text("original")
    monkeypatch.setenv("SLURM_TMPDIR", str(node_tmp))
    with pytest.raises(ConversionError, match="already exists"):
        DicomConverter(ConversionRunner(), make_config(tmp_path)).convert(
            [plan()], destination, "russpold/r01network"
        )
    assert marker.read_text() == "original"


def test_functional_only_conversion_writes_an_empty_success_receipt(tmp_path, monkeypatch):
    destination = convert(tmp_path, monkeypatch, ConversionRunner(), [plan("func")])
    receipt = json.loads((destination / "code/network_fw2bids/defacing/sub-s03.json").read_text())
    assert receipt["status"] == "success"
    assert receipt["images"] == []


def test_commands_and_receipt_exclude_token_and_persistent_paths(tmp_path, monkeypatch):
    token = "secret-token"
    monkeypatch.setenv("FLYWHEEL_API_TOKEN", token)
    runner = ConversionRunner()
    destination = convert(tmp_path, monkeypatch, runner)
    commands = "\n".join(" ".join(command) for command in runner.commands)
    receipt = (destination / "code/network_fw2bids/defacing/sub-s03.json").read_text()

    assert token not in commands
    assert str(tmp_path / "persistent") not in commands
    assert token not in receipt
    assert str(tmp_path / "node-tmp") not in receipt
