# -*- coding: utf-8 -*-

"""
cross-process writer locks for import jobs

only one writer process may hold the lock for a given target database at a
time.  the lock is backed by an advisory kernel lock (fcntl.flock) on a lock
file next to the sqlite database, so:

  * a second writer either waits until the lock is released or fails fast
    when timeout == 0
  * there is no interleave of transactions between two writers
  * when a process dies (even abnormally) the kernel closes its file
    descriptor and releases the lock automatically
"""

import fcntl
import hashlib
import time
from pathlib import Path


class LockAcquireError(Exception):
    """raised when a writer lock cannot be acquired within the timeout"""


class BaseWriterLock:
    """context manager base class"""

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    def acquire(self):
        raise NotImplementedError

    def release(self):
        raise NotImplementedError


class NoOpWriterLock(BaseWriterLock):
    """lock for targets that have no cross-process lock file (e.g. memory)"""

    def acquire(self):
        return self

    def release(self):
        pass


class FileWriterLock(BaseWriterLock):
    """
    flock-based exclusive writer lock

    timeout < 0 waits forever, timeout == 0 fails fast, otherwise waits up
    to timeout seconds.
    """

    def __init__(self, lock_path, timeout: float = 30.0, poll_interval: float = 0.1):
        self.lock_path = str(lock_path)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fh = None

    def acquire(self):
        Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)
        # open with O_CLOEXEC semantics implicitly; the fd is closed on
        # release or when the process terminates, freeing the lock
        self._fh = open(self.lock_path, "a+")
        deadline = None if self.timeout < 0 else time.monotonic() + self.timeout

        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if deadline is not None and time.monotonic() >= deadline:
                    self._fh.close()
                    self._fh = None
                    raise LockAcquireError(
                        f"another writer holds the lock: {self.lock_path}"
                    )
                time.sleep(self.poll_interval)

    def release(self):
        if self._fh is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


class PostgresWriterLock(BaseWriterLock):
    """
    session-level postgres advisory lock (pg_advisory_lock)

    the lock is released explicitly, and automatically when the session
    terminates abnormally.  timeout == 0 fails fast via the try-acquire
    variant; other timeouts poll so the semantics match the file lock.
    """

    def __init__(self, interface, timeout: float = 30.0, poll_interval: float = 0.5):
        self.interface = interface
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._key = None
        self._held = False

    def _advisory_key(self):
        digest = hashlib.sha256(str(self.interface.name).encode("utf-8")).digest()
        # two signed 32-bit ints from the first 8 bytes
        raw = digest[:8]
        hi = int.from_bytes(raw[:4], "big", signed=True)
        lo = int.from_bytes(raw[4:], "big", signed=True)
        return hi, lo

    def acquire(self):
        from .interfaces import DATABASE

        self.interface.initialize(DATABASE)
        DATABASE.connect(reuse_if_open=True)
        self._key = self._advisory_key()
        deadline = None if self.timeout < 0 else time.monotonic() + self.timeout
        cursor = DATABASE.execute_sql("SELECT pg_try_advisory_lock(%s, %s)", self._key)
        while not cursor.fetchone()[0]:
            if deadline is not None and time.monotonic() >= deadline:
                raise LockAcquireError(
                    "another writer holds the postgres advisory lock "
                    f"for {self.interface.name}"
                )
            time.sleep(self.poll_interval)
            cursor = DATABASE.execute_sql(
                "SELECT pg_try_advisory_lock(%s, %s)", self._key
            )
        self._held = True
        return self

    def release(self):
        if not self._held:
            return
        from .interfaces import DATABASE

        DATABASE.execute_sql("SELECT pg_advisory_unlock(%s, %s)", self._key)
        self._held = False


def sqlite_lock_path(db_name: str) -> str:
    """
    derive the lock file path for a sqlite database path
    """

    return str(Path(db_name).with_suffix(Path(db_name).suffix + ".lock"))


def writer_lock_for(interface, timeout: float = 30.0) -> BaseWriterLock:
    """
    return the appropriate writer lock for an interface instance
    """

    name = getattr(interface, "name", None) or ""

    # in-memory databases are private to the process; no cross-process lock
    if name == ":memory:":
        return NoOpWriterLock()

    if interface.endpoint.startswith("postgres"):
        return PostgresWriterLock(interface, timeout=timeout)

    return FileWriterLock(sqlite_lock_path(name), timeout=timeout)
