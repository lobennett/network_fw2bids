import hashlib
import json
from pathlib import Path
import shutil

import nibabel as nib
import numpy as np
import pytest

from network_fw2bids.defacing import (
    DefaceConfig,
    DefacingError,
    deface_dataset,
    load_receipt,
    receipt_path,
    write_receipt_atomic,
)


T1W_PATH = "sub-s03/ses-01/anat/sub-s03_ses-01_T1w.nii.gz"
T2W_PATH = "sub-s03/ses-01/anat/sub-s03_ses-01_T2w.nii.gz"
BOLD_PATH = "sub-s03/ses-01/func/sub-s03_ses-01_task-flanker_bold.nii.gz"
T1W_JSON = "sub-s03/ses-01/anat/sub-s03_ses-01_T1w.json"
T2W_JSON = "sub-s03/ses-01/anat/sub-s03_ses-01_T2w.json"


def write_image(path: Path, shape=(8, 9, 10)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.arange(np.prod(shape), dtype=np.float32).reshape(shape)
    affine = np.array(
        [[1.1, 0, 0, -4], [0, 1.2, 0, 2], [0, 0, 1.3, 7], [0, 0, 0, 1]],
        dtype=np.float64,
    )
    image = nib.Nifti1Image(data, affine)
    image.header.set_zooms((1.1, 1.2, 1.3) + (1.0,) * (len(shape) - 3))
    nib.save(image, path)


def write_dataset_with_t1w(tmp_path: Path) -> Path:
    bids = tmp_path / "bids"
    write_image(bids / T1W_PATH)
    (bids / T1W_JSON).write_text(json.dumps({"Manufacturer": "Example"}))
    return bids


def write_dataset_with_t1w_t2w_and_bold(tmp_path: Path) -> Path:
    bids = write_dataset_with_t1w(tmp_path)
    write_image(bids / T2W_PATH)
    (bids / T2W_JSON).write_text("{}")
    write_image(bids / BOLD_PATH, shape=(8, 9, 10, 2))
    return bids


def config(tmp_path: Path) -> DefaceConfig:
    image = tmp_path / "pydeface.sif"
    image.write_bytes(b"pinned-container")
    return DefaceConfig(
        image=image,
        version="2.1.0",
        sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
    )


class FakePyDeface:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str], *, check: bool, **_kwargs: object) -> None:
        assert check is True
        self.commands.append(command)
        source = Path(command[command.index("pydeface") + 1].replace("/work/", ""))
        output = Path(command[command.index("--outfile") + 1].replace("/work/", ""))
        dataset_root = Path(command[command.index("--bind") + 1].split(":", 1)[0])
        output_path = dataset_root / output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image = nib.load(dataset_root / source)
        data = image.get_fdata(dtype=np.float32)
        data[0, 0, 0] = -1
        nib.save(nib.Nifti1Image(data, image.affine, image.header), output_path)


class CopyInputRunner:
    def __call__(self, command: list[str], *, check: bool, **_kwargs: object) -> None:
        source = Path(command[command.index("pydeface") + 1].replace("/work/", ""))
        output = Path(command[command.index("--outfile") + 1].replace("/work/", ""))
        dataset_root = Path(command[command.index("--bind") + 1].split(":", 1)[0])
        destination = dataset_root / output
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dataset_root / source, destination)


def test_deface_dataset_replaces_only_anatomy(tmp_path):
    bids = write_dataset_with_t1w_t2w_and_bold(tmp_path)
    original_bold = (bids / BOLD_PATH).read_bytes()
    runner = FakePyDeface()

    receipt = deface_dataset(bids, "s03", config(tmp_path), runner=runner)

    assert [item.path for item in receipt.images] == [T1W_PATH, T2W_PATH]
    assert nib.load(bids / T1W_PATH).shape == (8, 9, 10)
    assert (bids / BOLD_PATH).read_bytes() == original_bold
    assert json.loads((bids / T1W_JSON).read_text())["Defaced"] is True
    assert all("--cleanenv" in command and "--containall" in command for command in runner.commands)
    assert all("/work/" in " ".join(command) for command in runner.commands)


def test_deface_dataset_rejects_unchanged_output(tmp_path):
    bids = write_dataset_with_t1w(tmp_path)

    with pytest.raises(DefacingError, match="unchanged"):
        deface_dataset(bids, "s03", config(tmp_path), runner=CopyInputRunner())


