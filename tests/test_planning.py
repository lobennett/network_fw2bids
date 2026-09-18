from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from pathlib import Path
import unittest

from network_fw2bids.errors import PlanningError
from network_fw2bids.planning import ArchivePlan, SubjectPlanner
from tests.fakes import (
    FakeAcquisition,
    FakeClient,
    FakeFile,
    FakeProject,
    FakeSession,
    FakeSubject,
)


class TestSubjectPlanner(unittest.TestCase):
    project_path = "russpold/r01network"

    def setUp(self) -> None:
        self.start = datetime(2026, 1, 1, 9, 0)
        self.early_session = self.session(
            "100",
            1,
            [self.acquisition("NEW Sag_MPRAGE_T1", 1)],
        )
        self.late_session = self.session(
            "200",
            2,
            [self.acquisition("task-flanker_bold", 1)],
        )

    def acquisition(
        self,
        label: str,
        minutes: int,
        files: list[FakeFile] | None = None,
    ) -> FakeAcquisition:
        return FakeAcquisition(
            label,
            self.start + timedelta(minutes=minutes),
            files if files is not None else [FakeFile()],
        )

    def session(
        self, label: str, minutes: int, acquisitions: list[FakeAcquisition]
    ) -> FakeSession:
        return FakeSession(label, self.start + timedelta(minutes=minutes), acquisitions)

    def planner_for_subjects(self, subjects: list[FakeSubject]) -> SubjectPlanner:
        return SubjectPlanner(FakeClient(FakeProject(subjects)), self.project_path)

    def planner_for_sessions(self, sessions: list[FakeSession]) -> SubjectPlanner:
        return self.planner_for_subjects([FakeSubject("s03", sessions)])

    def planner_for_acquisition(self, label: str) -> SubjectPlanner:
        return self.planner_for_sessions([self.session("100", 1, [self.acquisition(label, 1)])])

    @staticmethod
    def source_session_labels(plans: list[ArchivePlan]) -> list[str | None]:
        return [plan.acquisition.source_session_label for plan in plans]

    def test_plans_sessions_chronologically(self) -> None:
        plans = self.planner_for_sessions([self.late_session, self.early_session]).plan(
            "s03"
        )
        self.assertEqual(
            [str(plan.relative_prefix) for plan in plans],
            [
                "sub-s03/ses-01/anat/sub-s03_ses-01_acq-SagMPRAGE_run-1_T1w",
                "sub-s03/ses-02/func/sub-s03_ses-02_task-flanker_run-1_bold",
            ],
        )

    def test_reassigns_22752_from_s03_to_s10(self) -> None:
        reassigned = self.session(
            "22752",
            2,
            [
                self.acquisition("NEW Sag_MPRAGE_T1", 1),
                self.acquisition("T2w CUBE PROMO .8mm sag", 2),
                self.acquisition("DTI_pe0_g105", 3),
                self.acquisition("task-flanker_bold", 4),
                self.acquisition("fmap-fieldmap", 5),
            ],
        )
        self.planner = self.planner_for_subjects(
            [
                FakeSubject("s03", [self.early_session, reassigned]),
                FakeSubject("s10", []),
            ]
        )

        self.assertNotIn("22752", self.source_session_labels(self.planner.plan("s03")))
        self.assertEqual(
            [str(plan.relative_prefix).split("/")[0] for plan in self.planner.plan("s10")],
            ["sub-s10"] * 5,
        )

    def test_unknown_dicom_acquisition_fails_before_download(self) -> None:
        with self.assertRaisesRegex(PlanningError, "no BIDS mapping"):
            self.planner_for_acquisition("unknown-series").plan("s03")

    def test_uses_alias_subject_sessions_with_canonical_destination(self) -> None:
        plans = self.planner_for_subjects(
            [
                FakeSubject(
                    "s19-2",
                    [self.session("101", 1, [self.acquisition("NEW Sag_MPRAGE_T1", 1)])],
                )
            ]
        ).plan("s19")

        self.assertEqual(
            [str(plan.relative_prefix) for plan in plans],
            ["sub-s19/ses-01/anat/sub-s19_ses-01_acq-SagMPRAGE_run-1_T1w"],
        )

    def test_excludes_s29_accession_22424(self) -> None:
        plans = self.planner_for_subjects(
            [
                FakeSubject(
                    "s29",
                    [
                        self.session("22424", 1, [self.acquisition("NEW Sag_MPRAGE_T1", 1)]),
                        self.session("22425", 2, [self.acquisition("task-flanker_bold", 1)]),
                    ],
                )
            ]
        ).plan("s29")

        self.assertEqual(
            [str(plan.relative_prefix) for plan in plans],
            ["sub-s29/ses-01/func/sub-s29_ses-01_task-flanker_run-1_bold"],
        )

    def test_merges_s1258_sessions_under_one_session_number(self) -> None:
        plans = self.planner_for_subjects(
            [
                FakeSubject(
                    "s1258",
                    [
                        self.session("28338", 1, [self.acquisition("NEW Sag_MPRAGE_T1", 1)]),
                        self.session("unknown_2", 2, [self.acquisition("task-flanker_bold", 1)]),
                    ],
                )
            ]
        ).plan("s1258")

        self.assertEqual(
            [str(plan.relative_prefix) for plan in plans],
            [
                "sub-s1258/ses-01/anat/sub-s1258_ses-01_acq-SagMPRAGE_run-1_T1w",
                "sub-s1258/ses-01/func/sub-s1258_ses-01_task-flanker_run-1_bold",
            ],
        )

    def test_skips_unmapped_localizer_series(self) -> None:
        plans = self.planner_for_sessions(
            [
                self.session(
                    "100",
                    1,
                    [
                        self.acquisition("3Plane Loc SSFSE", 1),
                        self.acquisition("NEW Sag_MPRAGE_T1", 2),
                    ],
                )
            ]
        ).plan("s03")

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].modality, "anat")

    def test_rejects_duplicate_normalized_session_labels(self) -> None:
        planner = self.planner_for_sessions(
            [
                self.session("ses-01", 1, [self.acquisition("NEW Sag_MPRAGE_T1", 1)]),
                self.session("01", 2, [self.acquisition("task-flanker_bold", 1)]),
            ]
        )

        with self.assertRaisesRegex(PlanningError, "duplicate session label"):
            planner.plan("s03")

    def test_rejects_multiple_dicom_archives_for_one_acquisition(self) -> None:
        planner = self.planner_for_sessions(
            [
                self.session(
                    "100",
                    1,
                    [
                        self.acquisition(
                            "NEW Sag_MPRAGE_T1",
                            1,
                            [FakeFile("first.zip"), FakeFile("second.zip")],
                        )
                    ],
                )
            ]
        )

        with self.assertRaisesRegex(PlanningError, "expected one DICOM archive"):
            planner.plan("s03")

    def test_archive_plan_is_immutable(self) -> None:
        plan = ArchivePlan(
            acquisition=object(),
            dicom_file=FakeFile(),
            relative_prefix=Path("sub-s03/ses-01/anat/example"),
            modality="anat",
        )

        with self.assertRaises(FrozenInstanceError):
            plan.modality = "func"


if __name__ == "__main__":
    unittest.main()
