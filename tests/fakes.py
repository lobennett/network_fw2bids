from dataclasses import dataclass, field
from datetime import datetime


class Finder:
    def __init__(self, items: list) -> None:
        self.items = items

    def find_first(self, query: str):
        label = query.split('"')[1]
        return next((item for item in self.items if item.label == label), None)


@dataclass
class FakeFile:
    name: str = "scan.dicom.zip"
    type: str = "dicom"


@dataclass
class FakeAcquisition:
    label: str
    timestamp: datetime
    files: list[FakeFile] = field(default_factory=lambda: [FakeFile()])
    source_session_label: str | None = None


@dataclass
class FakeSession:
    label: str
    timestamp: datetime
    acquisition_items: list[FakeAcquisition]

    def acquisitions(self) -> list[FakeAcquisition]:
        for acquisition in self.acquisition_items:
            acquisition.source_session_label = self.label
        return self.acquisition_items


@dataclass
class FakeSubject:
    label: str
    session_items: list[FakeSession]

    def sessions(self) -> list[FakeSession]:
        return self.session_items


@dataclass
class FakeProject:
    subject_items: list[FakeSubject]

    def __post_init__(self) -> None:
        self.subjects = Finder(self.subject_items)


class FakeClient:
    def __init__(self, project: FakeProject | None) -> None:
        self.project = project
        self.lookups: list[str] = []

    def lookup(self, project_path: str) -> FakeProject | None:
        self.lookups.append(project_path)
        return self.project
