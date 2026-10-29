"""Drawing primitives, palettes and the ffmpeg frame writer for procedural backgrounds.

Everything renders into float32 RGB canvases in [0, 1] with numpy. Anti-aliasing is done
analytically with signed distances on small patches, so it is cheap enough for a Pi 5.
"""

from __future__ import annotations

import colorsys
import subprocess
from pathlib import Path

import numpy as np


# ---------------------------------------------------------------- palettes

def hsl(h: float, s: float, l: float) -> np.ndarray:
    r, g, b = colorsys.hls_to_rgb(h % 1.0, l, s)
    return np.array([r, g, b], dtype=np.float32)


def palette(rng: np.random.Generator, n: int = 5) -> dict:
    """A random but harmonious palette: background pair + n accent colours."""
    base = rng.random()
    scheme = rng.choice(["analogous", "triadic", "complementary", "split"])
    offsets = {
        "analogous": [0, 0.06, 0.12, -0.06, -0.12],
        "triadic": [0, 1 / 3, 2 / 3, 0.05, 1 / 3 + 0.05],
        "complementary": [0, 0.5, 0.04, 0.54, -0.04],
        "split": [0, 0.42, 0.58, 0.08, 0.5],
    }[scheme]
    sat = rng.uniform(0.55, 0.9)
    accents = [hsl(base + offsets[i % len(offsets)], sat, rng.uniform(0.5, 0.68)) for i in range(n)]
    dark = rng.random() < 0.8
    bg_top = hsl(base + rng.uniform(-0.1, 0.1), rng.uniform(0.3, 0.6), rng.uniform(0.06, 0.14) if dark else 0.85)
    bg_bottom = hsl(base + rng.uniform(0.1, 0.3), rng.uniform(0.3, 0.6), rng.uniform(0.02, 0.08) if dark else 0.7)
    return {"scheme": str(scheme), "base_hue": float(base), "accents": accents, "bg_top": bg_top,
            "bg_bottom": bg_bottom, "dark": bool(dark)}


def vertical_gradient(h: int, w: int, top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
    t = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    return np.broadcast_to(top * (1 - t) + bottom * t, (h, w, 3)).copy()


def vignette(h: int, w: int, strength: float = 0.35) -> np.ndarray:
    y = np.linspace(-1, 1, h, dtype=np.float32)[:, None]
    x = np.linspace(-1, 1, w, dtype=np.float32)[None, :] * (w / h)
    d = np.sqrt(x * x + y * y)
    return (1 - strength * np.clip(d - 0.3, 0, None) ** 1.5)[..., None].astype(np.float32)


# ---------------------------------------------------------------- AA primitives

def _patch(canvas: np.ndarray, x0: float, y0: float, x1: float, y1: float):
    h, w = canvas.shape[:2]
    ix0, iy0 = max(0, int(np.floor(x0))), max(0, int(np.floor(y0)))
    ix1, iy1 = min(w, int(np.ceil(x1)) + 1), min(h, int(np.ceil(y1)) + 1)
    if ix0 >= ix1 or iy0 >= iy1:
        return None
    xs = np.arange(ix0, ix1, dtype=np.float32)[None, :] + 0.5
    ys = np.arange(iy0, iy1, dtype=np.float32)[:, None] + 0.5
    return (slice(iy0, iy1), slice(ix0, ix1)), xs, ys


def _blend(canvas, sl, a, color, additive=False):
    patch = canvas[sl]
    a = a[..., None]
    if additive:
        patch += color * a
    else:
        patch += (color - patch) * a


def disc(canvas: np.ndarray, cx: float, cy: float, r: float, color: np.ndarray, alpha: float = 1.0,
         glow: float = 0.0, glow_alpha: float = 0.35, highlight: bool = False) -> None:
    R = r + 3 * glow + 1.5
    p = _patch(canvas, cx - R, cy - R, cx + R, cy + R)
    if p is None:
        return
    sl, xs, ys = p
    d = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
    if glow > 0:
        g = glow_alpha * np.exp(-(np.clip(d - r, 0, None) / glow) ** 2) * (d > r - 1)
        _blend(canvas, sl, g * alpha, color, additive=True)
    a = np.clip(r - d + 0.5, 0, 1) * alpha
    _blend(canvas, sl, a, color)
    if highlight and r > 4:
        hx, hy, hr = cx - r * 0.35, cy - r * 0.35, r * 0.3
        dh = np.sqrt((xs - hx) ** 2 + (ys - hy) ** 2)
        _blend(canvas, sl, np.clip(hr - dh + 0.5, 0, 1) * 0.55 * alpha, np.ones(3, np.float32))


def ring(canvas, cx, cy, r, width, color, alpha=1.0):
    R = r + width + 1.5
    p = _patch(canvas, cx - R, cy - R, cx + R, cy + R)
    if p is None:
        return
    sl, xs, ys = p
    d = np.abs(np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) - r)
    _blend(canvas, sl, np.clip(width / 2 - d + 0.5, 0, 1) * alpha, color)


