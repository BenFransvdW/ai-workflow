"""Procedural provider backed by a rotating pool of pre-rendered clips.

`fill_pool` renders clips during idle time (end of `shorts run`, or `shorts prerender`). Each clip
is used at most `max_uses_per_clip` times (different segments), then deleted and replaced with a
fresh render using a new seed, so the pool keeps changing.
"""

from __future__ import annotations

import json
import logging
import random
import time

from ...config import PROCEDURAL_STYLES, SeriesBackground
from ...context import Ctx
from .base import BackgroundProvider, Piece, Segment, free_start, register_clip, retire_clip, usage
from .procedural import render_clip

log = logging.getLogger("shorts.background.procedural")


def active_clips(ctx: Ctx, style: str):
    return ctx.conn.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM bg_usage u WHERE u.clip_id = c.id) AS uses FROM bg_clips c"
        " WHERE provider = 'procedural' AND style = ? AND retired_at IS NULL ORDER BY uses, c.id",
        (style,),
    ).fetchall()


def render_one(ctx: Ctx, style: str, seed: int | None = None, min_seconds: float = 0) -> int:
    pc = ctx.config.background.procedural
    seed = seed if seed is not None else random.SystemRandom().randrange(1 << 31)
    out = ctx.work_dir("bg_pool", style) / f"{style}_{seed}.mp4"
    meta = render_clip(style, out, width=pc.width, height=pc.height, fps=pc.fps,
                       seconds=max(pc.clip_seconds, min_seconds + 1),
                       seed=seed, crf=pc.crf, preset=pc.preset)
    log.info("rendered %s seed=%s in %.1fs (%s)", style, seed, meta["render_seconds"], meta["params"])
    return register_clip(ctx.conn, provider="procedural", path=out, duration=meta["seconds"], style=style,
                         params_json=json.dumps(meta), width=pc.width, height=pc.height,
                         license="Original procedural render (owned)", external_id=f"{style}:{seed}")


def fill_pool(ctx: Ctx, styles: list[str] | None = None, count: int | None = None,
              deadline: float | None = None) -> int:
    """Top up the pool to `count` unretired clips per style. Stops early at `deadline` (monotonic)."""
    styles = styles or list(PROCEDURAL_STYLES)
    count = count if count is not None else ctx.config.background.pool_size_per_style
    made = 0
    for _ in range(count):
        for style in styles:
            if deadline is not None and time.monotonic() > deadline:
                return made
            if len(active_clips(ctx, style)) < count:
                render_one(ctx, style)
                made += 1
    return made


class ProceduralProvider(BackgroundProvider):
    name = "procedural"

    def _style_order(self, styles: list[str]) -> list[str]:
        """Least recently used style first, so consecutive videos look different."""
        last: dict[str, int] = {}
        for r in self.ctx.conn.execute(
            "SELECT c.style, MAX(u.id) AS last FROM bg_usage u JOIN bg_clips c ON c.id = u.clip_id"
            " WHERE c.provider = 'procedural' GROUP BY c.style"
        ):
            last[r["style"]] = r["last"]
        shuffled = styles[:]
        self.rng.shuffle(shuffled)
        return sorted(shuffled, key=lambda s: last.get(s, -1))

    def get_segment(self, duration: float, spec: SeriesBackground) -> Segment:
        max_uses = self.ctx.config.background.max_uses_per_clip
        last = self.ctx.conn.execute(
            "SELECT clip_id FROM bg_usage u JOIN bg_clips c ON c.id = u.clip_id WHERE c.provider = 'procedural'"
            " ORDER BY u.id DESC LIMIT 1"
        ).fetchone()
        last_clip = last["clip_id"] if last else None
        for style in self._style_order(spec.styles or list(PROCEDURAL_STYLES)):
            for clip in active_clips(self.ctx, style):
                if clip["id"] == last_clip or clip["uses"] >= max_uses or clip["duration"] < duration:
                    continue
                start = free_start(clip["duration"], duration, usage(self.ctx.conn, clip["id"]), self.rng)
                if start is None:
                    continue
                return Segment(self.name, [Piece(clip["id"], clip["path"], start, duration)])
        # Pool empty for these styles: render one on demand (slower, but never blocks the pipeline).
        style = self._style_order(spec.styles or list(PROCEDURAL_STYLES))[0]
        log.info("procedural pool empty for %s; rendering on demand", style)
        clip_id = render_one(self.ctx, style, min_seconds=duration)
        clip = self.ctx.conn.execute("SELECT * FROM bg_clips WHERE id = ?", (clip_id,)).fetchone()
        return Segment(self.name, [Piece(clip_id, clip["path"], self.rng.uniform(0, clip["duration"] - duration), duration)])

    @staticmethod
    def retire_worn(ctx: Ctx) -> int:
        """Delete clips that reached max uses; fill_pool replaces them."""
        max_uses = ctx.config.background.max_uses_per_clip
        rows = ctx.conn.execute(
            "SELECT c.id FROM bg_clips c WHERE provider = 'procedural' AND retired_at IS NULL"
            " AND (SELECT COUNT(*) FROM bg_usage u WHERE u.clip_id = c.id) >= ?", (max_uses,)
        ).fetchall()
        for r in rows:
            retire_clip(ctx.conn, r["id"])
        return len(rows)
