from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import requests

from ...context import Ctx


class TTSError(RuntimeError):
    pass


class TTSProvider(ABC):
    def __init__(self, ctx: Ctx, opts: dict, session: requests.Session | None = None):
        self.ctx = ctx
        self.opts = opts
        self.session = session or requests.Session()

    @abstractmethod
    def synthesize(self, text: str, out_stem: Path) -> Path:
        """Write speech for `text` next to `out_stem` (provider picks the extension). Returns the file."""

    @staticmethod
    def _check(resp: requests.Response, name: str) -> None:
        if resp.status_code >= 400:
            raise TTSError(f"{name} TTS failed ({resp.status_code}): {resp.text[:300]}")
