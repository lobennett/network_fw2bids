# Modular package design

## Purpose

The current prototype puts study rules, Flywheel traversal, conversion, file placement,
and command-line behavior in one 451-line script. A reader must understand every part
before changing any part. The refactor will give each concern one home while keeping a
single public interface: `FlywheelBIDS`.

The refactor preserves current behavior. It plans or converts one canonical subject,
uses the existing r01network naming rules, defaults to a dry run, refuses unknown
acquisitions, and never merges into an existing output directory.

## Public interface

Library users import one class:

```python
from network_fw2bids import FlywheelBIDS

converter = FlywheelBIDS.from_token(token)
plans = converter.plan_subject("s03")
converter.convert_subject("s03", output_directory)
```

Command-line users run the installed command:

```bash
network-fw2bids --subject s03
network-fw2bids --subject s03 --execute --output /path/to/new/bids
```

`python -m network_fw2bids` will invoke the same command. `scripts/test.py` will remain
as a small compatibility wrapper during development; it will contain no application
logic.

## Modules and boundaries

### `rules.py`

This module owns the r01network policy: allowed tasks, acquisition mappings, skipped
series, subject aliases, session exclusions, session reassignments, and merged
sessions. Its functions accept strings and return plain values. It does not call
Flywheel or touch the filesystem.

### `planning.py`

This module reads Flywheel containers and produces immutable `ArchivePlan` values. It
owns chronological session numbering, canonical-subject resolution, functional run
numbering, and destination prefixes. It does not download data or invoke a converter.

`SubjectPlanner` receives a Flywheel client. This dependency remains injectable so the
planner can be tested without a network connection.

### `conversion.py`

This module turns one `ArchivePlan` into BIDS files. `DicomConverter` downloads the
archive, rejects unsafe ZIP paths, invokes `dcm2niix`, classifies field-map outputs,
writes sidecars, and copies diffusion gradients. It stages the complete subject in a
temporary directory and publishes the result only after every archive succeeds.

The subprocess runner remains injectable. Tests can exercise the complete conversion
flow without invoking a native executable.

### `api.py`

This module defines the public `FlywheelBIDS` façade. It constructs and coordinates a
`SubjectPlanner` and `DicomConverter`. It exposes `from_token`, `plan_subject`, and
`convert_subject`; callers do not need to know about the internal classes.

### `cli.py` and `__main__.py`

The CLI reads `FLYWHEEL_API_TOKEN`, validates arguments, creates `FlywheelBIDS`, and
prints the plan or completion message. Parsing and presentation stay here. Domain
modules raise specific exceptions instead of calling `argparse`, printing, or exiting.

### `__init__.py`

The package root exports `FlywheelBIDS` and `ArchivePlan`. Other classes remain
internal implementation details.

## Data flow

1. The CLI or a library caller creates `FlywheelBIDS`.
2. `SubjectPlanner` resolves canonical subject records and returns `ArchivePlan`
   objects.
3. A dry run prints those plans and stops.
4. An executing run passes the plans to `DicomConverter`.
5. `DicomConverter` downloads and converts each archive inside a temporary workspace.
6. The converter publishes the completed BIDS directory with one atomic rename.

The plans are the boundary between network discovery and filesystem mutation. A plan
contains the source acquisition, DICOM file, BIDS prefix, modality, and task. Neither
side needs to understand the other's implementation.

## Errors and safety

- Missing projects, subjects, mappings, sidecars, or converter outputs raise clear
  domain errors.
- Unknown acquisition labels stop planning before any download begins.
- Dry run remains the default.
- `--execute` requires a new output path.
- Conversion refuses existing files and directories.
- ZIP entries cannot escape the temporary extraction directory.
- Failed conversions leave no partial BIDS dataset at the requested destination.
- Tokens come only from `FLYWHEEL_API_TOKEN`; `.env` remains ignored.

## Tests

Tests will import package modules directly rather than execute a large script through
`runpy`.

- `test_rules.py` covers pure acquisition and subject rules.
- `test_planning.py` covers session order, reassignment, merges, skips, and BIDS paths.
- `test_conversion.py` covers download, extraction, functional sidecars, field maps,
  diffusion companions, and atomic output behavior.
- `test_cli.py` covers dry-run defaults, required execution arguments, and token
  handling.

Shared fake Flywheel containers will live in `tests/fakes.py`. Tests will continue to
use the standard library's `unittest`; the refactor adds no testing dependency.

## Scope

This change restructures the tested one-subject workflow. It does not add all-subject
execution, parallel downloads, resumability, BIDS validation, or new acquisition
mappings. The new boundaries make those features possible without enlarging the public
interface or coupling them to the converter.
