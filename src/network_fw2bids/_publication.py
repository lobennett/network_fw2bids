"""Native atomic directory publication with no replacement fallback."""

import ctypes
import errno
import os
from pathlib import Path
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
        else:
            rename = None
        if rename is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename is unavailable")
        # Darwin's flag is RENAME_EXCL. Never fall back to rename/os.replace.
        rename.argtypes = argument_types
        rename.restype = ctypes.c_int
        if rename(*arguments) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
    except OSError as exc:
        raise ConversionError(
            f"could not publish BIDS dataset without replacing {destination}: {exc}"
        ) from exc
