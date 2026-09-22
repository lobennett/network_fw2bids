import ctypes
import errno
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import network_fw2bids._publication as publication
from network_fw2bids._publication import publish_directory
from network_fw2bids.errors import ConversionError


class NativeCall:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments: object) -> int:
        self.calls.append(arguments)
        return 0


class OldLinuxLibc:
    def __init__(self) -> None:
        self.syscall = NativeCall()


class TestAtomicPublication(unittest.TestCase):
    def test_linux_uses_syscall_when_libc_has_no_renameat2_wrapper(self) -> None:
        libc = OldLinuxLibc()
        source = Path("/staged")
        destination = Path("/destination")
        with (
            patch.object(publication.ctypes, "CDLL", return_value=libc),
            patch.object(publication.sys, "platform", "linux"),
            patch.object(publication.platform, "machine", return_value="x86_64"),
        ):
            publish_directory(source, destination)

        self.assertEqual(
            libc.syscall.calls,
            [(316, -100, bytes(source), -100, bytes(destination), 1)],
        )
        self.assertIs(libc.syscall.restype, ctypes.c_long)

    def test_success_moves_the_complete_directory(self) -> None:
        with TemporaryDirectory() as scratch:
            staged = Path(scratch) / "staged"
            staged.mkdir()
            (staged / "complete").write_bytes(b"dataset")
            inode = staged.stat().st_ino
            destination = Path(scratch) / "bids"

            publish_directory(staged, destination)

            self.assertFalse(staged.exists())
            self.assertEqual(destination.stat().st_ino, inode)
            self.assertEqual((destination / "complete").read_bytes(), b"dataset")

    def test_existing_entries_are_never_replaced(self) -> None:
        for kind in ("empty directory", "populated directory", "file", "dangling symlink"):
            with self.subTest(kind=kind), TemporaryDirectory() as scratch:
                root = Path(scratch)
                staged = root / "staged"
                staged.mkdir()
                (staged / "complete").write_bytes(b"dataset")
                destination = root / "bids"
                if kind.endswith("directory"):
                    destination.mkdir(mode=0o700)
                    if kind == "populated directory":
                        (destination / "original").write_bytes(b"keep me")
                elif kind == "file":
                    destination.write_bytes(b"keep me")
                else:
                    destination.symlink_to(root / "missing")
                before = destination.lstat()

                with self.assertRaises(ConversionError) as raised:
                    publish_directory(staged, destination)

                self.assertIsInstance(raised.exception.__cause__, FileExistsError)
                self.assertEqual(destination.lstat().st_ino, before.st_ino)
                self.assertEqual(destination.lstat().st_mode, before.st_mode)
                self.assertEqual((staged / "complete").read_bytes(), b"dataset")
                if kind == "populated directory":
                    self.assertEqual((destination / "original").read_bytes(), b"keep me")
                elif kind == "empty directory":
                    self.assertEqual(list(destination.iterdir()), [])
                elif kind == "file":
                    self.assertEqual(destination.read_bytes(), b"keep me")
                else:
                    self.assertEqual(destination.readlink(), root / "missing")

    def test_unsupported_platform_fails_without_publication(self) -> None:
        with TemporaryDirectory() as scratch:
            staged = Path(scratch) / "staged"
            staged.mkdir()
            destination = Path(scratch) / "bids"
            with patch("network_fw2bids._publication.sys.platform", "unsupported"):
                with self.assertRaises(ConversionError) as raised:
                    publish_directory(staged, destination)
            self.assertEqual(raised.exception.__cause__.errno, errno.ENOTSUP)
            self.assertTrue(staged.is_dir())
            self.assertFalse(destination.exists())
