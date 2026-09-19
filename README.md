# network_fw2bids

`network-fw2bids` plans and converts one subject from the r01network Flywheel
project into a BIDS dataset. It preserves the study's existing acquisition
mappings, subject aliases, session ordering, session reassignment, and merged
sessions.

## Install and configure

Install the locked project dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

The command reads the Flywheel token only from the `FLYWHEEL_API_TOKEN`
environment variable. For local use, create an ignored `.env` file containing
the variable assignment and keep its token private:

```bash
FLYWHEEL_API_TOKEN=your-token-here
```

Do not commit `.env` or place the token in commands, source files, or logs.

## Plan a subject

Dry run is the default. It queries Flywheel and prints every planned DICOM
archive and its BIDS destination, but does not download or write data:

```bash
uv run --env-file .env network-fw2bids --subject s03
```

The tool supports the current one-subject r01network workflow only. It does
not provide all-subject execution, parallel conversion, resume support, BIDS
validation, or mappings beyond the existing study rules. Use `--project` to
select a different Flywheel group/project path when needed; the default is
`russpold/r01network`.

Unknown acquisition labels stop planning. Review the dry-run listing before
executing it, especially when a subject has unfamiliar acquisitions.

## Convert a subject

Pass `--execute` and a new output directory to download and convert the
planned archives:

```bash
uv run --env-file .env network-fw2bids \
  --subject s03 \
  --execute \
  --output /path/to/new/bids
```

The output directory must not already exist. Conversion stages the complete
subject in a temporary directory and publishes it only after every archive
succeeds. A failed conversion does not publish a partial dataset at the
requested output path.

Atomic publication requires macOS or Linux with filesystem support for an
exclusive rename (`renamex_np` with `RENAME_EXCL` on macOS, `renameat2` with
`RENAME_NOREPLACE` on Linux). Conversion fails if that operation is unavailable
or if the output appears during conversion; it never replaces that path.
Diffusion conversions require readable, nonempty `.bval` and `.bvec` files.

The same CLI is available through Python:

```bash
uv run python -m network_fw2bids --subject s03
```

## Convert the final 46-subject sample on Sherlock

[`final_sample_subjects.txt`](final_sample_subjects.txt) contains the five discovery
subjects and 41 validation subjects in the locked final sample. The Sherlock launcher
submits one array task per subject. Each task writes an isolated dataset under
`<BIDS_DIR>.parts`; a dependent finalizer runs only after all 46 tasks succeed and
atomically publishes the combined directory at `BIDS_DIR`.

On a Sherlock login node, clone this repository on a shared filesystem and keep the
Flywheel token in an ignored environment file. Then submit the workflow:

```bash
cd /path/to/network_fw2bids
export BIDS_DIR="$SCRATCH/network_fw2bids/final/bids"
export FLYWHEEL_ENV_FILE="$HOME/.config/network_fw2bids/.env"
bash scripts/submit_all_subjects.sh
```

The launcher uses the `russpold,normal` partitions and at most three concurrent
Flywheel downloads by default. Override these settings when needed:

```bash
PARTITION=normal THROTTLE=2 bash scripts/submit_all_subjects.sh
```

`BIDS_DIR` must be absolute and must not exist. The parts directory must be empty.
Logs go to `<BIDS_DIR>.logs`, outside the BIDS dataset. Failed array tasks prevent the
finalizer from running, and successful parts remain available for inspection. The
launcher does not resume or overwrite earlier output; choose new paths or deliberately
remove a failed run's parts before resubmitting.

For a retry that preserves the failed run for inspection, keep `BIDS_DIR` unchanged and
choose new `PARTS_DIR` and `LOG_DIR` values before submitting again. The three paths must
be distinct and cannot contain one another.

## Library use

The public library interface is `FlywheelBIDS`:

```python
from network_fw2bids import FlywheelBIDS

converter = FlywheelBIDS.from_token(token)
plans = converter.plan_subject("s03")
converter.convert_subject("s03", output_directory)
```

Supply `token` from a secure environment or secret manager. The plan records
are useful for inspecting the intended archive inputs and BIDS destinations
before conversion.

## Development checks

Run the local verification suite before changing or releasing the package:

```bash
uv lock --check
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q src scripts tests
uv run network-fw2bids --help
uv run python -m network_fw2bids --help
uv build
git diff --check
```
