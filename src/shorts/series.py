"""Series rotation: which format the next video uses, and the voice/style/music within it.

Each series is a consistent channel format; within a series we rotate voices, caption styles
and music beds so consecutive videos are never identical clones.
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass

from .config import Config, SeriesConfig


@dataclass
class Selection:
    series: SeriesConfig
    story_id: int
    voice: str
    caption_style: str
    music_bed: str


def _least_recent(conn: sqlite3.Connection, column: str, options: list[str], series: str | None, rng: random.Random) -> str:
    """Pick the option used least recently (never-used first, ties broken randomly)."""
    where, args = ("WHERE series = ?", [series]) if series else ("", [])
    last_used: dict[str, int] = {}
    for row in conn.execute(f"SELECT {column} AS v, MAX(id) AS last FROM groups {where} GROUP BY {column}", args):
        if row["v"] is not None:
            last_used[row["v"]] = row["last"]
    shuffled = options[:]
    rng.shuffle(shuffled)
    return min(shuffled, key=lambda o: last_used.get(o, -1))


def candidate_story(conn: sqlite3.Connection, series: SeriesConfig, rng: random.Random) -> int | None:
    subs = [s.lower() for s in series.subreddits]
    rows = conn.execute("SELECT id, subreddit, score FROM stories WHERE status = 'candidate' ORDER BY score DESC").fetchall()
    rows = [r for r in rows if not subs or r["subreddit"].lower() in subs]
    if not rows:
        return None
    # Prefer high-scoring stories but keep some randomness.
    top = rows[: max(3, len(rows) // 4)]
    return rng.choice(top)["id"]


def choose(conn: sqlite3.Connection, cfg: Config, rng: random.Random | None = None) -> Selection | None:
    """Pick the next series (least recently used that has a story available) and its options."""
    rng = rng or random.Random()
    names = [s.name for s in cfg.series]
    tried: set[str] = set()
    while len(tried) < len(names):
        name = _least_recent(conn, "series", [n for n in names if n not in tried], None, rng)
        tried.add(name)
        series = cfg.series_by_name(name)
        story_id = candidate_story(conn, series, rng)
        if story_id is None:
            continue
        return Selection(
            series=series,
            story_id=story_id,
            voice=_least_recent(conn, "voice", series.voices, name, rng),
            caption_style=_least_recent(conn, "caption_style", series.caption_styles, name, rng),
            music_bed=_least_recent(conn, "music_bed", series.music, name, rng),
        )
    return None
