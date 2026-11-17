"""Runtime health summary for the Telegram bot (`/health`), `shorts health`, and proactive alerts.

Builds on the offline `doctor` checks and adds signals that only show up while the pipeline
is running: repeated render/upload failures, videos that gave up after `limits.max_attempts`,
a YouTube OAuth token that has expired with no refresh token or was rejected by Google, a run
timer that stopped firing, and a nearly full data disk. Never touches the network.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import db
from .context import Ctx
from .doctor import FAIL, OK, WARN, Check, run_checks

_SEEN_KEY = "health_failed_event_seen"
LAST_RUN_KEY = "last_run_at"
_COND_SIG_KEY = "health_condition_sig"
_COND_AT_KEY = "health_condition_at"
_TOKEN_ERRORS = ("invalid_grant", "refresherror", "token has been expired or revoked")


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
    h = ctx.config.health
    window = f"{h.failure_window_hours:g}h"
    recent = ctx.conn.execute(
        "SELECT COUNT(*) FROM video_events WHERE to_state = 'failed' AND at >= ?", (_since(now, h.failure_window_hours),)
    ).fetchone()[0]
    if recent >= h.failure_threshold:
        out.append(Check(WARN, "repeated failures", f"{recent} failures in the last {window}; check the pipeline logs"))
    elif recent:
        out.append(Check(OK, "recent failures", f"{recent} in the last {window} (retrying)"))
    else:
        out.append(Check(OK, "recent failures", f"none in the last {window}"))
    return out


def _since(now: datetime, hours: float) -> str:
    return (now - timedelta(hours=hours)).astimezone(timezone.utc).isoformat(timespec="seconds")


def mark_run(ctx: Ctx, now: datetime | None = None) -> None:
    """Called by every completed pipeline pass so /health can notice when the timer stops firing."""
    now = now or datetime.now(timezone.utc)
    db.set_setting(ctx.conn, LAST_RUN_KEY, now.astimezone(timezone.utc).isoformat(timespec="seconds"))


def _run_check(ctx: Ctx, now: datetime) -> Check | None:
    if db.is_paused(ctx.conn):
        return None
    last = db.get_setting(ctx.conn, LAST_RUN_KEY)
    if not last:
        return None  # fresh install: nothing has run yet
    age = now - db.parse_iso(last)
    if age > timedelta(hours=ctx.config.health.stale_run_hours):
        return Check(WARN, "pipeline run", f"no completed run for {age.total_seconds() / 3600:.0f}h; "
                                           "is the systemd timer active, or is the run crashing?")
    return Check(OK, "pipeline run", f"last completed {age.total_seconds() / 60:.0f} min ago")


def _disk_check(ctx: Ctx) -> Check | None:
    target = ctx.data_dir if ctx.data_dir.exists() else ctx.config.root
    try:
        free_gb = shutil.disk_usage(target).free / 1e9
    except OSError:
        return None
    if free_gb < ctx.config.health.min_free_gb:
        return Check(WARN, "disk space", f"only {free_gb:.1f} GB free on the data disk")
    return Check(OK, "disk space", f"{free_gb:.1f} GB free")


def _token_rejected_check(ctx: Ctx, now: datetime) -> Check | None:
    """A refresh token revoked by Google only shows up as a failed upload; spot that error text."""
    if ctx.config.upload.dry_run:
        return None
    rows = ctx.conn.execute(
        "SELECT reason FROM video_events WHERE to_state = 'failed' AND reason LIKE 'upload:%' AND at >= ?",
        (_since(now, ctx.config.health.failure_window_hours),)).fetchall()
    if any(t in (r["reason"] or "").lower() for r in rows for t in _TOKEN_ERRORS):
        return Check(FAIL, "youtube token rejected",
                     "Google refused the token (expired or revoked); rerun `python -m shorts auth-youtube`")
    return None


def condition_checks(ctx: Ctx, now: datetime | None = None) -> list[Check]:
    """State-based problems that don't produce a new failure event on their own (dead timer, full disk)."""
    now = now or datetime.now(timezone.utc)
    return [c for c in (_run_check(ctx, now), _disk_check(ctx), _token_rejected_check(ctx, now)) if c]


def run_health(ctx: Ctx, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now(timezone.utc)
    checks = _failure_checks(ctx, now)
    tok = _token_check(ctx, now)
    if tok:
        checks.append(tok)
    checks.extend(condition_checks(ctx, now))
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


def condition_alert(ctx: Ctx, now: datetime | None = None) -> str | None:
    """Push text for stale-run / low-disk / rejected-token problems, or None. Debounced.

    Sends when a new problem appears, repeats every `health.alert_every_hours` while problems
    persist, and sends one all-clear once they're gone. New failures are covered by
    `new_failure_alert`; this catches the problems that produce no failure event.
    """
    if not ctx.config.health.condition_alerts:
        return None
    now = now or datetime.now(timezone.utc)
    bad = [c for c in condition_checks(ctx, now) if c.level != OK]
    names = {c.name for c in bad}
    prev = {n for n in (db.get_setting(ctx.conn, _COND_SIG_KEY) or "").split(",") if n}
    db.set_setting(ctx.conn, _COND_SIG_KEY, ",".join(sorted(names)))
    if not bad:
        return "✅ Health back to normal." if prev else None
    last = db.get_setting(ctx.conn, _COND_AT_KEY)
    overdue = last is None or now - db.parse_iso(last) >= timedelta(hours=ctx.config.health.alert_every_hours)
    if not (names - prev) and not overdue:
        return None
    db.set_setting(ctx.conn, _COND_AT_KEY, now.astimezone(timezone.utc).isoformat(timespec="seconds"))
    icon = {FAIL: "❌", WARN: "⚠️"}
    return "\n".join(["Health alert:"] + [f"{icon[c.level]} {c.name}: {c.detail}" for c in bad]
                     + ["Send /health for a full check."])
