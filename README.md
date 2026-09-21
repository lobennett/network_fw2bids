# network_fw2bids

`network-fw2bids` downloads one r01network subject from Flywheel and converts it to
BIDS using the study's fixed acquisition and session mappings.

## Setup

```bash
uv sync
```

Set `FLYWHEEL_API_TOKEN` in the environment or an ignored `.env` file.

## Convert one subject

Planning is the default and does not download data:

```bash
uv run --env-file .env network-fw2bids --subject s03
```

Review the plan, then run the conversion with the pinned PyDeface image:

```bash
export PYDEFACE_IMAGE=/shared/containers/pydeface-2.1.0-fsl-6.0.7.18.sif
export PYDEFACE_SHA256="$(sha256sum "$PYDEFACE_IMAGE" | awk '{print $1}')"

uv run --env-file .env network-fw2bids --subject s03 --execute \
  --output /path/to/new/subject-part \
  --pydeface-image "$PYDEFACE_IMAGE" \
  --pydeface-version 2.1.0 \
  --pydeface-sha256 "$PYDEFACE_SHA256"
```

The output path must not exist. The default project is `russpold/r01network`; use
`--project` to select another.

### Anatomical privacy

DICOMs, undefaced NIfTIs, and PyDeface files may exist only in `$SLURM_TMPDIR`.
Persistent output contains defaced anatomy, `Defaced: true` sidecars, and a checksum
receipt under `code/network_fw2bids/defacing/`. Inspect pilot T1w and T2w images before
processing the full sample.

## Convert all 46 subjects on Sherlock

[`final_sample_subjects.txt`](final_sample_subjects.txt) defines the sample. The launcher
creates one combined BIDS dataset after every array task succeeds.

```bash
export BIDS_DIR="$SCRATCH/network_fw2bids/final/bids"
export FLYWHEEL_ENV_FILE="$HOME/.config/network_fw2bids/.env"
bash scripts/submit_all_subjects.sh
```

`BIDS_DIR` must be an absolute, nonexistent path. Subject parts and logs are written to
`<BIDS_DIR>.parts` and `<BIDS_DIR>.logs`. Set `PARTITION` or `THROTTLE` to override the
launcher defaults.

## Development

```bash
uv lock --check
uv run pytest -q
uv build
git diff --check
```
