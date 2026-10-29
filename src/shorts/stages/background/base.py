"""Background provider interface and shared segment-selection helpers."""

from __future__ import annotations

import random
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from pathlib import Path

from ...config import SeriesBackground
from ...context import Ctx
from ...db import now_iso, utcnow


@dataclass
class Piece:
    clip_id: int
    path: str
    start: float
    duration: float


@dataclass
class Segment:
    """One or more clip pieces that are concatenated to cover the narration."""

    provider: str
    pieces: list[Piece]
    credits: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return sum(p.duration for p in self.pieces)

    def to_json(self) -> dict:
        return {"provider": self.provider, "pieces": [asdict(p) for p in self.pieces], "credits": self.credits}

    @classmethod
    def from_json(cls, d: dict) -> "Segment":
        return cls(provider=d["provider"], pieces=[Piece(**p) for p in d["pieces"]], credits=d.get("credits", []))


class NoBackgroundAvailable(RuntimeError):
    pass


class BackgroundProvider(ABC):
    name: str

    def __init__(self, ctx: Ctx, rng: random.Random | None = None):
        self.ctx = ctx
        self.rng = rng or random.Random()

    @abstractmethod
    def get_segment(self, duration: float, spec: SeriesBackground) -> Segment:
        """Return footage covering at least `duration` seconds."""


def overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return a0 < b1 and b0 < a1


def free_start(clip_len: float, need: float, used: list[tuple[float, float]], rng: random.Random,
               tries: int = 40) -> float | None:
    """A random start such that [start, start+need) fits in the clip and avoids all `used` intervals."""
    if need > clip_len:
        return None
    hi = clip_len - need
    if not used:
        return rng.uniform(0, hi)
    for _ in range(tries):
        s = rng.uniform(0, hi)
        if not any(overlaps(s, s + need, u0, u1) for u0, u1 in used):
            return s
    # Deterministic sweep over gaps as a fallback.
    edges = sorted({0.0, *[u1 for _, u1 in used]})
    for s in edges:
        if s <= hi and not any(overlaps(s, s + need, u0, u1) for u0, u1 in used):
            return s
    return None


def usage(conn: sqlite3.Connection, clip_id: int, within_days: float | None = None) -> list[tuple[float, float]]:
    if within_days is None:
        rows = conn.execute("SELECT start, end FROM bg_usage WHERE clip_id = ?", (clip_id,))
    else:
        since = (utcnow() - timedelta(days=within_days)).isoformat(timespec="seconds")
        rows = conn.execute("SELECT start, end FROM bg_usage WHERE clip_id = ? AND used_at >= ?", (clip_id, since))
    return [(r["start"], r["end"]) for r in rows]


def last_usage(conn: sqlite3.Connection, provider: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT u.* FROM bg_usage u JOIN bg_clips c ON c.id = u.clip_id WHERE c.provider = ? ORDER BY u.id DESC LIMIT 1",
        (provider,),
    ).fetchone()


def record_usage(conn: sqlite3.Connection, seg: Segment, video_id: int | None) -> None:
    ts = now_iso()
    for p in seg.pieces:
        conn.execute("INSERT INTO bg_usage(clip_id, start, end, video_id, used_at) VALUES (?, ?, ?, ?, ?)",
                     (p.clip_id, p.start, p.start + p.duration, video_id, ts))


def register_clip(conn: sqlite3.Connection, *, provider: str, path: Path, duration: float, style: str | None = None,
                  params_json: str | None = None, width: int | None = None, height: int | None = None,
                  source_url: str | None = None, author: str | None = None, author_url: str | None = None,
                  license: str | None = None, external_id: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO bg_clips(provider, style, params_json, path, duration, width, height, source_url, author,"
        " author_url, license, external_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (provider, style, params_json, str(path), duration, width, height, source_url, author, author_url, license,
         external_id, now_iso()),
    )
    return cur.lastrowid


def retire_clip(conn: sqlite3.Connection, clip_id: int) -> None:
    row = conn.execute("SELECT path FROM bg_clips WHERE id = ?", (clip_id,)).fetchone()
    if row:
        Path(row["path"]).unlink(missing_ok=True)
    conn.execute("UPDATE bg_clips SET retired_at = ? WHERE id = ?", (now_iso(), clip_id))
