"""Non-blocking process lock so overlapping systemd/bot-triggered runs never collide."""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


class LockHeld(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise LockHeld(f"another run holds {self.path}")
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return self

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
