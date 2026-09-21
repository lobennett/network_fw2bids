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

    def __call__(self, command: list[str], *, check: bool) -> None:
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
    def __call__(self, command: list[str], *, check: bool) -> None:
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
        def __call__(self, command: list[str], *, check: bool) -> None:
            output = Path(command[command.index("--outfile") + 1].replace("/work/", ""))
            dataset_root = Path(command[command.index("--bind") + 1].split(":", 1)[0])
            write_image(dataset_root / output, shape=(7, 9, 10))

    with pytest.raises(DefacingError, match="geometry"):
        deface_dataset(bids, "s03", config(tmp_path), runner=GeometryRunner())


def test_receipt_is_written_atomically_and_loaded(tmp_path):
    bids = write_dataset_with_t1w(tmp_path)
    receipt = deface_dataset(bids, "s03", config(tmp_path), runner=FakePyDeface())

    path = write_receipt_atomic(bids, receipt)

    assert path == receipt_path(bids, "s03")
    assert load_receipt(path) == receipt
    assert "bids" not in path.read_text()


def test_receipt_loader_rejects_malformed_content(tmp_path):
    path = tmp_path / "sub-s03.json"
    path.write_text('{"schema_version": 1}')

    with pytest.raises(DefacingError, match="receipt"):
        load_receipt(path)
