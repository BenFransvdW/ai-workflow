"""Procedural background renderer: original, code-generated visuals (copyright-safe by construction)."""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np

from .base import Style
from .bouncing import Bouncing
from .common import FrameWriter
from .flowfield import FlowField
from .plasma import Plasma
from .plinko import Plinko
from .runner import Runner

STYLES: dict[str, type[Style]] = {cls.name: cls for cls in (Bouncing, Plinko, Runner, Plasma, FlowField)}


def render_clip(style: str, out_path: Path, *, width: int, height: int, fps: int, seconds: float,
                seed: int, crf: int = 20, preset: str = "veryfast") -> dict:
    """Render `seconds` of `style` to an MP4. Returns metadata including the randomised params."""
    rng = np.random.default_rng(seed)
    anim = STYLES[style](width, height, fps, rng)
    frames = int(round(seconds * fps))
    t0 = time.monotonic()
    with FrameWriter(out_path, width, height, fps, crf=crf, preset=preset) as wr:
        for i in range(frames):
            wr.write(anim.to_uint8(anim.frame(i)))
    elapsed = time.monotonic() - t0
    return {"style": style, "seed": seed, "frames": frames, "seconds": frames / fps, "render_seconds": round(elapsed, 2),
            "width": width, "height": height, "fps": fps, "params": anim.params}
