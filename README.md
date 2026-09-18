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

The same CLI is available through Python:

```bash
uv run python -m network_fw2bids --subject s03
```

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
