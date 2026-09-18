# Modular Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 451-line prototype with a readable package whose single public class plans and converts one Flywheel subject to BIDS.

**Architecture:** A `FlywheelBIDS` façade composes a read-only `SubjectPlanner` with a filesystem-writing `DicomConverter`. Pure study rules live separately from Flywheel access, conversion, and CLI presentation, so each unit can be understood and tested alone.

**Tech Stack:** Python 3.11+, uv, Flywheel SDK 22.4+, dcm2niix 1.0.20260724+, Hatchling, standard-library `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-18-modular-package-design.md`

## Global Constraints

- Keep `FlywheelBIDS` as the only main public class; export it and `ArchivePlan` from `network_fw2bids`.
- Preserve one-subject planning and conversion behavior, including aliases, session reassignments, exclusions, and merges.
- Preserve dry run as the CLI default; require both `--execute` and `--output` before downloading.
- Refuse unknown acquisition labels and existing output paths.
- Stage a complete subject and publish it with one atomic rename.
- Read credentials only from `FLYWHEEL_API_TOKEN`; never read or print `.env` contents.
- Keep tests on standard-library `unittest`; add no testing dependency.
- Do not add all-subject execution, parallelism, resumability, BIDS validation, or new acquisition mappings.

---

### Task 1: Pure study rules and domain errors

**Files:**
- Create: `src/network_fw2bids/errors.py`
- Create: `src/network_fw2bids/rules.py`
- Create: `tests/test_rules.py`

**Interfaces:**
- Produces: `PlanningError`, `ConversionError`.
- Produces: immutable `AcquisitionRule(modality, suffix=None, task=None, acquisition=None, direction=None)`.
- Produces: `map_acquisition(label: str) -> AcquisitionRule | None`.
- Produces: `relevant_subject_labels(canonical: str) -> set[str]` and `normalize_label(label: str) -> str`.

- [ ] **Step 1: Write failing rule tests**

```python
import unittest

from network_fw2bids.rules import (
    AcquisitionRule,
    map_acquisition,
    normalize_label,
    relevant_subject_labels,
)


class TestRules(unittest.TestCase):
    def test_maps_functional_acquisition(self) -> None:
        self.assertEqual(
            map_acquisition("task-flanker_bold"),
            AcquisitionRule(modality="func", suffix="bold", task="flanker"),
        )

    def test_maps_diffusion_acquisition(self) -> None:
        self.assertEqual(
            map_acquisition("DTI_pe0_g105"),
            AcquisitionRule(
                modality="dwi", suffix="dwi", acquisition="g105", direction="AP"
            ),
        )

    def test_skips_localizer_and_rejected_scan(self) -> None:
        self.assertIsNone(map_acquisition("3Plane Loc SSFSE"))
        self.assertIsNone(map_acquisition("task-flanker_bold_qa-reject"))

    def test_finds_aliases_and_reassignment_sources(self) -> None:
        self.assertEqual(relevant_subject_labels("s10"), {"s10", "s03"})
        self.assertEqual(normalize_label("ses-01"), "01")
```

- [ ] **Step 2: Run the rule tests and verify the module imports fail**

Run: `uv run python -m unittest tests.test_rules -v`

Expected: `ModuleNotFoundError: No module named 'network_fw2bids.rules'`.

- [ ] **Step 3: Implement the domain errors and pure rules**

```python
# errors.py
class NetworkFW2BIDSError(Exception):
    """Base error for predictable package failures."""


class PlanningError(NetworkFW2BIDSError):
    """Flywheel contents cannot produce an unambiguous plan."""


class ConversionError(NetworkFW2BIDSError):
    """A planned archive cannot be converted safely."""
```

```python
# rules.py
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class AcquisitionRule:
    modality: str
    suffix: str | None = None
    task: str | None = None
    acquisition: str | None = None
    direction: str | None = None


def map_acquisition(label: str) -> AcquisitionRule | None:
    if label in SKIP_ACQUISITIONS or label.endswith("_qa-reject"):
        return None
    match = FUNCTIONAL.fullmatch(label)
    if match and match["task"] in TASKS:
        return AcquisitionRule(modality="func", suffix="bold", task=match["task"])
    return NON_FUNCTIONAL.get(label)


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
```

Move the exact task allowlist, acquisition mappings, skip set, subject aliases,
session overrides, and session merges from `scripts/test.py` into `rules.py`. Store
nonfunctional mappings as `AcquisitionRule` values.

- [ ] **Step 4: Run the rule tests**

Run: `uv run python -m unittest tests.test_rules -v`

Expected: four tests pass.

- [ ] **Step 5: Commit the pure rules**

```bash
git add src/network_fw2bids/errors.py src/network_fw2bids/rules.py tests/test_rules.py
git commit -m "Refactor study rules into pure module"
```

---

