"""Free-licensed vertical stock footage from the Pexels API, cached locally with licence metadata.

Only the official API is used (https://www.pexels.com/api/documentation/#videos-search); clips are
covered by the Pexels License (free for commercial use, no attribution required, but we credit
the creator in the description anyway).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import requests

from ... import media
from ...config import SeriesBackground
from ...context import Ctx
from .base import BackgroundProvider, NoBackgroundAvailable, Piece, Segment, free_start, register_clip, retire_clip, usage

log = logging.getLogger("shorts.background.pexels")
API = "https://api.pexels.com/videos/search"
LICENSE = "Pexels License (https://www.pexels.com/license/)"


def pick_file(video: dict, min_height: int) -> dict | None:
    """Best portrait mp4 rendition: the smallest one at least `min_height` tall, else the tallest."""
    files = [f for f in video.get("video_files", [])
             if f.get("file_type") == "video/mp4" and f.get("height") and f.get("width")
             and f["height"] > f["width"]]
    if not files:
        return None
    big = sorted((f for f in files if f["height"] >= min_height), key=lambda f: f["height"])
    return big[0] if big else max(files, key=lambda f: f["height"])


class PexelsProvider(BackgroundProvider):
    name = "pexels"

    def __init__(self, ctx: Ctx, rng=None, session: requests.Session | None = None):
        super().__init__(ctx, rng)
        self.session = session or requests.Session()
        self.cfg = ctx.config.background.pexels

    # ------------------------------------------------------------ API + cache
    def search(self, theme: str, page: int = 1) -> list[dict]:
        key = self.ctx.secrets.require("PEXELS_API_KEY")
        resp = self.session.get(API, headers={"Authorization": key}, timeout=30, params={
            "query": theme, "orientation": "portrait", "size": "medium", "per_page": self.cfg.per_page, "page": page,
        })
        resp.raise_for_status()
        return resp.json().get("videos", [])

    def download(self, video: dict, theme: str) -> int | None:
        exists = self.ctx.conn.execute(
            "SELECT id FROM bg_clips WHERE provider = 'pexels' AND external_id = ?", (str(video["id"]),)
        ).fetchone()
        if exists:
            return None
        f = pick_file(video, self.cfg.min_height)
        if f is None:
            return None
        out = self.ctx.work_dir("bg_cache", "pexels") / f"{video['id']}_{f['height']}.mp4"
        tmp = out.with_suffix(".part")
        with self.session.get(f["link"], stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_content(1 << 16):
                    fh.write(chunk)
        tmp.replace(out)
        try:
            dur = media.duration(out)
        except media.FFmpegError:
            dur = float(video.get("duration") or 0)
        user = video.get("user") or {}
        meta = {"theme": theme, "file_id": f.get("id"), "quality": f.get("quality"), "fps": f.get("fps"),
                "image": video.get("image")}
        clip_id = register_clip(
            self.ctx.conn, provider="pexels", path=out, duration=dur, style=theme, params_json=json.dumps(meta),
            width=f["width"], height=f["height"], source_url=video.get("url"), author=user.get("name"),
            author_url=user.get("url"), license=LICENSE, external_id=str(video["id"]),
        )
        log.info("cached pexels video %s by %s (%s, %.1fs)", video["id"], user.get("name"), theme, dur)
        return clip_id

    def refresh(self, theme: str, want: int = 3) -> int:
        added = 0
        for page in (1, 2, 3):
            for v in self.search(theme, page):
                if self.download(v, theme):
                    added += 1
                    if added >= want:
                        self.evict()
                        return added
        self.evict()
        return added

    def evict(self) -> None:
        """Keep the cache bounded: drop least-recently-used clips beyond max_cached_clips."""
        rows = self.ctx.conn.execute(
            "SELECT c.id, COALESCE((SELECT MAX(used_at) FROM bg_usage u WHERE u.clip_id = c.id), c.created_at) AS last"
            " FROM bg_clips c WHERE provider = 'pexels' AND retired_at IS NULL ORDER BY last DESC"
        ).fetchall()
        for r in rows[self.cfg.max_cached_clips:]:
            retire_clip(self.ctx.conn, r["id"])

    # ------------------------------------------------------------ selection
    def _clips(self, themes: list[str]):
        marks = ",".join("?" for _ in themes)
        return [r for r in self.ctx.conn.execute(
            f"SELECT * FROM bg_clips WHERE provider = 'pexels' AND retired_at IS NULL AND style IN ({marks})", themes)
            if Path(r["path"]).exists()]

    def _assemble(self, clips, duration: float) -> Segment | None:
        """Use one long-enough clip, or chain several short ones; never reuse a segment within reuse_days."""
        self.rng.shuffle(clips)
        clips.sort(key=lambda c: -c["duration"] if c["duration"] >= duration else 0)
        pieces: list[Piece] = []
        credits: list[str] = []
        remaining = duration
        for c in clips:
            if remaining <= 0.05:
                break
            used = usage(self.ctx.conn, c["id"], within_days=self.cfg.reuse_days)
            take = min(remaining, c["duration"])
            if take < remaining and take < 3:
                continue  # too short to be worth a cut
            start = free_start(c["duration"], take, used, self.rng)
            if start is None:
                continue
            pieces.append(Piece(c["id"], c["path"], start, take))
            credit = f"Background video by {c['author']} on Pexels ({c['source_url']})"
            if credit not in credits:
                credits.append(credit)
            remaining -= take
        if remaining > 0.05:
            return None
        return Segment(self.name, pieces, credits)

    def get_segment(self, duration: float, spec: SeriesBackground) -> Segment:
        themes = spec.themes or self.cfg.themes
        seg = self._assemble(self._clips(themes), duration)
        if seg is None:
            theme = self.rng.choice(themes)
            if not self.refresh(theme):
                log.info("no new pexels clips for %r", theme)
            seg = self._assemble(self._clips(themes), duration)
        if seg is None:
            raise NoBackgroundAvailable(f"not enough unused Pexels footage for themes {themes}")
        return seg
