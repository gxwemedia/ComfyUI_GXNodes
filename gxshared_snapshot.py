"""Request-local verified upload snapshots; no network or persistent registry.

Protects against normal concurrent file changes and pathname reopeners. Process
injection, handle duplication by a privileged/same-account attacker and kernel
access are outside this contract. Cancellation cannot interrupt a blocked OS read;
the worker retains ownership and cleans up when that read returns.
"""
import asyncio
import copy
import hashlib
import os
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


class SnapshotError(ValueError):
    pass


@dataclass
class SnapshotItem:
    resource: dict
    source: object

    def take(self):
        if self.source is None:
            raise SnapshotError("素材快照句柄已移交")
        source, self.source = self.source, None
        return source


class SnapshotSet:
    def __init__(self):
        self.items = []

    def close(self):
        first = None
        for item in self.items:
            if item.source is None:
                continue
            try:
                item.source.close()
            except OSError as error:
                first = first or error
        if first:
            raise first

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _private_file(directory):
    if os.name != "nt":
        return tempfile.TemporaryFile(mode="w+b", dir=directory)
    import ctypes
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                       ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    create.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    # CREATE_NEW, no sharing, non-inheritable, delete-on-close. No second open.
    name = str(Path(directory or tempfile.gettempdir()) / ("gx-snapshot-" + uuid.uuid4().hex + ".tmp"))
    handle = create(name, 0x80000000 | 0x40000000 | 0x00010000, 0,
                    None, 1, 0x04000000 | 0x00000100, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    except BaseException:
        close(handle)
        raise
    try:
        os.set_inheritable(fd, False)
        return os.fdopen(fd, "w+b")
    except BaseException:
        os.close(fd)
        raise


class _Work:
    def __init__(self, resources, max_bytes, directory):
        self.resources = resources
        self.max_bytes = max_bytes
        self.directory = directory
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.result = None
        self.cleanup = None

    def abandon(self):
        with self.lock:
            self.cancelled.set()
            result, self.result = self.result, None
        if result is not None:
            # The executor owns the set until close returns; cancellation must
            # not wait on a buffered-file lock or replace CancelledError.
            self.cleanup = asyncio.get_running_loop().run_in_executor(None, result.close)
            def consumed(done):
                if not done.cancelled() and done.exception() is not None:
                    print("[ComfyUI_GXNodes] 已取消素材快照清理失败，请检查临时存储")
            self.cleanup.add_done_callback(consumed)

    def build(self):
        result = SnapshotSet()
        try:
            remaining = self.max_bytes
            for resource in self.resources:
                if self.cancelled.is_set():
                    raise SnapshotError("素材快照已取消")
                size, expected = resource.get("size"), resource.get("sha256")
                if (isinstance(size, bool) or not isinstance(size, int) or size < 0
                        or size > remaining or not isinstance(expected, str)
                        or len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)):
                    raise SnapshotError("素材大小、摘要或总量无效，请重新确认")
                sink = _private_file(self.directory)
                result.items.append(SnapshotItem(resource, sink))
                digest, total = hashlib.sha256(), 0
                with open(resource["_path"], "rb") as source:
                    while True:
                        if self.cancelled.is_set():
                            raise SnapshotError("素材快照已取消")
                        chunk = source.read(min(1024 * 1024, size - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > size:
                            raise SnapshotError("素材已变化，请重新确认")
                        sink.write(chunk)
                        digest.update(chunk)
                if total != size or digest.hexdigest() != expected:
                    raise SnapshotError("素材已变化，请重新确认")
                sink.flush()
                sink.seek(0)
                remaining -= total
            with self.lock:
                if self.cancelled.is_set():
                    raise SnapshotError("素材快照已取消")
                self.result = result
            return result
        except BaseException:
            result.close()
            raise


async def snapshot_resources(resources, max_bytes, *, directory=None):
    """Return all verified snapshots or none; caller closes the returned set.

    ``directory`` is an optional pre-existing scratch directory for isolation.
    Each repeated resource is charged independently against ``max_bytes``.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise SnapshotError("素材总量限制无效")
    work = _Work(copy.deepcopy(list(resources)), max_bytes, directory)
    task = asyncio.create_task(asyncio.to_thread(work.build))
    try:
        result = await asyncio.shield(task)
    except asyncio.CancelledError:
        work.abandon()
        # Consume eventual worker failure without waiting on a blocked OS read.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        raise
    except (OSError, KeyError, TypeError) as error:
        raise SnapshotError("素材快照无法创建或读取，请检查后重试") from error
    with work.lock:
        work.result = None  # ownership transfers to caller, with no await gap
    return result