### Task 2: Flywheel planning

**Files:**
- Create: `src/network_fw2bids/planning.py`
- Create: `tests/fakes.py`
- Create: `tests/test_planning.py`

**Interfaces:**
- Consumes: rules and `PlanningError` from Task 1.
- Produces: immutable `ArchivePlan(acquisition, dicom_file, relative_prefix, modality, task=None)`.
- Produces: `SubjectPlanner(client, project_path).plan(subject_label) -> list[ArchivePlan]`.

- [ ] **Step 1: Add reusable fake Flywheel containers**

```python
# tests/fakes.py
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


@dataclass
class FakeSession:
    label: str
    timestamp: datetime
    acquisition_items: list[FakeAcquisition]

    def acquisitions(self) -> list[FakeAcquisition]:
        return self.acquisition_items
```

- [ ] **Step 2: Write failing planner tests**

Create tests for these exact behaviors in `tests/test_planning.py`:

```python
def test_plans_sessions_chronologically(self) -> None:
    plans = self.planner_for_sessions([self.late_session, self.early_session]).plan("s03")
    self.assertEqual(
        [str(plan.relative_prefix) for plan in plans],
        [
            "sub-s03/ses-01/anat/sub-s03_ses-01_acq-SagMPRAGE_run-1_T1w",
            "sub-s03/ses-02/func/sub-s03_ses-02_task-flanker_run-1_bold",
        ],
    )


def test_reassigns_22752_from_s03_to_s10(self) -> None:
    self.assertNotIn("22752", self.source_session_labels(self.planner.plan("s03")))
    self.assertEqual(
        [str(plan.relative_prefix).split("/")[0] for plan in self.planner.plan("s10")],
        ["sub-s10"] * 5,
    )


def test_unknown_dicom_acquisition_fails_before_download(self) -> None:
    with self.assertRaisesRegex(PlanningError, "no BIDS mapping"):
        self.planner_for_acquisition("unknown-series").plan("s03")
```

Also cover alias subjects, excluded accession `22424`, merged sessions for `s1258`,
skipped acquisitions, duplicate session labels, and more than one DICOM archive.

- [ ] **Step 3: Run planner tests and verify failure**

Run: `uv run python -m unittest tests.test_planning -v`

Expected: import failure for `network_fw2bids.planning`.

- [ ] **Step 4: Implement `ArchivePlan` and `SubjectPlanner`**

```python
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
        project = self._client.lookup(self._project_path)
        if project is None:
            raise PlanningError(f"Flywheel project {self._project_path!r} was not found")
        sessions = self._canonical_sessions(project, subject_label)
        session_numbers = self._number_sessions(sessions, subject_label)
        return self._build_plans(subject_label, sessions, session_numbers)
```

Extract `_canonical_sessions`, `_number_sessions`, `_build_plans`, `_build_prefix`,
and `_timestamp_key` as short private methods. Each method should perform one of the
named operations and raise `PlanningError` with the messages already tested. Keep all
Flywheel reads in this module.

- [ ] **Step 5: Run rules and planner tests**

Run: `uv run python -m unittest tests.test_rules tests.test_planning -v`

Expected: all tests pass.

- [ ] **Step 6: Commit planning**

```bash
git add src/network_fw2bids/planning.py tests/fakes.py tests/test_planning.py
git commit -m "Extract Flywheel subject planning"
```

---

### Task 3: DICOM conversion and atomic publication

**Files:**
- Create: `src/network_fw2bids/conversion.py`
- Create: `tests/test_conversion.py`

**Interfaces:**
- Consumes: `ArchivePlan` and `ConversionError`.
- Produces: `DicomConverter(runner=subprocess.run)`.
- Produces: `convert(plans: Sequence[ArchivePlan], destination: Path, dataset_name: str) -> None`.

- [ ] **Step 1: Write failing conversion tests**

Use a fake acquisition whose `download_file` writes a small ZIP. Inject a runner that
writes synthetic dcm2niix outputs into the `-o` directory.

```python
def test_converts_functional_archive_and_adds_task_name(self) -> None:
    self.converter.convert([self.functional_plan], self.destination, "r01network")
    prefix = self.destination / self.functional_plan.relative_prefix
    self.assertEqual(Path(f"{prefix}.nii.gz").read_bytes(), b"nifti")
    self.assertEqual(json.loads(prefix.with_suffix(".json").read_text())["TaskName"], "flanker")


def test_places_fieldmap_outputs_and_units(self) -> None:
    self.converter.convert([self.fieldmap_plan], self.destination, "r01network")
    fieldmap = self.destination / "sub-s03/ses-01/fmap/sub-s03_ses-01_run-1_fieldmap.json"
    self.assertEqual(json.loads(fieldmap.read_text())["Units"], "Hz")


def test_rejects_zip_path_traversal(self) -> None:
    self.archive_writer = lambda path: self.write_zip(path, {"../escape.dcm": b"bad"})
    with self.assertRaisesRegex(ConversionError, "unsafe path"):
        self.converter.convert([self.functional_plan], self.destination, "r01network")


def test_failure_leaves_no_destination(self) -> None:
    self.runner = Mock(side_effect=CalledProcessError(1, ["dcm2niix"]))
    with self.assertRaises(ConversionError):
        self.converter.convert([self.functional_plan], self.destination, "r01network")
    self.assertFalse(self.destination.exists())
```

