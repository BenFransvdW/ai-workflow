"""Background from a folder of your own recordings (optional)."""

from __future__ import annotations

import logging
from pathlib import Path

from ... import media
from ...config import SeriesBackground
from .base import BackgroundProvider, NoBackgroundAvailable, Piece, Segment, free_start, last_usage, register_clip

log = logging.getLogger("shorts.background.local")
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}


class LocalFolderProvider(BackgroundProvider):
    name = "local_folder"

    def scan(self) -> list:
        folder = self.ctx.config.background.local_folder.path
        if folder is None:
            raise NoBackgroundAvailable("background.local_folder.path is not set")
        folder = self.ctx.config.path(folder)
        if not folder.is_dir():
            raise NoBackgroundAvailable(f"local folder {folder} does not exist")
        known = {r["path"] for r in self.ctx.conn.execute("SELECT path FROM bg_clips WHERE provider = 'local_folder'")}
        for f in sorted(folder.rglob("*")):
            if f.suffix.lower() in VIDEO_EXT and str(f) not in known:
                try:
                    w, h = media.video_size(f)
                    register_clip(self.ctx.conn, provider="local_folder", path=f, duration=media.duration(f),
                                  width=w, height=h, license="Own recording", external_id=str(f.relative_to(folder)))
                except media.FFmpegError as e:
                    log.warning("skipping unreadable clip %s: %s", f, e)
        return [r for r in self.ctx.conn.execute(
            "SELECT * FROM bg_clips WHERE provider = 'local_folder' AND retired_at IS NULL") if Path(r["path"]).exists()]

    def get_segment(self, duration: float, spec: SeriesBackground) -> Segment:
        clips = [c for c in self.scan() if c["duration"] >= duration]
        if not clips:
            raise NoBackgroundAvailable(f"no local clip is at least {duration:.1f}s long")
        prev = last_usage(self.ctx.conn, self.name)
        self.rng.shuffle(clips)
        # Prefer a different clip from last time; otherwise a non-overlapping segment of the same one.
        clips.sort(key=lambda c: prev is not None and c["id"] == prev["clip_id"])
        for c in clips:
            same_as_last = prev is not None and c["id"] == prev["clip_id"]
            used = [(prev["start"], prev["end"])] if same_as_last else []
            start = free_start(c["duration"], duration, used, self.rng)
            if start is not None:
                return Segment(self.name, [Piece(c["id"], c["path"], start, duration)])
        raise NoBackgroundAvailable("every local clip segment would repeat the previous one")
