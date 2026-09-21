"""Node-local containment for transient identifiable conversion material."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import signal
from tempfile import TemporaryDirectory

from .errors import ConversionError


_SAFE_JOB_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_TEMPORARY_DIRECTORY_SUFFIX = re.compile(r"[a-z0-9_]{8}\Z")


class _SensitiveWorkspaceInterrupted(BaseException):
    """Cause context cleanup before propagating a termination signal."""


def _validated_job_value(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None:
        return None
    if not _SAFE_JOB_VALUE.fullmatch(value):
        raise ConversionError(f"{name} is unsafe")
    return value


def _workspace_prefix(environ: Mapping[str, str]) -> str | None:
    job_id = _validated_job_value(environ, "SLURM_JOB_ID")
    task_id = _validated_job_value(environ, "SLURM_ARRAY_TASK_ID")
    values = tuple(value for value in (job_id, task_id) if value is not None)
    if not values:
        return None
    return "network-fw2bids-sensitive-" + "-".join(values) + "-"


def _remove_stale_workspace(root: Path, prefix: str | None) -> None:
    if prefix is None:
        return
    try:
        candidates = tuple(root.iterdir())
    except OSError as exc:
        raise ConversionError(f"could not inspect prior sensitive workspaces: {root}") from exc
    for stale in candidates:
        suffix = stale.name.removeprefix(prefix)
        if stale.name == prefix or not _TEMPORARY_DIRECTORY_SUFFIX.fullmatch(suffix):
            continue
        try:
            info = stale.lstat()
        except OSError as exc:
            raise ConversionError(f"could not inspect prior sensitive workspace: {stale}") from exc
        if stale.is_symlink() or not stale.is_dir() or info.st_uid != os.getuid():
            raise ConversionError(f"prior sensitive workspace is unsafe: {stale}")
        try:
            shutil.rmtree(stale)
        except OSError as exc:
            raise ConversionError(f"could not remove prior sensitive workspace: {stale}") from exc


def _install_cleanup_handlers() -> dict[int, signal.Handlers]:
    def interrupt(_signum: int, _frame: object) -> None:
        raise _SensitiveWorkspaceInterrupted()

    previous: dict[int, signal.Handlers] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, interrupt)
    except (ValueError, OSError) as exc:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        raise ConversionError("could not install sensitive workspace cleanup handlers") from exc
    return previous


def _restore_cleanup_handlers(previous: Mapping[int, signal.Handlers]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


@contextmanager
def sensitive_workspace(environ: Mapping[str, str] = os.environ) -> Iterator[Path]:
    """Yield a private, job-local directory and remove it on every exit path."""
    raw = environ.get("SLURM_TMPDIR")
    if not raw:
        raise ConversionError("SLURM_TMPDIR is required for sensitive conversion")
    if "\0" in raw or "\n" in raw or "\r" in raw:
        raise ConversionError("SLURM_TMPDIR is missing or unsafe")
    root = Path(raw)
    if root.is_symlink() or not root.is_dir() or not os.access(root, os.W_OK | os.X_OK):
        raise ConversionError(f"SLURM_TMPDIR is missing or unsafe: {root}")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise ConversionError(f"SLURM_TMPDIR is missing or unsafe: {root}") from exc
    if resolved == Path(resolved.anchor):
        raise ConversionError("SLURM_TMPDIR cannot be a filesystem root")

    job_prefix = _workspace_prefix(environ)
    _remove_stale_workspace(resolved, job_prefix)
    prefix = job_prefix or "network-fw2bids-sensitive-"
    workspace: Path | None = None
    try:
        with TemporaryDirectory(prefix=prefix, dir=resolved) as name:
            workspace = Path(name)
            try:
                workspace.chmod(0o700)
                if workspace.resolve().parent != resolved:
                    raise ConversionError("sensitive workspace resolves outside SLURM_TMPDIR")
            except OSError as exc:
                raise ConversionError("could not secure sensitive workspace") from exc
            previous = _install_cleanup_handlers()
            try:
                yield workspace
            finally:
                _restore_cleanup_handlers(previous)
    except _SensitiveWorkspaceInterrupted as exc:
        raise ConversionError("sensitive workspace interrupted and cleaned up") from exc
    if workspace is not None and workspace.exists():
        raise ConversionError("sensitive workspace cleanup did not complete")
