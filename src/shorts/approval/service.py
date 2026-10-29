"""Approval logic, independent of Telegram so it can be tested and reused (CLI `status`)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db
from ..context import Ctx
from ..metadata import final_metadata
from ..records import count_groups_in, group_story, group_videos, state_counts
from ..schedule import next_upload_time, uploads_today
from ..state import PENDING_APPROVAL, State, group_state, transition_group

_PART_SUFFIX = re.compile(r"\s*\(Part \d+/\d+\)$")


class ApprovalError(RuntimeError):
    pass


@dataclass
class PartMedia:
    video_id: int
    path: Path
    part_index: int
    part_count: int
    duration: float


@dataclass
class Outgoing:
    group_id: int
    parts: list[PartMedia]
    summary: str


class ApprovalService:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.conn = ctx.conn
        self.cfg = ctx.config

    # ------------------------------------------------------------ outgoing
    def ready_to_send(self) -> list[int]:
        """Groups whose every part is rendered and that haven't been sent yet."""
        rows = self.conn.execute(
            "SELECT g.id FROM groups g WHERE g.sent_at IS NULL AND EXISTS (SELECT 1 FROM videos v WHERE v.group_id = g.id)"
            " AND NOT EXISTS (SELECT 1 FROM videos v WHERE v.group_id = g.id AND v.state != 'rendered') ORDER BY g.id"
        ).fetchall()
        return [r["id"] for r in rows]

    def outgoing(self, group_id: int) -> Outgoing:
        parts = []
        for v in group_videos(self.conn, group_id):
            path = Path(v["preview_path"] or v["video_path"])
            parts.append(PartMedia(v["id"], path, v["part_index"], v["part_count"], v["duration"] or 0))
        return Outgoing(group_id, parts, self.summary(group_id))

    def summary(self, group_id: int) -> str:
        group = self.conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        story = group_story(self.conn, group_id)
        videos = group_videos(self.conn, group_id)
        meta = final_metadata(self.conn, videos[0]["id"])
        series = self.cfg.series_by_name(group["series"])
        title = _PART_SUFFIX.sub("", meta["title"])
        expires = self.expires_at(group_id)
        lines = [
            f"🎬 #{group_id} · {series.display_name}" + (f" · {len(videos)} parts" if len(videos) > 1 else ""),
            f"Title: {title}",
            f"r/{story['subreddit']} · {story['url']}",
            f"Voice: {group['voice']} · Captions: {group['caption_style']} · Music: {group['music_bed']}",
            "",
            meta["description"],
            "",
            f"Expires {expires.astimezone(self.cfg.tz):%a %H:%M} unless approved. Reply \"title: …\" to rename.",
        ]
        return "\n".join(lines)[:4000]

    def mark_sent(self, group_id: int, message_id: int, video_message_ids: list[int]) -> None:
        transition_group(self.conn, group_id, State.AWAITING_APPROVAL, "sent to Telegram",
                         only_from={State.RENDERED})
        db.update(self.conn, "groups", group_id, tg_message_id=message_id, sent_at=db.now_iso(),
                  tg_video_message_ids=",".join(map(str, video_message_ids)))

    # ------------------------------------------------------------ actions
    def group_for_message(self, message_id: int) -> int | None:
        row = self.conn.execute("SELECT id FROM groups WHERE tg_message_id = ?", (message_id,)).fetchone()
        if row:
            return row["id"]
        for r in self.conn.execute("SELECT id, tg_video_message_ids FROM groups WHERE tg_video_message_ids IS NOT NULL"):
            if str(message_id) in (r["tg_video_message_ids"] or "").split(","):
                return r["id"]
        return None

    def _require(self, group_id: int, allowed: set[State]) -> State:
        st = group_state(self.conn, group_id)
        if st not in allowed:
            raise ApprovalError(f"#{group_id} is {st.value if st else 'unknown'}; nothing to do")
        return st

    def approve(self, group_id: int, now: datetime | None = None) -> datetime | None:
        self._require(group_id, {State.AWAITING_APPROVAL})
        transition_group(self.conn, group_id, State.APPROVED, "approved via Telegram",
                         only_from={State.AWAITING_APPROVAL}, approved_at=db.now_iso())
        return next_upload_time(self.conn, self.cfg, now or datetime.now(timezone.utc))

    def reject(self, group_id: int, reason: str | None = None) -> None:
        self._require(group_id, {State.AWAITING_APPROVAL, State.RENDERED})
        transition_group(self.conn, group_id, State.REJECTED, reason or "rejected via Telegram",
                         only_from={State.AWAITING_APPROVAL, State.RENDERED})
        db.update(self.conn, "groups", group_id, reject_reason=reason)
        story = group_story(self.conn, group_id)
        self.conn.execute("UPDATE stories SET status = 'rejected', filter_reason = ? WHERE id = ?",
                          (f"rejected by reviewer: {reason or 'no reason'}", story["id"]))

    def set_reject_reason(self, group_id: int, reason: str) -> None:
        db.update(self.conn, "groups", group_id, reject_reason=reason)
        story = group_story(self.conn, group_id)
        self.conn.execute("UPDATE stories SET filter_reason = ? WHERE id = ?",
                          (f"rejected by reviewer: {reason}", story["id"]))
        self.conn.execute("UPDATE video_events SET reason = ? WHERE to_state = 'rejected' AND video_id IN"
                          " (SELECT id FROM videos WHERE group_id = ?)", (f"rejected: {reason}", group_id))

    def regenerate(self, group_id: int) -> int:
        """Supersede this version and queue a fresh one (new script, voice, captions, background)."""
        from ..pipeline import regenerate_group

        self._require(group_id, {State.AWAITING_APPROVAL, State.RENDERED})
        transition_group(self.conn, group_id, State.SUPERSEDED, "regenerate requested",
                         only_from={State.AWAITING_APPROVAL, State.RENDERED})
        return regenerate_group(self.ctx, group_id)

    def edit_title(self, group_id: int, title: str) -> str:
        title = " ".join(title.split())
        if not title:
            raise ApprovalError("empty title")
        self._require(group_id, {State.AWAITING_APPROVAL, State.APPROVED, State.RENDERED})
        videos = group_videos(self.conn, group_id)
        n = len(videos)
        for v in videos:
            if v["state"] == State.UPLOADED.value:
                continue
            suffix = f" (Part {v['part_index']}/{n})" if n > 1 else ""
            db.update(self.conn, "videos", v["id"], title=(title[: 100 - len(suffix)] + suffix))
        return title

    # ------------------------------------------------------------ housekeeping
    def expires_at(self, group_id: int) -> datetime:
        g = self.conn.execute("SELECT sent_at, created_at FROM groups WHERE id = ?", (group_id,)).fetchone()
        start = db.parse_iso(g["sent_at"] or g["created_at"])
        return start + timedelta(hours=self.cfg.approval.window_hours)

    def expire(self, now: datetime | None = None) -> list[int]:
        """Expire groups that weren't approved in time. They are never uploaded."""
        now = now or datetime.now(timezone.utc)
        expired = []
        rows = self.conn.execute(
            "SELECT DISTINCT group_id FROM videos WHERE state IN ('rendered', 'awaiting_approval')").fetchall()
        for r in rows:
            if self.expires_at(r["group_id"]) <= now:
                transition_group(self.conn, r["group_id"], State.EXPIRED, "approval window elapsed",
                                 only_from={State.RENDERED, State.AWAITING_APPROVAL})
                expired.append(r["group_id"])
        return expired

    def pause(self) -> None:
        db.set_setting(self.conn, "paused", "1")

    def resume(self) -> None:
        db.set_setting(self.conn, "paused", "0")

    def status(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        counts = state_counts(self.conn)
        nxt = next_upload_time(self.conn, self.cfg, now)
        approved_ready = self.conn.execute("SELECT COUNT(*) FROM videos WHERE state = 'approved'").fetchone()[0]
        return {
            "paused": db.is_paused(self.conn),
            "counts": counts,
            "awaiting_approval_groups": count_groups_in(self.conn, {State.AWAITING_APPROVAL}),
            "backlog_groups": count_groups_in(self.conn, PENDING_APPROVAL),
            "backlog_target": self.cfg.approval.backlog_target,
            "approved_queue": approved_ready,
            "uploads_today": uploads_today(self.conn, self.cfg, now),
            "max_uploads_per_day": self.cfg.limits.max_uploads_per_day,
            "next_upload_slot": nxt.astimezone(self.cfg.tz).strftime("%a %d %b %H:%M %Z") if nxt else None,
            "dry_run": self.cfg.upload.dry_run,
            "candidate_stories": self.conn.execute("SELECT COUNT(*) FROM stories WHERE status='candidate'").fetchone()[0],
        }

    def status_text(self, now: datetime | None = None) -> str:
        s = self.status(now)
        c = s["counts"]
        lines = [
            "⏸ PAUSED" if s["paused"] else "▶️ Running",
            f"Awaiting approval: {s['awaiting_approval_groups']} (backlog {s['backlog_groups']}/{s['backlog_target']})",
            f"Approved, queued: {s['approved_queue']}",
            f"Uploaded today: {s['uploads_today']}/{s['max_uploads_per_day']}"
            + (" (dry run)" if s["dry_run"] else ""),
            f"Next upload slot: {s['next_upload_slot'] or 'none'}",
            f"Candidate stories: {s['candidate_stories']}",
            "States: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())) if c else "States: none yet",
        ]
        return "\n".join(lines)