def segment(canvas: np.ndarray, x0: float, y0: float, x1: float, y1: float, width: float,
            color: np.ndarray, alpha: float = 1.0, glow: float = 0.0) -> None:
    pad = width / 2 + 3 * glow + 1.5
    p = _patch(canvas, min(x0, x1) - pad, min(y0, y1) - pad, max(x0, x1) + pad, max(y0, y1) + pad)
    if p is None:
        return
    sl, xs, ys = p
    bx, by = x1 - x0, y1 - y0
    L2 = bx * bx + by * by or 1e-6
    t = np.clip(((xs - x0) * bx + (ys - y0) * by) / L2, 0, 1)
    d = np.sqrt((xs - x0 - bx * t) ** 2 + (ys - y0 - by * t) ** 2)
    if glow > 0:
        _blend(canvas, sl, 0.3 * alpha * np.exp(-(np.clip(d - width / 2, 0, None) / glow) ** 2), color, True)
    _blend(canvas, sl, np.clip(width / 2 - d + 0.5, 0, 1) * alpha, color)


def rect(canvas, x0, y0, x1, y1, color, alpha=1.0, radius: float = 0.0):
    """Axis-aligned (optionally rounded) rectangle with AA edges."""
    p = _patch(canvas, x0 - 1, y0 - 1, x1 + 1, y1 + 1)
    if p is None:
        return
    sl, xs, ys = p
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hx, hy = (x1 - x0) / 2 - radius, (y1 - y0) / 2 - radius
    qx, qy = np.abs(xs - cx) - hx, np.abs(ys - cy) - hy
    d = np.sqrt(np.clip(qx, 0, None) ** 2 + np.clip(qy, 0, None) ** 2) + np.minimum(np.maximum(qx, qy), 0) - radius
    _blend(canvas, sl, np.clip(0.5 - d, 0, 1) * alpha, color)


def to_uint8(canvas: np.ndarray, dither: np.random.Generator | None = None) -> np.ndarray:
    if dither is not None:  # breaks up banding in smooth gradients before x264
        canvas = canvas + dither.uniform(-0.5 / 255, 0.5 / 255, canvas.shape[:2])[..., None].astype(np.float32)
    return (np.clip(canvas, 0, 1) * 255 + 0.5).astype(np.uint8)


# ---------------------------------------------------------------- writer

class FrameWriter:
    """Pipes raw RGB frames into ffmpeg/libx264."""

    def __init__(self, path: Path, width: int, height: int, fps: int, crf: int = 20, preset: str = "veryfast"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(self.path),
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        self.proc.stdin.write(np.ascontiguousarray(frame).tobytes())

    def close(self) -> None:
        self.proc.stdin.close()
        err = self.proc.stderr.read().decode(errors="replace")
        if self.proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed writing {self.path}: {err}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.close()
        else:
            self.proc.kill()
            self.proc.wait()