Also test DWI `.bval` and `.bvec` companions, a missing JSON sidecar, multiple
non-fieldmap images, missing magnitude or phase output, duplicate roles, and refusal
to overwrite an existing destination.

- [ ] **Step 2: Run conversion tests and verify failure**

Run: `uv run python -m unittest tests.test_conversion -v`

Expected: import failure for `network_fw2bids.conversion`.

- [ ] **Step 3: Implement `DicomConverter`**

```python
class DicomConverter:
    def __init__(self, runner: Callable[..., CompletedProcess] = subprocess.run) -> None:
        self._runner = runner

    def convert(
        self,
        plans: Sequence[ArchivePlan],
        destination: Path,
        dataset_name: str,
    ) -> None:
        destination = Path(destination)
        self._require_new_destination(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".network-fw2bids-", dir=destination.parent) as scratch:
            staged = Path(scratch) / "bids"
            staged.mkdir()
            self._write_dataset_description(staged, dataset_name)
            for index, plan in enumerate(plans):
                self._convert_archive(plan, staged, Path(scratch) / f"archive-{index}")
            os.replace(staged, destination)
```

Extract private methods for `_require_new_destination`, `_write_dataset_description`,
`_convert_archive`, `_download`, `_extract`, `_run_dcm2niix`,
`_place_converted_images`, `_classify_fieldmap`, and `_place_image`. Wrap ZIP,
subprocess, JSON, and output-shape failures in `ConversionError` while retaining the
original exception as `__cause__`.

- [ ] **Step 4: Run conversion tests**

Run: `uv run python -m unittest tests.test_conversion -v`

Expected: all conversion tests pass.

- [ ] **Step 5: Commit conversion**

```bash
git add src/network_fw2bids/conversion.py tests/test_conversion.py
git commit -m "Extract atomic DICOM conversion"
```

---

### Task 4: Public façade

**Files:**
- Create: `src/network_fw2bids/api.py`
- Modify: `src/network_fw2bids/__init__.py`
- Create: `tests/test_api.py`

**Interfaces:**
- Consumes: `SubjectPlanner`, `DicomConverter`, and `ArchivePlan`.
- Produces: `FlywheelBIDS(client, project_path="russpold/r01network", runner=subprocess.run)`.
- Produces: `FlywheelBIDS.from_token(token, project_path=...)`.
- Produces: `plan_subject(subject_label)` and `convert_subject(subject_label, destination, plans=None)`.

- [ ] **Step 1: Write failing façade tests**

```python
from network_fw2bids import ArchivePlan, FlywheelBIDS


def test_from_token_constructs_flywheel_client(self) -> None:
    clients = []
    instance = FlywheelBIDS.from_token(
        "token", client_factory=lambda token: clients.append(token) or self.client
    )
    self.assertIsInstance(instance, FlywheelBIDS)
    self.assertEqual(clients, ["token"])


def test_convert_reuses_supplied_plan(self) -> None:
    instance = FlywheelBIDS(self.client, runner=self.runner)
    instance.convert_subject("s03", self.destination, plans=[self.plan])
    self.assertEqual(self.project_lookup_count, 0)
```

- [ ] **Step 2: Run façade tests and verify failure**

Run: `uv run python -m unittest tests.test_api -v`

Expected: import failure because the package does not export `FlywheelBIDS`.

- [ ] **Step 3: Implement the façade and exports**

```python
class FlywheelBIDS:
    def __init__(self, client: Any, project_path: str = DEFAULT_PROJECT, runner=subprocess.run) -> None:
        self._project_path = project_path
        self._planner = SubjectPlanner(client, project_path)
        self._converter = DicomConverter(runner)

    @classmethod
    def from_token(cls, token: str, project_path: str = DEFAULT_PROJECT, client_factory=flywheel.Client):
        return cls(client_factory(token), project_path)

    def plan_subject(self, subject_label: str) -> list[ArchivePlan]:
        return self._planner.plan(subject_label)

    def convert_subject(self, subject_label: str, destination: Path, plans=None) -> None:
        resolved = plans if plans is not None else self.plan_subject(subject_label)
        self._converter.convert(resolved, destination, self._project_path)
```

Export only `ArchivePlan`, `FlywheelBIDS`, and the base package error from
`__init__.py`; define `__all__` explicitly.

