"""Runtime health summary for the Telegram bot (`/health`) and proactive failure alerts.

Builds on the offline `doctor` checks and adds signals that only show up while the pipeline
is running: repeated render/upload failures, videos that gave up after `limits.max_attempts`,
and a YouTube OAuth token that has expired with no refresh token. Never touches the network.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .context import Ctx
from .doctor import FAIL, OK, WARN, Check, run_checks

# Failures within this window that trigger a "repeated failures" warning.
FAILURE_WINDOW = timedelta(hours=24)
REPEATED_FAILURES = 3
_SEEN_KEY = "health_failed_event_seen"


def _token_check(ctx: Ctx, now: datetime) -> Check | None:
    """Inspect the saved OAuth token offline. Expired access tokens are fine if a refresh token exists."""
    path = ctx.secrets.get("YOUTUBE_TOKEN_FILE")
    if not path or not Path(path).exists():
        return None  # doctor already reports a missing token
    level = WARN if ctx.config.upload.dry_run else FAIL
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as e:
        return Check(level, "youtube token", f"unreadable ({type(e).__name__}); rerun `python -m shorts auth-youtube`")
    if data.get("refresh_token"):
        return Check(OK, "youtube token", "has refresh token")
    expiry = data.get("expiry")
    if expiry:
        try:
            exp = db.parse_iso(expiry.replace("Z", "+00:00"))
        except ValueError:
            exp = None
        if exp is not None and exp <= now:
            return Check(level, "youtube token",
                         f"expired {exp:%Y-%m-%d %H:%M} UTC with no refresh token; rerun `python -m shorts auth-youtube`")
    return Check(WARN, "youtube token", "no refresh token; it will stop working when it expires")


def _failure_checks(ctx: Ctx, now: datetime) -> list[Check]:
    out = []
    max_attempts = ctx.config.limits.max_attempts
    gave_up = ctx.conn.execute(
        "SELECT id, group_id FROM videos WHERE state = 'failed' AND attempts >= ? ORDER BY id", (max_attempts,)
    ).fetchall()
    if gave_up:
        groups = sorted({r["group_id"] for r in gave_up})
        out.append(Check(FAIL, "gave up", f"{len(gave_up)} video(s) failed {max_attempts}× and won't retry: "
                                          + ", ".join(f"#{g}" for g in groups)))
    since = (now - FAILURE_WINDOW).astimezone(timezone.utc).isoformat(timespec="seconds")
    recent = ctx.conn.execute(
        "SELECT COUNT(*) FROM video_events WHERE to_state = 'failed' AND at >= ?", (since,)
    ).fetchone()[0]
    if recent >= REPEATED_FAILURES:
        out.append(Check(WARN, "repeated failures", f"{recent} failures in the last 24h; check the pipeline logs"))
    elif recent:
        out.append(Check(OK, "recent failures", f"{recent} in the last 24h (retrying)"))
    else:
        out.append(Check(OK, "recent failures", "none in the last 24h"))
    return out


def run_health(ctx: Ctx, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now(timezone.utc)
    checks = _failure_checks(ctx, now)
    tok = _token_check(ctx, now)
    if tok:
        checks.append(tok)
    checks.extend(run_checks(ctx))
    return checks


def health_text(ctx: Ctx, now: datetime | None = None) -> str:
    """Short Telegram-friendly summary: problems first, OK checks collapsed into a count."""
    checks = run_health(ctx, now)
    fails = [c for c in checks if c.level == FAIL]
    warns = [c for c in checks if c.level == WARN]
    n_ok = len(checks) - len(fails) - len(warns)
    head = "🔴 Problems found" if fails else ("🟡 Warnings" if warns else "🟢 All good")
    lines = [head]
    lines += [f"❌ {c.name}: {c.detail}" if c.detail else f"❌ {c.name}" for c in fails]
    lines += [f"⚠️ {c.name}: {c.detail}" if c.detail else f"⚠️ {c.name}" for c in warns]
    lines.append(f"✅ {n_ok} check(s) ok")
    return "\n".join(lines)[:4000]


def new_failure_alert(ctx: Ctx) -> str | None:
    """Text describing failures recorded since the last call, or None.

    The first call only records a baseline, so old history isn't replayed when the bot starts.
    """
    seen = db.get_setting(ctx.conn, _SEEN_KEY)
    latest = ctx.conn.execute("SELECT COALESCE(MAX(id), 0) FROM video_events").fetchone()[0]
    if seen is None:
        db.set_setting(ctx.conn, _SEEN_KEY, str(latest))
        return None
    rows = ctx.conn.execute(
        "SELECT e.video_id, e.reason, v.group_id, v.attempts FROM video_events e JOIN videos v ON v.id = e.video_id"
        " WHERE e.to_state = 'failed' AND e.id > ? ORDER BY e.id", (int(seen),)
    ).fetchall()
    db.set_setting(ctx.conn, _SEEN_KEY, str(latest))
    if not rows:
        return None
    max_attempts = ctx.config.limits.max_attempts
    lines = [f"⚠️ {len(rows)} new failure(s):"]
    for r in rows[:10]:
        tail = "giving up" if r["attempts"] >= max_attempts else f"attempt {r['attempts']}/{max_attempts}, will retry"
        reason = (r["reason"] or "").splitlines()[0][:150] if r["reason"] else "no reason recorded"
        lines.append(f"• #{r['group_id']} (video {r['video_id']}, {tail}): {reason}")
    if len(rows) > 10:
        lines.append(f"…and {len(rows) - 10} more")
    lines.append("Send /health for a full check.")
    return "\n".join(lines)
