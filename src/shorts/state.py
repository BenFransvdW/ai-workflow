"""Video state machine.

candidate → scripted → rendered → awaiting_approval → approved → uploaded
                                                    ↘ rejected   ↘ failed
                                                    ↘ expired
                                                    ↘ superseded (Regenerate)
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from .db import now_iso


class State(StrEnum):
    CANDIDATE = "candidate"
    SCRIPTED = "scripted"
    RENDERED = "rendered"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    UPLOADED = "uploaded"
    FAILED = "failed"


S = State
TRANSITIONS: dict[State, frozenset[State]] = {
    S.CANDIDATE: frozenset({S.SCRIPTED, S.REJECTED, S.FAILED, S.SUPERSEDED}),
    S.SCRIPTED: frozenset({S.RENDERED, S.FAILED, S.SUPERSEDED}),
    S.RENDERED: frozenset({S.AWAITING_APPROVAL, S.FAILED, S.EXPIRED, S.SUPERSEDED, S.REJECTED}),
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.REJECTED, S.EXPIRED, S.SUPERSEDED}),
    S.APPROVED: frozenset({S.UPLOADED, S.FAILED}),
    # A failed render can be retried from the stage it reached; a failed upload goes back to approved.
    S.FAILED: frozenset({S.CANDIDATE, S.SCRIPTED, S.APPROVED, S.SUPERSEDED, S.EXPIRED}),
    S.REJECTED: frozenset(),
    S.EXPIRED: frozenset(),
    S.SUPERSEDED: frozenset(),
    S.UPLOADED: frozenset(),
}

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)
# States in which a group still counts toward the approval backlog.
PENDING_APPROVAL = frozenset({S.CANDIDATE, S.SCRIPTED, S.RENDERED, S.AWAITING_APPROVAL})


class InvalidTransition(ValueError):
    pass


def can_transition(src: State | str, dst: State | str) -> bool:
    return State(dst) in TRANSITIONS[State(src)]


def transition(
    conn: sqlite3.Connection, video_id: int, dst: State | str, reason: str | None = None, **fields
) -> None:
    """Move one video to `dst`, validating the edge and recording an audit event."""
    dst = State(dst)
    row = conn.execute("SELECT state FROM videos WHERE id = ?", (video_id,)).fetchone()
    if row is None:
        raise KeyError(f"video {video_id} not found")
    src = State(row["state"])
    if not can_transition(src, dst):
        raise InvalidTransition(f"video {video_id}: {src} → {dst} is not allowed")
    ts = now_iso()
    fields = {"state": dst.value, "updated_at": ts, **fields}
    if dst != S.FAILED:
        fields.setdefault("error", None)
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            f"UPDATE videos SET {cols} WHERE id = ? AND state = ?", (*fields.values(), video_id, src.value)
        )
        if cur.rowcount != 1:
            raise InvalidTransition(f"video {video_id} changed state concurrently")
        conn.execute(
            "INSERT INTO video_events(video_id, from_state, to_state, reason, at) VALUES (?, ?, ?, ?, ?)",
            (video_id, src.value, dst.value, reason, ts),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def transition_group(
    conn: sqlite3.Connection,
    group_id: int,
    dst: State | str,
    reason: str | None = None,
    only_from: set[State] | None = None,
    **fields,
) -> list[int]:
    """Transition every part of a group (optionally only those in `only_from`). Returns moved ids."""
    moved = []
    for row in conn.execute(
        "SELECT id, state FROM videos WHERE group_id = ? ORDER BY part_index", (group_id,)
    ).fetchall():
        if only_from is not None and State(row["state"]) not in only_from:
            continue
        transition(conn, row["id"], dst, reason, **fields)
        moved.append(row["id"])
    return moved


def group_state(conn: sqlite3.Connection, group_id: int) -> State | None:
    """The least-advanced state across a group's parts (they move together until upload)."""
    order = list(State)
    states = [State(r["state"]) for r in conn.execute("SELECT state FROM videos WHERE group_id = ?", (group_id,))]
    if not states:
        return None
    return min(states, key=order.index)


def history(conn: sqlite3.Connection, video_id: int) -> list[tuple[str | None, str]]:
    return [
        (r["from_state"], r["to_state"])
        for r in conn.execute("SELECT from_state, to_state FROM video_events WHERE video_id = ? ORDER BY id", (video_id,))
    ]
