"""Balls bouncing inside a slowly rotating polygon, with gravity, collisions and fading trails."""

from __future__ import annotations

import numpy as np

from .base import Style
from .common import disc, palette, segment, vertical_gradient, vignette


class Bouncing(Style):
    name = "bouncing"

    def __init__(self, width, height, fps, rng):
        super().__init__(width, height, fps, rng)
        s = min(width, height)
        self.pal = palette(rng, 6)
        self.sides = int(rng.choice([3, 4, 5, 6, 7, 8, 64]))  # 64 ≈ circle
        self.radius = s * rng.uniform(0.40, 0.46)
        self.center = np.array([width / 2, height * rng.uniform(0.42, 0.55)], np.float32)
        self.omega = rng.uniform(0.15, 0.6) * rng.choice([-1, 1])  # rad/s
        self.gravity = s * rng.uniform(0.9, 1.8)  # px/s²
        self.restitution = rng.uniform(0.92, 1.0)
        self.decay = rng.uniform(0.80, 0.93)
        self.grow = bool(rng.random() < 0.5)
        n = int(rng.integers(4, 18))
        self.r0 = s * rng.uniform(0.018, 0.04)
        self.max_r = s * 0.11
        ang = rng.uniform(0, 2 * np.pi, n)
        dist = rng.uniform(0, self.radius * 0.5, n)
        self.pos = self.center + np.stack([np.cos(ang) * dist, np.sin(ang) * dist], 1).astype(np.float32)
        self.vel = rng.normal(0, s * 0.6, (n, 2)).astype(np.float32)
        self.r = np.full(n, self.r0, np.float32) * rng.uniform(0.8, 1.3, n).astype(np.float32)
        self.color_idx = np.arange(n) % len(self.pal["accents"])
        self.flash = np.zeros(self.sides, np.float32)
        self.bg = vertical_gradient(height, width, self.pal["bg_top"], self.pal["bg_bottom"]) * vignette(height, width)
        self.layer = self.bg.copy()
        self.substeps = 3
        self.params = {"sides": self.sides, "omega": round(float(self.omega), 3), "balls": n, "grow": self.grow,
                       "gravity": round(float(self.gravity), 1), "palette": self.pal["scheme"],
                       "hue": round(self.pal["base_hue"], 3)}

    def _vertices(self, t: float) -> np.ndarray:
        a = t * self.omega + np.arange(self.sides) * 2 * np.pi / self.sides + np.pi / 2
        return self.center + self.radius * np.stack([np.cos(a), np.sin(a)], 1)

    def _step(self, t: float, dt: float) -> None:
        self.vel[:, 1] += self.gravity * dt
        self.pos += self.vel * dt
        v = self._vertices(t)
        nxt = np.roll(v, -1, axis=0)
        edges = nxt - v
        normals = np.stack([edges[:, 1], -edges[:, 0]], 1)
        normals /= np.linalg.norm(normals, axis=1, keepdims=True)
        # Make normals point inward.
        mid = (v + nxt) / 2
        flip = np.sum((self.center - mid) * normals, 1) < 0
        normals[flip] *= -1
        for k in range(self.sides):
            n = normals[k]
            d = (self.pos - v[k]) @ n - self.r  # distance inside the wall
            hit = d < 0
            if not hit.any():
                continue
            self.pos[hit] -= np.outer(d[hit], n)
            rel = self.pos[hit] - self.center
            wall_v = self.omega * np.stack([-rel[:, 1], rel[:, 0]], 1)
            vr = self.vel[hit] - wall_v
            vn = vr @ n
            moving_out = vn < 0
            if moving_out.any():
                idx = np.where(hit)[0][moving_out]
                vr2 = vr[moving_out] - np.outer((1 + self.restitution) * vn[moving_out], n)
                self.vel[idx] = vr2 + wall_v[moving_out]
                self.flash[k] = 1.0
                if self.grow:
                    self.r[idx] = np.minimum(self.r[idx] * 1.025, self.max_r)
        # Ball–ball collisions (equal density, mass ∝ r²).
        n = len(self.pos)
        diff = self.pos[:, None, :] - self.pos[None, :, :]
        dist = np.linalg.norm(diff, axis=2) + np.eye(n, dtype=np.float32) * 1e6
        overlap = (self.r[:, None] + self.r[None, :]) - dist
        ii, jj = np.where(np.triu(overlap > 0, 1))
        for i, j in zip(ii, jj):
            nrm = diff[i, j] / dist[i, j]
            mi, mj = self.r[i] ** 2, self.r[j] ** 2
            corr = overlap[i, j] / 2
            self.pos[i] += nrm * corr
            self.pos[j] -= nrm * corr
            rel = (self.vel[i] - self.vel[j]) @ nrm
            if rel < 0:
                imp = -(1 + self.restitution) * rel / (1 / mi + 1 / mj)
                self.vel[i] += imp / mi * nrm
                self.vel[j] -= imp / mj * nrm
        # Keep total energy bounded and balls inside even after pushes.
        speed = np.linalg.norm(self.vel, axis=1)
        cap = min(self.w, self.h) * 2.2
        fast = speed > cap
        self.vel[fast] *= (cap / speed[fast])[:, None]
        if self.grow and float(np.sum(self.r ** 2)) > (self.radius ** 2) * 0.35:
            self.r[:] = self.r0  # "pop" back to small and keep going

    def frame(self, i: int) -> np.ndarray:
        dt = 1.0 / self.fps
        t = i * dt
        for s in range(self.substeps):
            self._step(t + s * dt / self.substeps, dt / self.substeps)
        self.layer = self.bg + (self.layer - self.bg) * self.decay
        acc = self.pal["accents"]
        v = self._vertices(t)
        for k in range(self.sides):
            a, b = v[k], v[(k + 1) % self.sides]
            col = acc[k % len(acc)] if self.sides < 64 else acc[0]
            segment(self.layer, a[0], a[1], b[0], b[1], 3 + 3 * self.flash[k], col, 0.5 + 0.5 * self.flash[k],
                    glow=6 * self.flash[k])
        self.flash *= 0.85
        for p, r, c in zip(self.pos, self.r, self.color_idx):
            disc(self.layer, p[0], p[1], r, acc[c], glow=r * 0.6, highlight=True)
        return self.layer
