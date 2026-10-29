from __future__ import annotations

import numpy as np


class Style:
    """A procedural animation. Subclasses set up state in __init__ and return a float canvas per frame."""

    name = "base"

    def __init__(self, width: int, height: int, fps: int, rng: np.random.Generator):
        self.w, self.h, self.fps, self.rng = width, height, fps, rng
        self.params: dict = {}

    def frame(self, i: int) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError

    def to_uint8(self, canvas: np.ndarray) -> np.ndarray:
        from .common import to_uint8

        return to_uint8(canvas)
