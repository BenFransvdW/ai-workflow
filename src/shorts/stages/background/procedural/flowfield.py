"""Thousands of particles drifting along an evolving flow field, leaving glowing additive trails."""

from __future__ import annotations

import numpy as np

from .base import Style
from .common import hsl, palette, vertical_gradient, vignette


class FlowField(Style):
    name = "flowfield"

    def __init__(self, width, height, fps, rng):
        super().__init__(width, height, fps, rng)
        self.pal = palette(rng, 5)
        scale = width / 540
        self.n = int(rng.integers(3000, 7000) * scale ** 2) or 50
        self.pos = np.stack([rng.uniform(0, width, self.n), rng.uniform(0, height, self.n)], 1).astype(np.float32)
        self.life = rng.uniform(0, 1, self.n).astype(np.float32)
        self.speed = float(rng.uniform(1.2, 2.6)) * scale * 30 / fps
        self.f = (rng.uniform(0.002, 0.008, 4) / scale).astype(np.float32)
        self.tw = rng.uniform(0.05, 0.25, 4).astype(np.float32)
        self.curl = float(rng.uniform(1.0, 2.5))
        self.decay = float(rng.uniform(0.9, 0.96))
        self.gain = float(rng.uniform(1.0, 1.8))
        hues = self.pal["base_hue"] + rng.choice([0, 0.08, 0.5, 0.33], self.n) + rng.normal(0, 0.03, self.n)
        self.colors = np.array([hsl(h, 0.8, 0.55) for h in hues[:256]], np.float32)
        self.color_idx = rng.integers(0, len(self.colors), self.n)
        self.buf = np.zeros((height * width, 3), np.float32)
        hue = self.pal["base_hue"]
        self.bg = vertical_gradient(height, width, hsl(hue, 0.5, rng.uniform(0.04, 0.09)), hsl(hue + 0.1, 0.5, 0.02))
        self.vig = vignette(height, width, 0.45)
        self.params = {"particles": self.n, "curl": round(self.curl, 2), "decay": round(self.decay, 3),
                       "palette": self.pal["scheme"], "hue": round(self.pal["base_hue"], 3)}

    def _angle(self, p: np.ndarray, t: float) -> np.ndarray:
        x, y = p[:, 0], p[:, 1]
        f, w = self.f, self.tw
        a = (np.sin(x * f[0] + t * w[0]) + np.cos(y * f[1] - t * w[1])
             + np.sin((x + y) * f[2] + np.sin(t * w[2]) * 2) + np.cos((x - y) * f[3] + t * w[3]))
        return a * self.curl

    def _splat(self, p: np.ndarray, weight: float) -> None:
        ix = p[:, 0].astype(np.int32)
        iy = p[:, 1].astype(np.int32)
        ok = (ix >= 0) & (ix < self.w) & (iy >= 0) & (iy < self.h)
        flat = iy[ok] * self.w + ix[ok]
        cols = self.colors[self.color_idx[ok]] * weight
        size = self.w * self.h
        for ch in range(3):
            self.buf[:, ch] += np.bincount(flat, weights=cols[:, ch], minlength=size)

    def frame(self, i: int) -> np.ndarray:
        t = i / self.fps
        self.buf *= self.decay
        for _ in range(2):
            a = self._angle(self.pos, t)
            step = np.stack([np.cos(a), np.sin(a)], 1) * (self.speed / 2)
            self.pos += step
            self._splat(self.pos, 0.6)
        self.life -= 1 / (self.fps * 6)  # ~6 s lifetime
        dead = (self.life <= 0) | (self.pos[:, 0] < -2) | (self.pos[:, 0] > self.w + 2) | \
               (self.pos[:, 1] < -2) | (self.pos[:, 1] > self.h + 2)
        k = int(dead.sum())
        if k:
            self.pos[dead] = np.stack([self.rng.uniform(0, self.w, k), self.rng.uniform(0, self.h, k)], 1)
            self.life[dead] = self.rng.uniform(0.5, 1.0, k)
        light = 1 - np.exp(-self.buf.reshape(self.h, self.w, 3) * self.gain)
        return (self.bg + light) * self.vig
