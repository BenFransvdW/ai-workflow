"""Domain-warped plasma "shader", computed at quarter resolution and upscaled smoothly."""

from __future__ import annotations

import numpy as np
from PIL import Image

from .base import Style
from .common import to_uint8, vignette


class Plasma(Style):
    name = "plasma"

    def __init__(self, width, height, fps, rng):
        super().__init__(width, height, fps, rng)
        self.div = 4
        gw, gh = width // self.div, height // self.div
        y, x = np.mgrid[0:gh, 0:gw].astype(np.float32)
        self.x = (x / gw - 0.5) * (gw / gh) * 2
        self.y = (y / gh - 0.5) * 2
        self.f = rng.uniform(1.5, 5.0, 6).astype(np.float32)
        self.s = rng.uniform(0.08, 0.35, 6).astype(np.float32) * rng.choice([-1, 1], 6)
        self.warp = float(rng.uniform(0.3, 1.2))
        self.zoom_speed = float(rng.uniform(0.01, 0.04))
        self.rot_speed = float(rng.uniform(-0.05, 0.05))
        self.bands = int(rng.choice([0, 0, 6, 9, 14]))  # 0 = smooth, otherwise topographic contours
        # Cosine palette (Inigo Quilez): a + b*cos(2π(c*t + d)).
        self.a = rng.uniform(0.35, 0.55, 3).astype(np.float32)
        self.b = rng.uniform(0.25, 0.42, 3).astype(np.float32)
        self.c = rng.choice([0.5, 1.0, 1.0, 2.0], 3).astype(np.float32)
        self.d = rng.uniform(0, 1, 3).astype(np.float32)
        self.dim = float(rng.uniform(0.55, 0.85))  # keep it calm behind captions
        self.vig = vignette(height, width, 0.4)
        self.dither = np.random.default_rng(int(rng.integers(1 << 31)))
        self.params = {"warp": round(self.warp, 2), "bands": self.bands, "freqs": [round(float(v), 2) for v in self.f]}

    def field(self, t: float) -> np.ndarray:
        ang = t * self.rot_speed
        z = 1 + 0.3 * np.sin(t * self.zoom_speed * 2 * np.pi)
        x = (self.x * np.cos(ang) - self.y * np.sin(ang)) * z
        y = (self.x * np.sin(ang) + self.y * np.cos(ang)) * z
        f, s = self.f, self.s
        wx = x + self.warp * np.sin(y * f[0] + t * s[0] * 6)
        wy = y + self.warp * np.cos(x * f[1] + t * s[1] * 6)
        v = (np.sin(wx * f[2] + t * s[2] * 6) + np.sin(wy * f[3] + t * s[3] * 6)
             + np.sin((wx + wy) * f[4] + t * s[4] * 6) + np.sin(np.sqrt(wx * wx + wy * wy) * f[5] + t * s[5] * 6))
        return v * 0.125 + 0.5  # → [0, 1]

    def frame(self, i: int) -> np.ndarray:
        t = i / self.fps
        v = self.field(t)
        if self.bands:
            # Contours must be computed at full resolution or they come out jagged after upscaling.
            v = np.asarray(Image.fromarray(v.astype(np.float32), mode="F").resize((self.w, self.h), Image.BILINEAR))
            q = v * self.bands
            shade = 1 - 0.35 * np.clip(1 - np.abs(q - np.round(q)) * 14, 0, 1)
            k = np.clip(q.astype(np.int32), 0, self.bands)
            levels = np.arange(self.bands + 1, dtype=np.float32)[:, None] / self.bands
            lut = (self.a + self.b * np.cos(2 * np.pi * (self.c * (levels + t * 0.01) + self.d))) * self.dim
            return lut[k] * (shade[..., None] * self.vig)
        col = self.a + self.b * np.cos(2 * np.pi * (self.c * (v[..., None] + t * 0.01) + self.d))
        small = to_uint8(col * self.dim)
        big = np.asarray(Image.fromarray(small).resize((self.w, self.h), Image.BICUBIC), dtype=np.float32) / 255
        return big * self.vig

    def to_uint8(self, canvas):
        return to_uint8(canvas, self.dither)
