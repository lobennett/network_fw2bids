from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from network_fw2bids.cli import main
from network_fw2bids.errors import PlanningError


class FakeFlywheelBIDS:
    def __init__(self) -> None:
        self.plans = [
            SimpleNamespace(
                acquisition=SimpleNamespace(label="task-flanker_bold"),
                dicom_file=SimpleNamespace(name="scan.dicom.zip"),
                relative_prefix=Path(
                    "sub-s03/ses-01/func/sub-s03_ses-01_task-flanker_run-1_bold"
                ),
            )
        ]
        self.planned_subjects: list[str] = []
        self.convert_calls: list[tuple[str, Path, list[object]]] = []

    def plan_subject(self, subject: str) -> list[object]:
        self.planned_subjects.append(subject)
        return self.plans

    def convert_subject(
        self, subject: str, output: Path, plans: list[object]
    ) -> None:
        self.convert_calls.append((subject, output, plans))


class TestCommandLine(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeFlywheelBIDS()
        self.tokens: list[str] = []

    def factory(self, token: str, **_kwargs: object) -> FakeFlywheelBIDS:
        self.tokens.append(token)
        return self.fake

    def test_dry_run_prints_plan_without_conversion(self) -> None:
        output = StringIO()

        with (
            patch.dict(os.environ, {"FLYWHEEL_API_TOKEN": "test-token"}),
            redirect_stdout(output),
        ):
            result = main(["--subject", "s03"], factory=self.factory)

        self.assertEqual(result, 0)
        self.assertIn("Dry run only", output.getvalue())
        self.assertIn("sub-s03/ses-01/func", output.getvalue())
        self.assertEqual(self.tokens, ["test-token"])
        self.assertEqual(self.fake.planned_subjects, ["s03"])
        self.assertEqual(self.fake.convert_calls, [])

    def test_execute_requires_output(self) -> None:
        with self.assertRaises(SystemExit):
            main(["--subject", "s03", "--execute"], factory=self.factory)

    def test_execute_converts_the_printed_plan_to_requested_output(self) -> None:
        with patch.dict(os.environ, {"FLYWHEEL_API_TOKEN": "test-token"}):
            result = main(
                ["--subject", "s03", "--execute", "--output", "bids"],
                factory=self.factory,
            )

        self.assertEqual(result, 0)
        self.assertEqual(
            self.fake.convert_calls,
            [("s03", Path("bids"), self.fake.plans)],
        )

    def test_missing_token_has_clear_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "FLYWHEEL_API_TOKEN is not set"):
                main(["--subject", "s03"], factory=self.factory)

    def test_domain_error_returns_clear_nonzero_status_without_traceback(self) -> None:
        def failing_factory(_token: str, **_kwargs: object) -> FakeFlywheelBIDS:
            self.fake.plan_subject = lambda _subject: (_ for _ in ()).throw(
                PlanningError("Flywheel subject 's03' was not found")
            )
            return self.fake

        error = StringIO()
        with (
            patch.dict(os.environ, {"FLYWHEEL_API_TOKEN": "test-token"}),
            redirect_stderr(error),
        ):
            result = main(["--subject", "s03"], factory=failing_factory)

        self.assertEqual(result, 1)
        self.assertIn("Flywheel subject 's03' was not found", error.getvalue())
        self.assertNotIn("Traceback", error.getvalue())


if __name__ == "__main__":
    unittest.main()
