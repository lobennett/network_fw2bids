"""Native atomic directory publication with no replacement fallback."""

import ctypes
import errno
import os
from pathlib import Path
import platform
import sys

from .errors import ConversionError


def publish_directory(staged: Path, destination: Path) -> None:
    """Rename on the same filesystem, failing atomically if destination exists."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            rename = getattr(libc, "renamex_np", None)
            arguments = (os.fsencode(staged), os.fsencode(destination), 0x00000004)
            argument_types = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        elif sys.platform == "linux":
            rename = getattr(libc, "renameat2", None)
            # AT_FDCWD = -100; RENAME_NOREPLACE = 1.
            arguments = (-100, os.fsencode(staged), -100, os.fsencode(destination), 1)
            argument_types = (
                ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
            )
            return_type = ctypes.c_int
            if rename is None:
                syscall_number = {"x86_64": 316, "aarch64": 276}.get(platform.machine())
                rename = getattr(libc, "syscall", None) if syscall_number is not None else None
                arguments = (syscall_number, *arguments)
                argument_types = (ctypes.c_long, *argument_types)
                return_type = ctypes.c_long
        else:
            rename = None
        if rename is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        # Darwin's flag is RENAME_EXCL. Never fall back to rename/os.replace.
        rename.argtypes = argument_types
        rename.restype = return_type if sys.platform == "linux" else ctypes.c_int
        if rename(*arguments) != 0:
            error = ctypes.get_errno()
            if sys.platform == "linux" and error in {
                errno.EINVAL,
                errno.ENOTSUP,
                errno.EOPNOTSUPP,
            }:
                _publish_with_reservation(staged, destination)
                return
            raise OSError(error, os.strerror(error), str(destination))
    except OSError as exc:
        raise ConversionError(
            f"could not publish BIDS dataset without replacing {destination}: {exc}"
        ) from exc


def _publish_with_reservation(staged: Path, destination: Path) -> None:
    """Reserve an absent destination before a filesystem-compatible rename."""
    destination.mkdir(mode=0o700)
    reservation = destination.lstat()
    try:
        os.rename(staged, destination)
    except OSError:
        try:
            current = destination.lstat()
            if current.st_ino == reservation.st_ino and current.st_dev == reservation.st_dev:
                destination.rmdir()
        except OSError:
            pass
        raise
