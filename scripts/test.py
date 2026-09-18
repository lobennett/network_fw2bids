import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import flywheel


FUNCTIONAL = re.compile(r"^task-(?P<task>[A-Za-z0-9]+)_bold(?:_\d+|_run_\d+)?$")
TASKS = {
    "rest",
    "cuedTS",
    "spatialTS",
    "directedForgetting",
    "flanker",
    "goNogo",
    "nBack",
    "shapeMatching",
    "stopSignal",
    "cuedTSWFlanker",
    "directedForgettingWCuedTS",
    "directedForgettingWFlanker",
    "flankerWShapeMatching",
    "nBackWShapeMatching",
    "nBackWSpatialTS",
    "shapeMatchingWCuedTS",
    "spatialTSWCuedTS",
    "spatialTSWShapeMatching",
    "stopSignalWDirectedForgetting",
    "stopSignalWFlanker",
}
NON_FUNCTIONAL = {
    "NEW Sag_MPRAGE_T1": {
        "modality": "anat",
        "suffix": "T1w",
        "acq": "SagMPRAGE",
    },
    "T2w CUBE PROMO .8mm sag": {
        "modality": "anat",
        "suffix": "T2w",
        "acq": "CubePromo",
    },
    "DTI_pe0_g105": {"modality": "dwi", "suffix": "dwi", "dir": "AP", "acq": "g105"},
    "DTI_pe1_g105": {"modality": "dwi", "suffix": "dwi", "dir": "PA", "acq": "g105"},
    "DTI_pe1_g71": {"modality": "dwi", "suffix": "dwi", "dir": "PA", "acq": "g71"},
    "fmap-fieldmap": {"modality": "fmap"},
}
SKIP_ACQUISITIONS = {
    "3Plane Loc SSFSE",
    "3Plane Loc SSFSE_1",
    "GE HOS FOV28",
    "GE HOS FOV28_1",
    "GE HOS FOV28_2",
    "GE HOS FOV28_3",
    "GE HOS FOV28_4",
    "HO Shim",
    "Processed Images",
    "Processed Images_1",
    "run-1_sbref",
    "fmap-fieldmap_1",
    "T1w MPRAGE PROMO",
}
SUBJECT_ALIASES = {"s19-2": "s19", "s29-2": "s29", "s43-2": "s43", "ex26207": "s297"}
SESSION_OVERRIDES = {
    "s03": {"22752": {"reassign_to": "s10"}},
    "s29": {"22424": {"exclude": True}},
}
SESSION_MERGES = {
    "s1258": {"unknown_2": "28338"},
    "s1391": {"unknown": "28270"},
    "s1445": {"unknown_5": "28037"},
}


@dataclass(frozen=True)
class ArchivePlan:
    acquisition: Any
    dicom_file: Any
    relative_prefix: Path
    modality: str
    task: str | None = None


def map_acquisition(label: str) -> dict[str, str] | None:
    if label in SKIP_ACQUISITIONS or label.endswith("_qa-reject"):
        return None
    match = FUNCTIONAL.match(label)
    if match and match["task"] in TASKS:
        return {"modality": "func", "suffix": "bold", "task": match["task"]}
    return NON_FUNCTIONAL.get(label)


def timestamp_key(container: Any) -> float:
    timestamp = getattr(container, "timestamp", None)
    return timestamp.timestamp() if timestamp is not None else float("inf")


def normalize_label(label: str) -> str:
    return re.sub("sub-", "", re.sub("ses-", "", label))


def relevant_subject_labels(canonical: str) -> set[str]:
    return (
        {canonical}
        | {label for label, target in SUBJECT_ALIASES.items() if target == canonical}
        | {
            source
            for source, overrides in SESSION_OVERRIDES.items()
            for override in overrides.values()
            if override.get("reassign_to") == canonical
        }
    )