- [ ] **Step 4: Run API and lower-level tests**

Run: `uv run python -m unittest tests.test_rules tests.test_planning tests.test_conversion tests.test_api -v`

Expected: all tests pass.

- [ ] **Step 5: Commit the façade**

```bash
git add src/network_fw2bids/api.py src/network_fw2bids/__init__.py tests/test_api.py
git commit -m "Add single FlywheelBIDS public interface"
```

---

### Task 5: Installed command and thin compatibility wrapper

**Files:**
- Create: `src/network_fw2bids/cli.py`
- Create: `src/network_fw2bids/__main__.py`
- Modify: `scripts/test.py`
- Modify: `pyproject.toml`
- Create: `tests/test_cli.py`
- Delete: `tests/test_script.py`

**Interfaces:**
- Consumes: public `FlywheelBIDS` façade.
- Produces: `main(argv: list[str] | None = None, factory=FlywheelBIDS.from_token) -> int`.
- Produces: installed `network-fw2bids` command.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_dry_run_prints_plan_without_conversion(self) -> None:
    output = StringIO()
    with patch.dict(os.environ, {"FLYWHEEL_API_TOKEN": "test-token"}), redirect_stdout(output):
        result = main(["--subject", "s03"], factory=self.factory)
    self.assertEqual(result, 0)
    self.assertIn("Dry run only", output.getvalue())
    self.assertEqual(self.fake.convert_calls, [])


def test_execute_requires_output(self) -> None:
    with self.assertRaises(SystemExit):
        main(["--subject", "s03", "--execute"], factory=self.factory)


def test_missing_token_has_clear_error(self) -> None:
    with patch.dict(os.environ, {}, clear=True):
        with self.assertRaisesRegex(SystemExit, "FLYWHEEL_API_TOKEN is not set"):
            main(["--subject", "s03"], factory=self.factory)
```

- [ ] **Step 2: Run CLI tests and verify failure**

Run: `uv run python -m unittest tests.test_cli -v`

Expected: import failure for `network_fw2bids.cli`.

- [ ] **Step 3: Implement CLI and entry points**

Move argument parsing and output formatting from the prototype into `cli.py`. Keep the
CLI orchestration under 80 lines. Add:

```toml
[project.scripts]
network-fw2bids = "network_fw2bids.cli:main"
```

Use the same two-line entry point in `__main__.py` and `scripts/test.py`:

```python
from network_fw2bids.cli import main

raise SystemExit(main())
```

- [ ] **Step 4: Remove the monolithic script test and run all tests**

Run: `uv run python -m unittest discover -s tests -v`

Expected: all new module-level tests pass; no test imports `scripts/test.py`.

- [ ] **Step 5: Verify both command forms**

Run: `uv run network-fw2bids --help`

Expected: help lists `--subject`, `--project`, `--output`, and `--execute`.

Run: `uv run python -m network_fw2bids --help`

Expected: identical options.

- [ ] **Step 6: Commit the CLI**

```bash
git add pyproject.toml uv.lock src/network_fw2bids/cli.py src/network_fw2bids/__main__.py scripts/test.py tests/test_cli.py
git commit -m "Add network-fw2bids command"
```

---

### Task 6: Documentation and end-to-end verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Documents: uv setup, `.env` usage, dry run, execution, library API, and current one-subject scope.

- [ ] **Step 1: Replace the minimal README with runnable examples**

Document these commands verbatim:

```bash
uv sync
uv run --env-file .env network-fw2bids --subject s03
uv run --env-file .env network-fw2bids \
  --subject s03 \
  --execute \
  --output /path/to/new/bids
```

State that output must not exist, dry run is the default, unknown acquisitions stop
the plan, and a failed conversion does not publish a partial dataset. Include the
three-line library example from the spec.

- [ ] **Step 2: Run the full local verification suite**

Run:

```bash
uv lock --check
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src scripts tests
uv run network-fw2bids --help
uv run python -m network_fw2bids --help
uv build
git diff --check
```

Expected: every command exits zero; the test output contains no failures or errors.

- [ ] **Step 3: Run live read-only dry runs**

Run:

```bash
uv run --env-file .env network-fw2bids --subject s03
uv run --env-file .env network-fw2bids --subject s10
```

Expected: `s03` plans 63 archives, `s10` plans five archives, accession `22752` appears
only under `sub-s10`, and neither command downloads files.

- [ ] **Step 4: Confirm credentials and generated files remain untracked**

Run:

```bash
git check-ignore -v .env
git status --short
```

Expected: `.env` is ignored; `.venv`, `dist`, and Python caches do not appear.

- [ ] **Step 5: Commit documentation**

```bash
git add .gitignore README.md
git commit -m "Document modular Flywheel to BIDS interface"
```