def test_deface_dataset_rejects_geometry_change(tmp_path):
    bids = write_dataset_with_t1w(tmp_path)

    class GeometryRunner:
        def __call__(self, command: list[str], *, check: bool, **_kwargs: object) -> None:
            output = Path(command[command.index("--outfile") + 1].replace("/work/", ""))
            dataset_root = Path(command[command.index("--bind") + 1].split(":", 1)[0])
            write_image(dataset_root / output, shape=(7, 9, 10))

    with pytest.raises(DefacingError, match="geometry"):
        deface_dataset(bids, "s03", config(tmp_path), runner=GeometryRunner())


@pytest.mark.parametrize("unsafe_directory", ["session", "anat"])
def test_deface_dataset_rejects_anatomy_behind_symlinked_directory(tmp_path, unsafe_directory):
    bids = tmp_path / "bids"
    subject = bids / "sub-s03"
    subject.mkdir(parents=True)
    target = tmp_path / "target"
    if unsafe_directory == "session":
        write_image(target / "anat" / "sub-s03_ses-01_T1w.nii.gz")
        (target / "anat" / "sub-s03_ses-01_T1w.json").write_text("{}")
        (subject / "ses-01").symlink_to(target, target_is_directory=True)
    else:
        session = subject / "ses-01"
        session.mkdir()
        write_image(target / "sub-s03_ses-01_T1w.nii.gz")
        (target / "sub-s03_ses-01_T1w.json").write_text("{}")
        (session / "anat").symlink_to(target, target_is_directory=True)

    with pytest.raises(DefacingError, match="unsafe"):
        deface_dataset(bids, "s03", config(tmp_path), runner=FakePyDeface())


def test_deface_dataset_rejects_preexisting_defaced_output_before_runner(tmp_path):
    bids = write_dataset_with_t1w(tmp_path)
    stale_output = bids / "sub-s03/ses-01/anat/.sub-s03_ses-01_T1w.defaced.nii.gz"
    stale_output.write_bytes(b"stale")

    class Runner:
        called = False

        def __call__(self, command: list[str], *, check: bool, **_kwargs: object) -> None:
            self.called = True

    runner = Runner()
    with pytest.raises(DefacingError, match="already exists"):
        deface_dataset(bids, "s03", config(tmp_path), runner=runner)
    assert runner.called is False


def test_receipt_is_written_atomically_and_loaded(tmp_path):
    bids = write_dataset_with_t1w(tmp_path)
    pinned = config(tmp_path)
    receipt = deface_dataset(bids, "s03", pinned, runner=FakePyDeface())

    path = write_receipt_atomic(bids, receipt)

    assert path == receipt_path(bids, "s03")
    assert load_receipt(path) == receipt
    serialized = json.loads(path.read_text())
    assert serialized["software"] == {
        "name": "PyDeface",
        "version": "2.1.0",
        "container": "pydeface.sif",
        "sha256": pinned.sha256,
    }
    assert str(pinned.image) not in path.read_text()


@pytest.mark.parametrize(
    "software",
    [
        {"name": "PyDeface", "version": "2.1.0", "sha256": "a" * 64},
        {"name": "PyDeface", "version": "2.1.0", "container": "image.sif", "sha256": "A" * 64},
        {"name": "Other", "version": "2.1.0", "container": "image.sif", "sha256": "a" * 64},
        {"name": "PyDeface", "version": "", "container": "image.sif", "sha256": "a" * 64},
        {"name": "PyDeface", "version": "2.1.0", "container": "/tmp/image.sif", "sha256": "a" * 64},
        {"name": "PyDeface", "version": "2.1.0", "container": "image.sif", "sha256": "a" * 64, "image": "extra"},
    ],
)
def test_receipt_loader_requires_exact_pydeface_provenance(tmp_path, software):
    path = tmp_path / "sub-s03.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "subject": "s03",
        "status": "success",
        "software": software,
        "images": [],
    }))

    with pytest.raises(DefacingError, match="software"):
        load_receipt(path)


def test_receipt_loader_rejects_malformed_content(tmp_path):
    path = tmp_path / "sub-s03.json"
    path.write_text('{"schema_version": 1}')

    with pytest.raises(DefacingError, match="receipt"):
        load_receipt(path)


