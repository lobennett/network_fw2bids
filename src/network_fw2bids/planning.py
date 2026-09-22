from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from flywheel.rest import ApiException

from .errors import PlanningError
from . import rules


@dataclass(frozen=True)
class ArchivePlan:
    acquisition: Any
    dicom_file: Any
    relative_prefix: Path
    modality: str
    task: str | None = None


class SubjectPlanner:
    def __init__(self, client: Any, project_path: str) -> None:
        self._client = client
        self._project_path = project_path

    def plan(self, subject_label: str) -> list[ArchivePlan]:
        if not re.fullmatch(r"[A-Za-z0-9]+", subject_label):
            raise PlanningError("BIDS subject label must contain only ASCII letters and digits")
        if subject_label == "n01":
            raise PlanningError("pilot subject n01 uses an unsupported naming convention")

        try:
            project = self._client.lookup(self._project_path)
            if project is None:
                raise PlanningError(
                    f"Flywheel project {self._project_path!r} was not found"
                )
            sessions = self._canonical_sessions(project, subject_label)
            session_numbers = self._number_sessions(sessions, subject_label)
            return self._build_plans(subject_label, sessions, session_numbers)
        except ApiException as exc:
            raise PlanningError(f"could not read Flywheel project {self._project_path!r}") from exc

    def _canonical_sessions(self, project: Any, subject_label: str) -> list[Any]:
        subject = project.subjects.find_first(f'label="{subject_label}"')
        if subject is None:
            subject = next(
                (
                    candidate
                    for candidate in project.subjects()
                    if candidate.label == subject_label
                ),
                None,
            )
        if subject is None:
            raise PlanningError(f"Flywheel subject {subject_label!r} was not found")
        sessions = list(subject.sessions())
        if not sessions:
            raise PlanningError(f"Flywheel subject {subject_label!r} has no sessions")
        return sessions

    def _number_sessions(
        self, sessions: list[Any], subject_label: str
    ) -> dict[str, int]:
        merges = {
            rules.normalize_label(stray): rules.normalize_label(twin)
            for stray, twin in rules.SESSION_MERGES.get(subject_label, {}).items()
        }
        session_numbers: dict[str, int] = {}
        next_number = 0
        for session in sorted(sessions, key=self._timestamp_key):
            label = rules.normalize_label(session.label)
            if label in merges:
                continue
            if label in session_numbers:
                raise PlanningError(f"duplicate session label {session.label!r}")
            next_number += 1
            session_numbers[label] = next_number
        for stray, twin in merges.items():
            if twin in session_numbers:
                session_numbers[stray] = session_numbers[twin]
        return session_numbers

    def _build_plans(
        self,
        subject_label: str,
        sessions: list[Any],
        session_numbers: dict[str, int],
    ) -> list[ArchivePlan]:
        plans: list[ArchivePlan] = []
        run_counts_by_session: dict[int, dict[str, int]] = {}
        for session in sorted(sessions, key=self._timestamp_key):
            normalized_session = rules.normalize_label(session.label)
            if normalized_session not in session_numbers:
                raise PlanningError(
                    f"merged session {session.label!r} has no numbered twin"
                )
            session_number = session_numbers[normalized_session]
            session_label = f"ses-{session_number:02d}"
            run_counts = run_counts_by_session.setdefault(session_number, {})
            for acquisition in sorted(session.acquisitions(), key=self._timestamp_key):
                dicom_files = [file for file in acquisition.files if file.type == "dicom"]
                if not dicom_files:
                    continue
                rule = rules.map_acquisition(acquisition.label)
                if rule is None:
                    if (
                        acquisition.label in rules.SKIP_ACQUISITIONS
                        or acquisition.label.endswith("_qa-reject")
                    ):
                        continue
                    raise PlanningError(
                        f"no BIDS mapping for acquisition {acquisition.label!r}"
                    )
                if len(dicom_files) != 1:
                    raise PlanningError(
                        f"expected one DICOM archive for {acquisition.label!r}, "
                        f"found {len(dicom_files)}"
                    )
                prefix = self._build_prefix(
                    subject_label, session_label, rule, run_counts
                )
                plans.append(
                    ArchivePlan(
                        acquisition=acquisition,
                        dicom_file=dicom_files[0],
                        relative_prefix=prefix,
                        modality=rule.modality,
                        task=rule.task,
                    )
                )
        return plans

    @staticmethod
    def _build_prefix(
        subject_label: str,
        session_label: str,
        rule: rules.AcquisitionRule,
        run_counts: dict[str, int],
    ) -> Path:
        directory = Path(f"sub-{subject_label}") / session_label / rule.modality
        stem = f"sub-{subject_label}_{session_label}"
        if rule.modality == "func":
            task = rule.task
            if task is None:
                raise PlanningError("functional acquisition is missing its task")
            run_counts[task] = run_counts.get(task, 0) + 1
            stem += f"_task-{task}_run-{run_counts[task]}_{rule.suffix}"
        elif rule.modality == "anat":
            stem += f"_acq-{rule.acquisition}_run-1_{rule.suffix}"
        elif rule.modality == "dwi":
            stem += (
                f"_acq-{rule.acquisition}_dir-{rule.direction}_run-1_{rule.suffix}"
            )
        else:
            stem += "_run-1"
        return directory / stem

    @staticmethod
    def _timestamp_key(container: Any) -> float:
        timestamp = getattr(container, "timestamp", None)
        return timestamp.timestamp() if timestamp is not None else float("inf")
