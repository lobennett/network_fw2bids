#!/bin/bash
#SBATCH --job-name=fw2bids-finalize
#SBATCH --partition=russpold,normal
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=02:00:00
#SBATCH --output=network-fw2bids-finalize-%j.out
#SBATCH --error=network-fw2bids-finalize-%j.err

set -euo pipefail

: "${REPO_ROOT:?REPO_ROOT is required}"
: "${SUBJECTS_FILE:?SUBJECTS_FILE is required}"
: "${PARTS_DIR:?PARTS_DIR is required}"
: "${BIDS_DIR:?BIDS_DIR is required}"

python_bin="${PYTHON_BIN:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
    echo "Python is not executable: $python_bin" >&2
    exit 2
fi

cd "$REPO_ROOT"
"$python_bin" -m network_fw2bids._assembly \
    --subjects "$SUBJECTS_FILE" \
    --parts "$PARTS_DIR" \
    --output "$BIDS_DIR"