@pytest.mark.parametrize("location", ["func", "fmap", "", "ses-01/other", "unexpected/anat"])
@pytest.mark.parametrize("suffix", ["T1w.nii", "T2w.nii.gz"])
def test_misplaced_anatomy_is_rejected_before_running(tmp_path, location, suffix):
    bids = write_dataset_with_t1w(tmp_path)
    misplaced = bids / "sub-s03" / location / f"sub-s03_{suffix}"
    write_image(misplaced)
    runner = FakePyDeface()
    with pytest.raises(DefacingError, match="misplaced anatomical"):
        deface_dataset(bids, "s03", config(tmp_path), runner=runner)
    assert runner.commands == []


def test_runtime_has_private_directories_and_ignores_ambient_mounts(tmp_path, monkeypatch):
    import os
    import stat

    bids = write_dataset_with_t1w(tmp_path)
    for variable in ("APPTAINER_BIND", "APPTAINER_BINDPATH", "APPTAINER_MOUNT", "SINGULARITY_BIND", "APPTAINERENV_TMPDIR"):
        monkeypatch.setenv(variable, "/persistent:/leak:rw")
    monkeypatch.setenv("FLYWHEEL_API_TOKEN", "sensitive-token")

    def runtime(command, **kwargs):
        environment = kwargs["env"]
        assert not any(key.startswith(("APPTAINER", "SINGULARITY")) for key in environment)
        assert "FLYWHEEL_API_TOKEN" not in environment
        disabled = set(command[command.index("--no-mount") + 1].split(","))
        assert {"home", "cwd", "hostfs", "bind-paths", "tmp"} <= disabled
        assert command.count("--bind") == 1
        assert command[command.index("--pwd") + 1] == "/work/.network-fw2bids-tmp"
        for name, variable in ((".network-fw2bids-home", "HOME"), (".network-fw2bids-tmp", "TMPDIR")):
            path = bids / name
            assert path.is_dir()
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
            assert environment[variable] == str(path)
            assert f"{variable}=/work/{name}" in command
            (path / "sensitive-intermediate.nii.gz").write_bytes(b"private voxels")
        assert Path(kwargs["cwd"]) == bids / ".network-fw2bids-tmp"
        FakePyDeface()(command, check=kwargs["check"])

    deface_dataset(bids, "s03", config(tmp_path), runner=runtime)
    assert not list(bids.glob(".network-fw2bids-*"))
    assert os.environ["APPTAINER_BIND"] == "/persistent:/leak:rw"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_pydeface_subprocess_output_is_never_exposed(tmp_path, capfd, exit_code):
    import subprocess
    import sys
    import traceback

    bids = write_dataset_with_t1w(tmp_path)
    marker = f"sensitive-tool-output {tmp_path}/raw-dicom.dcm"

    def noisy_runtime(command, **kwargs):
        subprocess.run(
            [sys.executable, "-c", f"import sys; print({marker!r}); print({marker!r}, file=sys.stderr); sys.exit({exit_code})"],
            **kwargs,
        )
        FakePyDeface()(command, check=kwargs["check"])

    if exit_code:
        with pytest.raises(DefacingError) as caught:
            deface_dataset(bids, "s03", config(tmp_path), runner=noisy_runtime)
        rendered = "".join(traceback.format_exception(caught.value))
        assert marker not in rendered
        assert str(tmp_path) not in str(caught.value)
        assert T1W_PATH in str(caught.value)
    else:
        deface_dataset(bids, "s03", config(tmp_path), runner=noisy_runtime)
    captured = capfd.readouterr()
    assert marker not in captured.out + captured.err
    assert not list(bids.glob(".network-fw2bids-*"))


@pytest.mark.parametrize("delimiter", [":", ",", "\n"])
def test_container_rejects_bind_path_delimiters_before_running(tmp_path, delimiter):
    directory = tmp_path / f"scratch{delimiter}unsafe"
    directory.mkdir()
    bids = write_dataset_with_t1w(directory)
    runner = FakePyDeface()
    with pytest.raises(DefacingError, match="unsafe container bind"):
        deface_dataset(bids, "s03", config(tmp_path), runner=runner)
    assert runner.commands == []