class FlywheelBIDS:
    def __init__(
        self,
        client: Any,
        project_path: str = "russpold/r01network",
        runner: Any = subprocess.run,
    ) -> None:
        self.client = client
        self.project_path = project_path
        self.runner = runner

    def plan_subject(self, subject_label: str) -> list[ArchivePlan]:
        project = self.client.lookup(self.project_path)
        if project is None:
            raise ValueError(f"Flywheel project {self.project_path!r} was not found")
        if subject_label == "n01":
            raise ValueError("pilot subject n01 uses an unsupported naming convention")

        records: list[Any] = []
        for flywheel_label in sorted(relevant_subject_labels(subject_label)):
            subject = project.subjects.find_first(f'label="{flywheel_label}"')
            if subject is None:
                continue
            for session in subject.sessions():
                override = SESSION_OVERRIDES.get(flywheel_label, {}).get(
                    session.label, {}
                )
                if override.get("exclude"):
                    continue
                canonical = override.get("reassign_to") or SUBJECT_ALIASES.get(
                    flywheel_label, flywheel_label
                )
                if canonical == subject_label:
                    records.append(session)
        if not records:
            raise ValueError(f"Flywheel subject {subject_label!r} was not found")

        merges = {
            normalize_label(stray): normalize_label(twin)
            for stray, twin in SESSION_MERGES.get(subject_label, {}).items()
        }
        session_numbers: dict[str, int] = {}
        next_number = 0
        for session in sorted(records, key=timestamp_key):
            label = normalize_label(session.label)
            if label in merges:
                continue
            if label in session_numbers:
                raise ValueError(f"duplicate session label {session.label!r}")
            next_number += 1
            session_numbers[label] = next_number
        for stray, twin in merges.items():
            if twin in session_numbers:
                session_numbers[stray] = session_numbers[twin]

        plans: list[ArchivePlan] = []
        run_counts_by_session: dict[int, dict[str, int]] = {}
        for session in sorted(records, key=timestamp_key):
            normalized_session = normalize_label(session.label)
            if normalized_session not in session_numbers:
                raise ValueError(
                    f"merged session {session.label!r} has no numbered twin"
                )
            session_number = session_numbers[normalized_session]
            session_label = f"ses-{session_number:02d}"
            run_counts = run_counts_by_session.setdefault(session_number, {})
            for acquisition in sorted(session.acquisitions(), key=timestamp_key):
                dicom_files = [file for file in acquisition.files if file.type == "dicom"]
                if not dicom_files:
                    continue
                entities = map_acquisition(acquisition.label)
                if entities is None:
                    if acquisition.label in SKIP_ACQUISITIONS or acquisition.label.endswith(
                        "_qa-reject"
                    ):
                        continue
                    raise ValueError(
                        f"no BIDS mapping for acquisition {acquisition.label!r}"
                    )

                modality = entities["modality"]
                directory = Path(f"sub-{subject_label}") / session_label / modality
                stem = f"sub-{subject_label}_{session_label}"
                if modality == "func":
                    task = entities["task"]
                    run_counts[task] = run_counts.get(task, 0) + 1
                    stem += f"_task-{task}_run-{run_counts[task]}_bold"
                elif modality == "anat":
                    stem += f"_acq-{entities['acq']}_run-1_{entities['suffix']}"
                elif modality == "dwi":
                    stem += (
                        f"_acq-{entities['acq']}_dir-{entities['dir']}_run-1_dwi"
                    )
                else:
                    stem += "_run-1"

                if len(dicom_files) != 1:
                    raise ValueError(
                        f"expected one DICOM archive for {acquisition.label!r}, "
                        f"found {len(dicom_files)}"
                    )
                plans.append(
                    ArchivePlan(
                        acquisition=acquisition,
                        dicom_file=dicom_files[0],
                        relative_prefix=directory / stem,
                        modality=modality,
                        task=entities.get("task"),
                    )
                )
        return plans

    def convert_subject(
        self,
        subject_label: str,
        destination: Path,
        plans: list[ArchivePlan] | None = None,
    ) -> None:
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"BIDS destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)

        plans = plans if plans is not None else self.plan_subject(subject_label)
        with tempfile.TemporaryDirectory(
            prefix=".network-fw2bids-", dir=destination.parent
        ) as scratch_name:
            scratch = Path(scratch_name)
            staged_dataset = scratch / "bids"
            staged_dataset.mkdir()
            (staged_dataset / "dataset_description.json").write_text(
                json.dumps(
                    {
                        "Name": f"{self.project_path} export",
                        "BIDSVersion": "1.10.1",
                    },
                    indent=2,
                )
                + "\n"
            )
            for index, plan in enumerate(plans):
                self._convert_archive(plan, staged_dataset, scratch / f"archive-{index}")
            os.replace(staged_dataset, destination)

    def _convert_archive(
        self, plan: ArchivePlan, dataset_root: Path, workspace: Path
    ) -> None:
        workspace.mkdir()
        archive_path = workspace / "dicom.zip"
        plan.acquisition.download_file(plan.dicom_file.name, str(archive_path))

        dicom_directory = workspace / "dicoms"
        dicom_directory.mkdir()
        self._safe_extract(archive_path, dicom_directory)

        converted_directory = workspace / "converted"
        converted_directory.mkdir()
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
            self.runner(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise RuntimeError(
                "dcm2niix is required for conversion but was not found on PATH"
            ) from exc

        images = sorted(
            path
            for path in converted_directory.iterdir()
            if path.name.endswith((".nii", ".nii.gz"))
        )
        self._place_converted_images(images, plan, dataset_root)

    @staticmethod
    def _safe_extract(archive_path: Path, destination: Path) -> None:
        root = destination.resolve()
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if target != root and root not in target.parents:
                    raise ValueError(f"unsafe path in DICOM archive: {member.filename!r}")
            archive.extractall(destination)

    @classmethod
    def _place_converted_images(
        cls, images: list[Path], plan: ArchivePlan, dataset_root: Path
    ) -> None:
        if plan.modality != "fmap":
            if len(images) != 1:
                raise ValueError(
                    f"expected one converted image for {plan.acquisition.label!r}, "
                    f"found {len(images)}"
                )
            cls._place_image(images[0], plan, dataset_root)
            return

        roles: dict[str, Path] = {}
        for image in images:
            source_stem = image.name.removesuffix(".nii.gz").removesuffix(".nii")
            sidecar = image.with_name(source_stem).with_suffix(".json")
            if not sidecar.exists():
                raise ValueError(f"dcm2niix did not produce a JSON sidecar for {image.name}")
            metadata = json.loads(sidecar.read_text())
            image_type = {str(value).upper() for value in metadata.get("ImageType", [])}
            component = str(metadata.get("ComplexImageComponent", "")).upper()
            role = (
                "fieldmap"
                if source_stem.lower().endswith("_ph")
                or "P" in image_type
                or component == "PHASE"
                else "magnitude"
            )
            if role in roles:
                raise ValueError(
                    f"multiple {role} images produced for {plan.acquisition.label!r}"
                )
            roles[role] = image
        if set(roles) != {"fieldmap", "magnitude"}:
            raise ValueError(
                f"expected magnitude and phase outputs for {plan.acquisition.label!r}; "
                f"found {sorted(roles)}"
            )
        cls._place_image(
            roles["magnitude"],
            plan,
            dataset_root,
            relative_prefix=Path(f"{plan.relative_prefix}_magnitude"),
        )
        cls._place_image(
            roles["fieldmap"],
            plan,
            dataset_root,
            relative_prefix=Path(f"{plan.relative_prefix}_fieldmap"),
            metadata_updates={"Units": "Hz"},
        )

    @staticmethod
    def _place_image(
        image: Path,
        plan: ArchivePlan,
        dataset_root: Path,
        relative_prefix: Path | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> None:
        source_stem = image.name.removesuffix(".nii.gz").removesuffix(".nii")
        source_prefix = image.with_name(source_stem)
        destination_prefix = dataset_root / (relative_prefix or plan.relative_prefix)
        destination_prefix.parent.mkdir(parents=True, exist_ok=True)

        image_extension = ".nii.gz" if image.name.endswith(".nii.gz") else ".nii"
        shutil.copy2(image, Path(f"{destination_prefix}{image_extension}"))

        sidecar = source_prefix.with_suffix(".json")
        if not sidecar.exists():
            raise ValueError(f"dcm2niix did not produce a JSON sidecar for {image.name}")
        metadata = json.loads(sidecar.read_text())
        if plan.task:
            metadata["TaskName"] = plan.task
        metadata.update(metadata_updates or {})
        destination_prefix.with_suffix(".json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        )
        for extension in (".bval", ".bvec"):
            source = source_prefix.with_suffix(extension)
            if source.exists():
                shutil.copy2(source, destination_prefix.with_suffix(extension))


def get_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download one Flywheel subject and convert its DICOM archives to BIDS"
    )
    parser.add_argument("--subject", required=True, help="Flywheel subject label")
    parser.add_argument(
        "--project",
        default="russpold/r01network",
        help="Flywheel group/project path",
    )
    parser.add_argument("--output", type=Path, help="new BIDS dataset directory")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="download and convert; without this flag, only print the plan",
    )
    return parser


def main(
    argv: list[str] | None = None,
    client_factory: Any = flywheel.Client,
) -> int:
    parser = get_parser()
    args = parser.parse_args(argv)
    if args.execute and args.output is None:
        parser.error("--execute requires --output")

    token = os.environ.get("FLYWHEEL_API_TOKEN")
    if not token:
        raise SystemExit("FLYWHEEL_API_TOKEN is not set")
    converter = FlywheelBIDS(client_factory(token), project_path=args.project)
    plans = converter.plan_subject(args.subject)
    for plan in plans:
        print(
            f"{plan.acquisition.label}: {plan.dicom_file.name} "
            f"-> {plan.relative_prefix}"
        )
    print(f"{len(plans)} DICOM archives planned for {args.subject}")

    if args.execute:
        converter.convert_subject(args.subject, args.output, plans=plans)
        print(f"BIDS dataset written to {args.output}")
    else:
        print("Dry run only; pass --execute --output DIR to download and convert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
