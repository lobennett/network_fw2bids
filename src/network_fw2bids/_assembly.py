"""Assemble isolated subject exports into one atomically published BIDS dataset."""

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory

from ._publication import publish_directory
from .errors import ConversionError, NetworkFW2BIDSError


SUBJECT_PATTERN = re.compile(r"s[0-9]+")


def load_subjects(path: Path) -> tuple[str, ...]:
    """Read a strict, ordered, one-label-per-line subject roster."""
    try:
        subjects = tuple(Path(path).read_text().splitlines())
    except OSError as exc:
        raise ConversionError(f"could not read subject roster: {path}") from exc
    if not subjects:
        raise ConversionError(f"subject roster is empty: {path}")
    if any(not SUBJECT_PATTERN.fullmatch(subject) for subject in subjects):
        raise ConversionError(f"subject roster contains an invalid or blank label: {path}")
    if len(set(subjects)) != len(subjects):
        raise ConversionError(f"subject roster contains duplicate labels: {path}")
    return subjects


def _inspect_part(part: Path, subject: str) -> dict[str, object]:
    subject_directory = part / f"sub-{subject}"
    description_path = part / "dataset_description.json"
    expected_names = {description_path.name, subject_directory.name}
    try:
        actual_names = {entry.name for entry in part.iterdir()}
        if actual_names != expected_names:
            raise ConversionError(
                f"subject part {part} must contain only {sorted(expected_names)}"
            )
        if not subject_directory.is_dir() or subject_directory.is_symlink():
            raise ConversionError(f"subject directory is missing or unsafe: {subject_directory}")
        if not description_path.is_file() or description_path.is_symlink():
            raise ConversionError(f"dataset description is missing or unsafe: {description_path}")
        description = json.loads(description_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConversionError(f"could not inspect subject part: {part}") from exc
    if not isinstance(description, dict):
        raise ConversionError(f"dataset description must be a JSON object: {description_path}")
    _require_complete_subject_tree(subject_directory)
    return description


def _require_complete_subject_tree(subject_directory: Path) -> None:
    files: list[Path] = []
    for path in subject_directory.rglob("*"):
        if path.is_symlink():
            raise ConversionError(f"subject part contains a symbolic link: {path}")
        if path.is_file():
            try:
                if path.stat().st_size == 0:
                    raise ConversionError(f"subject part contains an empty file: {path}")
            except OSError as exc:
                raise ConversionError(f"could not inspect subject data file: {path}") from exc
            files.append(path)
        elif not path.is_dir():
            raise ConversionError(f"subject part contains an unsupported filesystem entry: {path}")

    images = [path for path in files if path.name.endswith((".nii", ".nii.gz"))]
    if not images:
        raise ConversionError(f"subject part contains no NIfTI images: {subject_directory}")
    for image in images:
        if image.name.endswith(".nii.gz"):
            sidecar = image.with_name(image.name[:-7] + ".json")
        else:
            sidecar = image.with_suffix(".json")
        if sidecar not in files:
            raise ConversionError(f"NIfTI image has no JSON sidecar: {image}")


def assemble_subject_parts(subjects_file: Path, parts_directory: Path, destination: Path) -> None:
    """Validate every subject part and atomically publish their combined dataset."""
    subjects = load_subjects(subjects_file)
    parts_directory = Path(parts_directory)
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ConversionError(f"BIDS destination already exists: {destination}")

    descriptions: dict[str, dict[str, object]] = {}
    for subject in subjects:
        part = parts_directory / subject
        if not part.is_dir() or part.is_symlink():
            raise ConversionError(f"subject part is missing or unsafe: {part}")
        descriptions[subject] = _inspect_part(part, subject)
    first_description = descriptions[subjects[0]]
    inconsistent = [subject for subject in subjects if descriptions[subject] != first_description]
    if inconsistent:
        raise ConversionError(
            "subject parts have inconsistent dataset descriptions: " + ", ".join(inconsistent)
        )

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".network-fw2bids-assembly-", dir=destination.parent) as scratch:
            staged = Path(scratch) / "bids"
            staged.mkdir()
            (staged / "dataset_description.json").write_text(
                json.dumps(first_description, indent=2) + "\n"
            )
            for subject in subjects:
                shutil.copytree(
                    parts_directory / subject / f"sub-{subject}",
                    staged / f"sub-{subject}",
                    symlinks=False,
                )
            publish_directory(staged, destination)
    except OSError as exc:
        raise ConversionError(f"could not assemble BIDS dataset at {destination}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble subject BIDS parts")
    parser.add_argument("--subjects", required=True, type=Path)
    parser.add_argument("--parts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        assemble_subject_parts(args.subjects, args.parts, args.output)
    except NetworkFW2BIDSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"BIDS dataset assembled at {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
