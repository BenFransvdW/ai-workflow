"""Parallax endless-runner: layered hills, sky, clouds and a stick runner jumping obstacles."""

from __future__ import annotations

import numpy as np

from .base import Style
from .common import disc, hsl, rect, segment, vertical_gradient


class Runner(Style):
    name = "runner"

    def __init__(self, width, height, fps, rng):
        super().__init__(width, height, fps, rng)
        base = rng.random()
        self.night = bool(rng.random() < 0.5)
        sky_top = hsl(base, 0.55, 0.08 if self.night else 0.55)
        sky_bot = hsl(base + rng.uniform(0.05, 0.2), 0.6, 0.25 if self.night else 0.82)
        self.sky = vertical_gradient(height, width, sky_top, sky_bot)
        # Sun or moon with glow, baked into the sky.
        cx, cy, r = width * rng.uniform(0.2, 0.8), height * rng.uniform(0.15, 0.32), width * rng.uniform(0.06, 0.12)
        disc(self.sky, cx, cy, r, hsl(base + 0.1, 0.4, 0.92 if self.night else 0.75), glow=r * 0.8, glow_alpha=0.25)
        if self.night:
            for _ in range(int(rng.integers(60, 160))):
                disc(self.sky, rng.uniform(0, width), rng.uniform(0, height * 0.55), rng.uniform(0.5, 1.6),
                     np.ones(3, np.float32), alpha=rng.uniform(0.3, 0.9))

        n_layers = int(rng.integers(3, 6))
        self.layers = []
        for k in range(n_layers):
            depth = (k + 1) / n_layers  # 0 far … 1 near
            light = (0.18 + 0.5 * (1 - depth)) if not self.night else (0.08 + 0.22 * (1 - depth))
            col = hsl(base + 0.45 + rng.uniform(-0.08, 0.08) + depth * 0.05, 0.35 + 0.3 * depth, light)
            freqs = rng.uniform(0.002, 0.012, 4) * (1 + depth)
            amps = rng.uniform(0.02, 0.07, 4) * height * (1.2 - 0.5 * depth)
            phases = rng.uniform(0, 2 * np.pi, 4)
            baseline = height * (0.48 + 0.26 * depth)
            self.layers.append(dict(col=col, freqs=freqs, amps=amps, phases=phases, base=baseline,
                                    speed=width * (0.04 + 0.45 * depth ** 2)))
        self.ground_y = height * 0.82
        self.ground_col = hsl(base + 0.5, 0.4, 0.12 if self.night else 0.22)
        self.stripe_col = hsl(base + 0.5, 0.35, 0.2 if self.night else 0.32)
        self.speed = width * rng.uniform(0.55, 0.85)  # foreground px/s
        self.clouds = [dict(x=rng.uniform(0, width), y=rng.uniform(0.08, 0.4) * height,
                            s=rng.uniform(0.6, 1.5), v=rng.uniform(8, 25)) for _ in range(int(rng.integers(3, 7)))]
        self.cloud_col = hsl(base, 0.2, 0.7 if self.night else 0.97)
        # Obstacles in world x.
        xs, x = [], width * 1.2
        for _ in range(400):
            x += rng.uniform(0.7, 1.6) * width
            xs.append(x)
        self.obstacles = np.array(xs)
        self.ob_h = rng.uniform(0.035, 0.06, len(xs)) * height
        self.ob_w = rng.uniform(0.04, 0.07, len(xs)) * width
        self.ob_col = hsl(base + 0.95, 0.75, 0.55)
        self.runner_x = width * 0.3
        self.runner_col = hsl(base + 0.55, 0.15, 0.95 if self.night else 0.08)
        self.jump_t = 0.65
        self.jump_h = height * 0.13
        self.jump_start = -10.0
        self.yy = np.arange(height, dtype=np.float32)[:, None] + 0.5
        self.xs = np.arange(width, dtype=np.float32)[None, :]
        self.params = {"layers": n_layers, "night": self.night, "speed": round(float(self.speed), 1),
                       "hue": round(float(base), 3)}

    def _hills(self, canvas, layer, offset):
        x = (self.xs + offset * layer["speed"])[0]
        hgt = layer["base"] + sum(a * np.sin(f * x + p) for a, f, p in zip(layer["amps"], layer["freqs"], layer["phases"]))
        bottom = int(np.ceil(self.ground_y)) + 1
        y0 = max(0, int(hgt.min()))
        y1 = min(bottom, int(np.ceil(hgt.max())) + 1)
        if y0 < y1:  # anti-aliased skyline band
            alpha = np.clip(self.yy[y0:y1] - hgt[None, :], 0, 1)[..., None]
            band = canvas[y0:y1]
            band += (layer["col"] - band) * alpha
        if y1 < bottom:  # solid below the skyline
            canvas[y1:bottom] = layer["col"]

    def _runner(self, canvas, t):
        world = t * self.speed
        ahead = self.obstacles - (world + self.runner_x)
        nxt = ahead[ahead > 0]
        if len(nxt) and 0 < nxt[0] < self.speed * self.jump_t * 0.45 and t - self.jump_start > self.jump_t:
            self.jump_start = t
        jt = (t - self.jump_start) / self.jump_t
        air = 0 <= jt <= 1
        lift = 4 * self.jump_h * jt * (1 - jt) if air else 0.0
        s = self.h * 0.03  # body unit
        hip = np.array([self.runner_x, self.ground_y - 2.2 * s - lift])
        neck = hip + [s * 0.35, -2.2 * s]
        c, w = self.runner_col, s * 0.42
        phase = t * self.speed / (s * 6) * 2 * np.pi
        for sign in (1, -1):
            if air:
                knee = hip + [s * 0.9, s * 0.5]
                foot = knee + [-s * 0.4, s * 0.9]
                elbow, hand = neck + [s * 0.7 * sign, s * 0.5], neck + [s * 1.1, -s * 0.2 * sign]
            else:
                a = np.sin(phase) * sign
                knee = hip + [s * 0.9 * a, s * 1.0]
                foot = knee + [s * 0.7 * a - s * 0.3 * abs(a), s * 1.1]
                elbow = neck + [-s * 0.7 * a, s * 0.7]
                hand = elbow + [s * 0.5, -s * 0.2 - s * 0.4 * a]
            segment(canvas, *hip, *knee, w, c)
            segment(canvas, *knee, *foot, w, c)
            segment(canvas, *neck, *elbow, w * 0.9, c)
            segment(canvas, *elbow, *hand, w * 0.9, c)
        segment(canvas, *hip, *neck, w * 1.3, c)
        disc(canvas, neck[0] + s * 0.25, neck[1] - s * 0.75, s * 0.6, c)
        if not air:  # dust puffs
            for k in range(3):
                disc(canvas, self.runner_x - s * (1 + k * 0.8), self.ground_y - s * 0.2 * k, s * 0.18 * (3 - k),
                     self.stripe_col + 0.15, alpha=0.4)

    def frame(self, i: int) -> np.ndarray:
        t = i / self.fps
        canvas = self.sky.copy()
        for cl in self.clouds:
            x = (cl["x"] - t * cl["v"]) % (self.w * 1.4) - self.w * 0.2
            for dx, dy, rr in ((0, 0, 28), (25, 6, 22), (-24, 8, 18), (10, -10, 20)):
                disc(canvas, x + dx * cl["s"], cl["y"] + dy * cl["s"], rr * cl["s"], self.cloud_col, alpha=0.55)
        for layer in self.layers:
            self._hills(canvas, layer, t)
        rect(canvas, 0, self.ground_y, self.w, self.h, self.ground_col)
        stripe = self.w * 0.18
        off = (t * self.speed) % stripe
        for k in range(-1, int(self.w / stripe) + 2):
            x = k * stripe - off
            rect(canvas, x, self.ground_y + self.h * 0.03, x + stripe * 0.5, self.ground_y + self.h * 0.036, self.stripe_col)
        world = t * self.speed
        vis = np.where((self.obstacles > world - self.w * 0.2) & (self.obstacles < world + self.w * 1.2))[0]
        for k in vis:
            x = self.obstacles[k] - world
            rect(canvas, x - self.ob_w[k] / 2, self.ground_y - self.ob_h[k], x + self.ob_w[k] / 2, self.ground_y + 1,
                 self.ob_col, radius=3)
        self._runner(canvas, t)
        return canvas
