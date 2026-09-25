from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from flywheel.rest import ApiException

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

    def test_rejects_unsafe_subject_labels_before_flywheel_lookup(self) -> None:
        for label in ("../escaped", "/absolute", "s03/../../escaped", "s-03", "s_03", "", "é03"):
            with self.subTest(label=label):
                client = FakeClient(FakeProject([FakeSubject(label, [self.early_session])]))
                with self.assertRaisesRegex(PlanningError, "subject label"):
                    SubjectPlanner(client, self.project_path).plan(label)
                self.assertEqual(client.lookups, [])

    def test_wraps_sdk_lookup_and_traversal_failures(self) -> None:
        subject = FakeSubject("s03", [self.early_session])
        project = FakeProject([subject])
        client = FakeClient(project)
        for owner, attribute in (
            (client, "lookup"), (project.subjects, "find_first"),
            (subject, "sessions"), (self.early_session, "acquisitions"),
        ):
            with self.subTest(attribute=attribute):
                failure = ApiException(status=403, reason="forbidden")
                with patch.object(owner, attribute, Mock(side_effect=failure)):
                    with self.assertRaises(PlanningError) as raised:
                        SubjectPlanner(client, self.project_path).plan("s03")
                self.assertIs(raised.exception.__cause__, failure)

    def test_falls_back_to_exact_subject_label_when_sdk_filter_misses(self) -> None:
        subject = FakeSubject("s76", [self.early_session])
        project = FakeProject([subject])
        project.subjects.find_first = Mock(return_value=None)

        plans = SubjectPlanner(FakeClient(project), self.project_path).plan("s76")

        self.assertEqual(len(plans), 1)
        self.assertEqual(str(plans[0].relative_prefix).split("/")[0], "sub-s76")

    def test_unknown_dicom_acquisition_fails_before_download(self) -> None:
        with self.assertRaisesRegex(PlanningError, "no BIDS mapping"):
            self.planner_for_acquisition("unknown-series").plan("s03")

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


def test_selection_inventory_records_skips_and_selected_destination():
    start = datetime(2026, 1, 1)
    rejected = FakeAcquisition('NEW Sag_MPRAGE_T1_qa-reject', start)
    selected = FakeAcquisition('task-goNogo_bold', start)
    rejected.id, selected.id = 'rejected-id', 'selected-id'
    planner = SubjectPlanner(FakeClient(FakeProject([FakeSubject('s03', [FakeSession('100', start, [rejected, selected])])])), 'russpold/r01network')
    plans = planner.plan('s03')
    receipt = planner.selection
    assert receipt['snapshot_kind'] == 'current_inventory'
    assert receipt['subject'] == 's03'
    reject, keep = receipt['acquisitions']
    assert reject['decision'] == 'skipped'
    assert reject['reason'] == 'qa-reject'
    assert reject['acquisition_id'] == 'rejected-id'
    assert keep['decision'] == 'selected'
    assert keep['bids_prefix'] == plans[0].relative_prefix.as_posix()
    assert 'run-1' in keep['bids_prefix']
