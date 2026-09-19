#!/bin/bash
# Submit the 46-subject conversion array and its dependent finalizer.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUBJECTS_FILE="${SUBJECTS_FILE:-$REPO_ROOT/final_sample_subjects.txt}"
: "${BIDS_DIR:?Set BIDS_DIR to the new final BIDS directory}"
FLYWHEEL_ENV_FILE="${FLYWHEEL_ENV_FILE:-$REPO_ROOT/.env}"
PARTS_DIR="${PARTS_DIR:-${BIDS_DIR}.parts}"
LOG_DIR="${LOG_DIR:-${BIDS_DIR}.logs}"
PARTITION="${PARTITION:-russpold,normal}"
THROTTLE="${THROTTLE:-3}"
PROJECT_PATH="${PROJECT_PATH:-russpold/r01network}"

command -v python3 >/dev/null || { echo "python3 is not on PATH" >&2; exit 2; }
canonical_path() {
    python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve())' "$1"
}
paths_overlap() {
    [[ "$1" == "$2" || "$1" == "$2/"* || "$2" == "$1/"* ]]
}

case "$BIDS_DIR" in
    /*) ;;
    *) echo "BIDS_DIR must be an absolute path: $BIDS_DIR" >&2; exit 2 ;;
esac
if [[ -e "$BIDS_DIR" || -L "$BIDS_DIR" ]]; then
    echo "Final BIDS directory already exists: $BIDS_DIR" >&2
    exit 2
fi
if [[ ! -f "$SUBJECTS_FILE" ]]; then
    echo "Subject roster does not exist: $SUBJECTS_FILE" >&2
    exit 2
fi
if [[ ! -f "$FLYWHEEL_ENV_FILE" ]]; then
    echo "Flywheel environment file does not exist: $FLYWHEEL_ENV_FILE" >&2
    exit 2
fi
if [[ ! "$THROTTLE" =~ ^[1-9][0-9]*$ ]]; then
    echo "THROTTLE must be a positive integer: $THROTTLE" >&2
    exit 2
fi
for directory in "$PARTS_DIR" "$LOG_DIR"; do
    if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
        echo "Working path is a symlink or is not a directory: $directory" >&2
        exit 2
    fi
done
bids_canonical="$(canonical_path "$BIDS_DIR")"
parts_canonical="$(canonical_path "$PARTS_DIR")"
logs_canonical="$(canonical_path "$LOG_DIR")"
if paths_overlap "$bids_canonical" "$parts_canonical" \
    || paths_overlap "$bids_canonical" "$logs_canonical" \
    || paths_overlap "$parts_canonical" "$logs_canonical"; then
    echo "BIDS_DIR, PARTS_DIR, and LOG_DIR must be distinct, non-overlapping paths" >&2
    exit 2
fi
if grep -Evq '^s[0-9]+$' "$SUBJECTS_FILE"; then
    echo "Subject roster contains an invalid or blank label" >&2
    exit 2
fi
subject_count="$(awk 'END {print NR}' "$SUBJECTS_FILE")"
if [[ "$subject_count" -ne 46 ]]; then
    echo "Expected 46 subjects, found $subject_count in $SUBJECTS_FILE" >&2
    exit 2
fi
if [[ -n "$(sort "$SUBJECTS_FILE" | uniq -d)" ]]; then
    echo "Subject roster contains duplicate labels" >&2
    exit 2
fi
if [[ -d "$PARTS_DIR" && -n "$(find "$PARTS_DIR" -mindepth 1 -print -quit)" ]]; then
    echo "Parts directory is not empty: $PARTS_DIR" >&2
    exit 2
fi

command -v uv >/dev/null || { echo "uv is not on PATH" >&2; exit 2; }
command -v sbatch >/dev/null || { echo "sbatch is not on PATH" >&2; exit 2; }
mkdir -p "$PARTS_DIR" "$LOG_DIR"

cd "$REPO_ROOT"
uv sync --frozen

export REPO_ROOT SUBJECTS_FILE BIDS_DIR FLYWHEEL_ENV_FILE PARTS_DIR PROJECT_PATH
array_job="$(
    sbatch --parsable \
        --array="0-$((subject_count - 1))%$THROTTLE" \
        --partition="$PARTITION" \
        --output="$LOG_DIR/%x-%A_%a.out" \
        --error="$LOG_DIR/%x-%A_%a.err" \
        --export=ALL \
        "$REPO_ROOT/scripts/convert_subject_array.sbatch"
)"
array_job="${array_job%%;*}"

finalizer_job="$(
    sbatch --parsable \
        --dependency="afterok:$array_job" \
        --partition="$PARTITION" \
        --output="$LOG_DIR/%x-%j.out" \
        --error="$LOG_DIR/%x-%j.err" \
        --export=ALL \
        "$REPO_ROOT/scripts/assemble_subjects.sbatch"
)"
finalizer_job="${finalizer_job%%;*}"

echo "array job: $array_job"
echo "finalizer job: $finalizer_job"
echo "parts: $PARTS_DIR"
echo "final BIDS directory: $BIDS_DIR"
