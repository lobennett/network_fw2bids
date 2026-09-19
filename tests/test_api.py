from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
import zipfile

from flywheel.rest import ApiException

from network_fw2bids import ArchivePlan, FlywheelBIDS, NetworkFW2BIDSError
from network_fw2bids.errors import ConversionError, PlanningError
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

    def test_from_token_wraps_sdk_authentication_failure(self) -> None:
        failure = ApiException(status=401, reason="unauthorized")
        with self.assertRaises(PlanningError) as raised:
            FlywheelBIDS.from_token("token", client_factory=Mock(side_effect=failure))
        self.assertIs(raised.exception.__cause__, failure)

    def test_supplied_plan_paths_cannot_escape_the_staged_dataset(self) -> None:
        for kind in ("absolute", "parent"):
            with self.subTest(kind=kind), TemporaryDirectory() as scratch:
                root = Path(scratch)
                downloads = []

                def download(name: str, destination: str) -> None:
                    downloads.append(name)
                    with zipfile.ZipFile(destination, "w") as archive:
                        archive.writestr("scan.dcm", b"dicom")

                def runner(command: list[str], **kwargs) -> None:
                    directory = Path(command[command.index("-o") + 1])
                    (directory / "converted.nii.gz").write_bytes(b"nifti")
                    (directory / "converted.json").write_text("{}")

                prefix = root / "escaped/scan" if kind == "absolute" else Path("../../escaped/scan")
                plan = ArchivePlan(
                    acquisition=Mock(label="test", download_file=download),
                    dicom_file=FakeFile(), relative_prefix=prefix, modality="func",
                )
                instance = FlywheelBIDS(FakeClient(None), runner=runner)
                with self.assertRaisesRegex(ConversionError, "unsafe.*prefix"):
                    instance.convert_subject("s03", root / "bids", plans=[plan])
                self.assertEqual(downloads, [])
                self.assertEqual(list(root.iterdir()), [])

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
