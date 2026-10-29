"""Posting slots and the daily upload cap."""

from __future__ import annotations

import sqlite3
from datetime import datetime, time, timedelta, timezone

from .config import Config
from .db import parse_iso


def _slots_for_day(cfg: Config, day: datetime) -> list[datetime]:
    out = []
    for t in cfg.limits.posting_times:
        hh, mm = map(int, t.split(":"))
        out.append(datetime.combine(day.date(), time(hh, mm), tzinfo=cfg.tz))
    return out


def local_day_start(cfg: Config, now: datetime) -> datetime:
    local = now.astimezone(cfg.tz)
    return datetime.combine(local.date(), time(0, 0), tzinfo=cfg.tz)


def uploads_today(conn: sqlite3.Connection, cfg: Config, now: datetime) -> int:
    since = local_day_start(cfg, now).astimezone(timezone.utc).isoformat(timespec="seconds")
    return conn.execute(
        "SELECT COUNT(*) FROM videos WHERE state = 'uploaded' AND uploaded_at >= ?", (since,)
    ).fetchone()[0]


def last_upload(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT MAX(uploaded_at) AS t FROM videos WHERE state = 'uploaded'").fetchone()
    return parse_iso(row["t"]) if row and row["t"] else None


def can_upload_now(conn: sqlite3.Connection, cfg: Config, now: datetime) -> tuple[bool, str]:
    """True when a posting slot has passed today that hasn't been used yet and we're under the cap."""
    now = now.astimezone(cfg.tz)
    done = uploads_today(conn, cfg, now)
    cap = cfg.limits.max_uploads_per_day
    if done >= cap:
        return False, f"daily cap reached ({done}/{cap})"
    passed = [s for s in _slots_for_day(cfg, now) if s <= now]
    if not passed:
        return False, "no posting slot has passed yet today"
    if done >= len(passed):
        return False, "all passed slots already used"
    last = last_upload(conn)
    if last is not None and last >= passed[-1]:
        return False, "current slot already used"
    return True, f"slot {passed[-1].strftime('%H:%M')} open ({done}/{cap} today)"


def next_upload_time(conn: sqlite3.Connection, cfg: Config, now: datetime) -> datetime | None:
    """Earliest time an upload could happen (ignores whether anything is approved)."""
    ok, _ = can_upload_now(conn, cfg, now)
    if ok:
        return now.astimezone(cfg.tz)
    local = now.astimezone(cfg.tz)
    done = uploads_today(conn, cfg, now)
    if done < cfg.limits.max_uploads_per_day:
        for s in _slots_for_day(cfg, local):
            if s > local:
                return s
    tomorrow = local + timedelta(days=1)
    slots = _slots_for_day(cfg, tomorrow)
    return slots[0] if slots else None
