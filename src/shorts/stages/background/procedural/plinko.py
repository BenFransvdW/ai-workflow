"""Marble-run peg board: marbles cascade through pegs into bins, stack up, then the board clears."""

from __future__ import annotations

import numpy as np

from .base import Style
from .common import disc, hsl, palette, rect, vertical_gradient, vignette


class Plinko(Style):
    name = "plinko"

    def __init__(self, width, height, fps, rng):
        super().__init__(width, height, fps, rng)
        self.pal = palette(rng, 6)
        cols = int(rng.integers(7, 12))
        rows = int(rng.integers(10, 16))
        margin = width * 0.06
        self.spacing = (width - 2 * margin) / cols
        top, self.floor = height * 0.12, height * 0.95
        self.bin_top = height * rng.uniform(0.66, 0.72)
        row_h = (self.bin_top - top - self.spacing) / rows
        pegs = []
        for r in range(rows):
            offs = 0.5 if r % 2 else 0.0
            for c in range(cols + 1):
                x = margin + (c + offs) * self.spacing
                if margin * 0.9 <= x <= width - margin * 0.9:
                    pegs.append((x, top + self.spacing + r * row_h))
        self.pegs = np.array(pegs, np.float32)
        self.peg_r = self.spacing * rng.uniform(0.07, 0.12)
        self.marble_r = self.spacing * rng.uniform(0.17, 0.24)
        self.peg_flash = np.zeros(len(self.pegs), np.float32)
        self.margin = margin
        self.n_bins = cols
        self.bin_w = (width - 2 * margin) / cols
        self.gravity = height * rng.uniform(0.9, 1.4)
        self.restitution = rng.uniform(0.35, 0.6)
        self.spawn_every = rng.uniform(0.08, 0.25)  # seconds
        self.hue_speed = rng.uniform(0.01, 0.05)
        self.pos = np.zeros((0, 2), np.float32)
        self.vel = np.zeros((0, 2), np.float32)
        self.col = np.zeros((0, 3), np.float32)
        self.stacks = np.zeros(self.n_bins, np.int32)
        self.max_stack = int((self.floor - self.bin_top) / (2 * self.marble_r * 0.87)) - 1
        self.next_spawn = 0.0
        self.fade = 1.0
        self.clearing = False
        self.count = 0

        bg = vertical_gradient(height, width, self.pal["bg_top"], self.pal["bg_bottom"]) * vignette(height, width)
        self.static = bg.copy()
        frame_col = self.pal["accents"][1] * 0.6
        for b in range(self.n_bins + 1):
            x = margin + b * self.bin_w
            rect(self.static, x - 2, self.bin_top, x + 2, self.floor, frame_col, radius=2)
        rect(self.static, margin - 2, self.floor, width - margin + 2, self.floor + 4, frame_col)
        self.bg = bg
        self.settled = np.zeros_like(bg)  # colour of settled marbles, composited with settled_a
        self.settled_a = np.zeros(bg.shape[:2] + (1,), np.float32)
        self.params = {"cols": cols, "rows": rows, "spawn_every": round(float(self.spawn_every), 3),
                       "restitution": round(float(self.restitution), 2), "palette": self.pal["scheme"],
                       "hue": round(self.pal["base_hue"], 3)}

    def _spawn(self, t: float) -> None:
        x = self.w / 2 + self.rng.normal(0, self.spacing * 0.4)
        self.pos = np.vstack([self.pos, [[x, self.h * 0.05]]]).astype(np.float32)
        self.vel = np.vstack([self.vel, [[self.rng.normal(0, 20), 0]]]).astype(np.float32)
        hue = self.pal["base_hue"] + t * self.hue_speed + 0.15 * np.sin(self.count * 0.07)
        self.col = np.vstack([self.col, hsl(hue, 0.75, 0.6)[None]]).astype(np.float32)
        self.count += 1

    def _settle(self, idx: np.ndarray) -> None:
        keep = np.ones(len(self.pos), bool)
        for i in idx:
            b = int(np.clip((self.pos[i, 0] - self.margin) // self.bin_w, 0, self.n_bins - 1))
            k = self.stacks[b]
            per_row = max(1, int(self.bin_w // (2 * self.marble_r)))
            row, slot = divmod(k, per_row)
            x = self.margin + b * self.bin_w + self.bin_w / 2 + (slot - (per_row - 1) / 2) * 2 * self.marble_r
            x += self.marble_r * (row % 2) * 0.5 * (1 if slot < per_row - 1 else -1)
            y = self.floor - self.marble_r - row * 2 * self.marble_r * 0.87
            disc(self.settled, x, y, self.marble_r + 0.5, self.col[i], highlight=True)
            disc(self.settled_a, x, y, self.marble_r, np.ones(1, np.float32))
            self.stacks[b] += 1
            keep[i] = False
            if row >= self.max_stack:
                self.clearing = True
        self.pos, self.vel, self.col = self.pos[keep], self.vel[keep], self.col[keep]

    def _step(self, dt: float) -> None:
        if not len(self.pos):
            return
        self.vel[:, 1] += self.gravity * dt
        self.pos += self.vel * dt
        # Pegs.
        d = self.pos[:, None, :] - self.pegs[None, :, :]
        dist = np.linalg.norm(d, axis=2)
        mi, pj = np.where(dist < self.marble_r + self.peg_r)
        for i, j in zip(mi, pj):
            n = d[i, j] / max(dist[i, j], 1e-3)
            self.pos[i] = self.pegs[j] + n * (self.marble_r + self.peg_r)
            vn = self.vel[i] @ n
            if vn < 0:
                self.vel[i] -= (1 + self.restitution) * vn * n
                self.vel[i, 0] += self.rng.normal(0, 8)  # tiny jitter so paths differ
                self.peg_flash[j] = 1.0
        # Side walls and bin dividers.
        lo, hi = self.margin + self.marble_r, self.w - self.margin - self.marble_r
        for side, bound in ((self.pos[:, 0] < lo, lo), (self.pos[:, 0] > hi, hi)):
            self.pos[side, 0] = bound
            self.vel[side, 0] *= -self.restitution
        in_bins = self.pos[:, 1] > self.bin_top
        if in_bins.any():
            rel = (self.pos[:, 0] - self.margin) / self.bin_w
            b = np.floor(rel)
            frac = rel - b
            lim = self.marble_r / self.bin_w
            left, right = in_bins & (frac < lim), in_bins & (frac > 1 - lim)
            self.pos[left, 0] = self.margin + (b[left] + lim) * self.bin_w
            self.pos[right, 0] = self.margin + (b[right] + 1 - lim) * self.bin_w
            self.vel[left | right, 0] *= -self.restitution
            # Landing on the stack in this bin.
            bins = np.clip(b.astype(int), 0, self.n_bins - 1)
            per_row = max(1, int(self.bin_w // (2 * self.marble_r)))
            surface = self.floor - (self.stacks[bins] // per_row + 1) * 2 * self.marble_r * 0.87
            landed = np.where(in_bins & (self.pos[:, 1] >= surface))[0]
            if len(landed):
                self._settle(landed)

    def frame(self, i: int) -> np.ndarray:
        dt = 1.0 / self.fps
        t = i * dt
        if self.clearing:
            self.fade -= dt / 1.2
            if self.fade <= 0:
                self.settled[:] = 0
                self.settled_a[:] = 0
                self.stacks[:] = 0
                self.fade, self.clearing = 1.0, False
        elif t >= self.next_spawn:
            self._spawn(t)
            self.next_spawn = t + self.spawn_every * self.rng.uniform(0.6, 1.4)
        for _ in range(4):
            self._step(dt / 4)
        out = self.static + (self.settled - self.static) * (self.settled_a * self.fade)
        peg_col = self.pal["accents"][0]
        for (x, y), f in zip(self.pegs, self.peg_flash):
            disc(out, x, y, self.peg_r * (1 + 0.5 * f), peg_col * (0.6 + 0.4 * f) + f * 0.3, glow=4 * f if f > 0.05 else 0)
        self.peg_flash *= 0.88
        for p, c in zip(self.pos, self.col):
            disc(out, p[0], p[1], self.marble_r, c, highlight=True)
        return out
