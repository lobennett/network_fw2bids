from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import Mock

from network_fw2bids import ArchivePlan, FlywheelBIDS, NetworkFW2BIDSError
from tests.fakes import FakeAcquisition, FakeClient, FakeFile, FakeProject, FakeSession, FakeSubject


class TestFlywheelBIDS(unittest.TestCase):
    def setUp(self) -> None:
        self.project_path = "russpold/r01network"
        self.client = FakeClient(
            FakeProject(
                [
                    FakeSubject(
                        "s03",
                        [
                            FakeSession(
                                "100",
                                datetime(2026, 1, 1, 9, 0),
                                [
                                    FakeAcquisition(
                                        "NEW Sag_MPRAGE_T1",
                                        datetime(2026, 1, 1, 9, 1),
                                    )
                                ],
                            )
                        ],
                    )
                ]
            )
        )

    def test_plan_subject_returns_archive_plans_from_composed_planner(self) -> None:
        instance = FlywheelBIDS(self.client, project_path=self.project_path)

        plans = instance.plan_subject("s03")

        self.assertEqual(len(plans), 1)
        self.assertIsInstance(plans[0], ArchivePlan)
        self.assertEqual(self.client.lookups, [self.project_path])

    def test_from_token_constructs_flywheel_client(self) -> None:
        clients = []
        client = object()

        instance = FlywheelBIDS.from_token(
            "token",
            client_factory=lambda token: clients.append(token) or client,
        )

        self.assertIsInstance(instance, FlywheelBIDS)
        self.assertEqual(clients, ["token"])

    def test_convert_reuses_supplied_plan_without_planning(self) -> None:
        client = FakeClient(None)
        plan = ArchivePlan(
            acquisition=Mock(),
            dicom_file=FakeFile(),
            relative_prefix=Path(
                "sub-s03/ses-01/anat/sub-s03_ses-01_acq-SagMPRAGE_run-1_T1w"
            ),
            modality="anat",
        )
        converter = Mock()
        instance = FlywheelBIDS(client, runner=Mock())
        instance._converter = converter

        destination = Path("bids")
        instance.convert_subject("s03", destination, plans=[plan])

        self.assertEqual(client.lookups, [])
        converter.convert.assert_called_once_with([plan], destination, self.project_path)

    def test_convert_subject_delegates_plans_in_planning_order(self) -> None:
        converter = Mock()
        instance = FlywheelBIDS(self.client, runner=Mock())
        instance._converter = converter

        plans = [Mock(spec=ArchivePlan), Mock(spec=ArchivePlan)]
        instance.plan_subject = Mock(return_value=plans)
        destination = Path("bids")

        instance.convert_subject("s03", destination)

        instance.plan_subject.assert_called_once_with("s03")
        converter.convert.assert_called_once_with(plans, destination, self.project_path)


class TestPublicExports(unittest.TestCase):
    def test_public_exports_are_explicit(self) -> None:
        self.assertEqual(
            set(__import__("network_fw2bids").__all__),
            {"ArchivePlan", "FlywheelBIDS", "NetworkFW2BIDSError"},
        )
        self.assertTrue(issubclass(NetworkFW2BIDSError, Exception))


if __name__ == "__main__":
    unittest.main()
