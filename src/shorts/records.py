"""Creating and querying stories, groups and videos."""

from __future__ import annotations

import sqlite3

from .db import now_iso
from .state import State


def create_group(
    conn: sqlite3.Connection,
    story_id: int,
    series: str,
    *,
    voice: str | None = None,
    caption_style: str | None = None,
    music_bed: str | None = None,
    regen_of: int | None = None,
) -> int:
    """Create a group with its first (candidate) part. More parts are added by the script stage."""
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO groups(story_id, series, voice, caption_style, music_bed, regen_of, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (story_id, series, voice, caption_style, music_bed, regen_of, ts),
    )
    group_id = cur.lastrowid
    create_video(conn, group_id, 1, 1)
    conn.execute("UPDATE stories SET status = 'in_use' WHERE id = ? AND status = 'candidate'", (story_id,))
    return group_id


def create_video(conn: sqlite3.Connection, group_id: int, part_index: int, part_count: int) -> int:
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO videos(group_id, part_index, part_count, state, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (group_id, part_index, part_count, State.CANDIDATE.value, ts, ts),
    )
    conn.execute(
        "INSERT INTO video_events(video_id, from_state, to_state, reason, at) VALUES (?, NULL, ?, 'created', ?)",
        (cur.lastrowid, State.CANDIDATE.value, ts),
    )
    return cur.lastrowid


def group_videos(conn: sqlite3.Connection, group_id: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM videos WHERE group_id = ? ORDER BY part_index", (group_id,)).fetchall()


def group_story(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row:
    return conn.execute(
        "SELECT s.* FROM stories s JOIN groups g ON g.story_id = s.id WHERE g.id = ?", (group_id,)
    ).fetchone()


def video_context(conn: sqlite3.Connection, video_id: int) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    """(video, group, story) for a video id."""
    video = conn.execute("SELECT * FROM videos WHERE id = ?", (video_id,)).fetchone()
    if video is None:
        raise KeyError(f"video {video_id} not found")
    group = conn.execute("SELECT * FROM groups WHERE id = ?", (video["group_id"],)).fetchone()
    story = conn.execute("SELECT * FROM stories WHERE id = ?", (group["story_id"],)).fetchone()
    return video, group, story


def count_groups_in(conn: sqlite3.Connection, states: set[State] | frozenset[State]) -> int:
    marks = ",".join("?" for _ in states)
    return conn.execute(
        f"SELECT COUNT(DISTINCT group_id) FROM videos WHERE state IN ({marks})", [s.value for s in states]
    ).fetchone()[0]


def state_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM videos GROUP BY state")}

