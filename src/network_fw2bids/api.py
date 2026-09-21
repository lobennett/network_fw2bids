"""Public library façade for planning and converting Flywheel subjects."""

import subprocess
from pathlib import Path
from typing import Any, Callable

import flywheel
from flywheel.rest import ApiException

from .conversion import DicomConverter
from .defacing import DefaceConfig
from .errors import PlanningError
from .planning import ArchivePlan, SubjectPlanner


DEFAULT_PROJECT = "russpold/r01network"


class FlywheelBIDS:
    """Coordinate subject planning and DICOM conversion for one Flywheel project."""

    def __init__(
        self,
        client: Any,
        project_path: str = DEFAULT_PROJECT,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        deface_config: DefaceConfig | None = None,
    ) -> None:
        if deface_config is not None:
            _require_pinned_deface_config(deface_config)
        self._project_path = project_path
        self._planner = SubjectPlanner(client, project_path)
        self._converter = DicomConverter(runner, deface_config)

    @classmethod
    def from_token(
        cls,
        token: str,
        project_path: str = DEFAULT_PROJECT,
        client_factory: Callable[[str], Any] = flywheel.Client,
        deface_config: DefaceConfig | None = None,
    ) -> "FlywheelBIDS":
        if deface_config is not None:
            _require_pinned_deface_config(deface_config)
        try:
            client = client_factory(token)
        except ApiException as exc:
            raise PlanningError("could not authenticate with Flywheel") from exc
        return cls(client, project_path, deface_config=deface_config)

    def plan_subject(self, subject_label: str) -> list[ArchivePlan]:
        return self._planner.plan(subject_label)

    def convert_subject(
        self,
        subject_label: str,
        destination: Path,
        plans: list[ArchivePlan] | None = None,
    ) -> None:
        resolved = plans if plans is not None else self.plan_subject(subject_label)
        self._converter.convert(resolved, destination, self._project_path)


def _require_pinned_deface_config(config: DefaceConfig) -> None:
    config.validate()
