#!/bin/bash
#SBATCH --job-name=fw2bids
#SBATCH --array=0-45%3
#SBATCH --partition=russpold,normal
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=04:00:00
#SBATCH --output=network-fw2bids-%A_%a.out
#SBATCH --error=network-fw2bids-%A_%a.err

set -euo pipefail

: "${SUBJECTS_FILE:?SUBJECTS_FILE is required}"
: "${PARTS_DIR:?PARTS_DIR is required}"
: "${FLYWHEEL_ENV_FILE:?FLYWHEEL_ENV_FILE is required}"
: "${SLURM_ARRAY_TASK_ID:?SLURM_ARRAY_TASK_ID is required}"

subject="$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$SUBJECTS_FILE")"
if [[ ! "$subject" =~ ^s[0-9]+$ ]]; then
    echo "Invalid or missing subject for array index $SLURM_ARRAY_TASK_ID" >&2
    exit 2
fi

set -a
source "$FLYWHEEL_ENV_FILE"
set +a
: "${FLYWHEEL_API_TOKEN:?FLYWHEEL_API_TOKEN is not set by FLYWHEEL_ENV_FILE}"

converter="${NETWORK_FW2BIDS_BIN:-${REPO_ROOT:?REPO_ROOT is required}/.venv/bin/network-fw2bids}"
if [[ ! -x "$converter" ]]; then
    echo "network-fw2bids is not executable: $converter" >&2
    exit 2
fi

part="$PARTS_DIR/$subject"
if [[ -e "$part" || -L "$part" ]]; then
    echo "Subject output already exists: $part" >&2
    exit 2
fi

"$converter" \
    --project "${PROJECT_PATH:-russpold/r01network}" \
    --subject "$subject" \
    --execute \
    --output "$part"
