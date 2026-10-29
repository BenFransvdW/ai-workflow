"""Orchestrator: one pass, safe to call from a systemd timer (non-blocking lockfile).

1. expire unapproved videos past the approval window
2. stop here if paused (/pause in Telegram)
3. upload at most one approved video if a posting slot is open and under the daily cap
4. regenerate requests and failed-render retries
5. top up the approval backlog (fetch more stories when candidates run low)
6. refresh the procedural background pool with any time left
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .approval.service import ApprovalService
from .context import Ctx
from .db import is_paused
from .lock import FileLock, LockHeld
from .pipeline import new_group, produce, retry_failed
from .records import count_groups_in
from .state import PENDING_APPROVAL, State

log = logging.getLogger("shorts.run")


def _pending_groups(ctx: Ctx) -> list[int]:
    """Groups with parts still to script/render (new regenerations, retries), oldest first."""
    return [r["group_id"] for r in ctx.conn.execute(
        "SELECT DISTINCT group_id FROM videos WHERE state IN ('candidate', 'scripted') ORDER BY group_id")]


def top_up(ctx: Ctx, *, client=None, use_whisper: bool = True, fetcher=None) -> int:
    """Render until the approval backlog reaches its target (bounded per run). Returns renders done."""
    from .stages.fetch import candidate_count, fetch

    limits, approval = ctx.config.limits, ctx.config.approval
    renders = 0
    retry_failed(ctx)
    for gid in _pending_groups(ctx):
        if renders >= limits.max_renders_per_run:
            return renders
        produce(ctx, gid, client=client, use_whisper=use_whisper)
        renders += 1
    approved_queue = count_groups_in(ctx.conn, {State.APPROVED})
    if approved_queue >= limits.max_uploads_per_day * 3:
        log.info("approved queue already holds %d groups; not generating more", approved_queue)
        return renders
    while renders < limits.max_renders_per_run and count_groups_in(ctx.conn, PENDING_APPROVAL) < approval.backlog_target:
        if candidate_count(ctx) < ctx.config.reddit.min_candidates:
            try:
                (fetcher or fetch)(ctx)
            except Exception as e:
                log.warning("fetch failed: %s", e)
        gid = new_group(ctx)
        if gid is None:
            log.warning("no candidate stories available")
            break
        produce(ctx, gid, client=client, use_whisper=use_whisper)
        renders += 1
    return renders


def run_once(ctx: Ctx, *, live: bool = False, generate: bool = True, now: datetime | None = None,
             client=None, use_whisper: bool = True, fetcher=None, uploader=None,
             pool_budget_seconds: float = 900) -> int:
    from .stages.background.pool import ProceduralProvider, fill_pool
    from .stages.upload import process_upload_queue

    started = time.monotonic()
    try:
        lock = FileLock(ctx.data_dir / "run.lock").acquire()
    except LockHeld as e:
        log.info("%s; exiting", e)
        return 0
    try:
        now = now or datetime.now(timezone.utc)
        svc = ApprovalService(ctx)
        expired = svc.expire(now)
        if expired:
            log.info("expired groups: %s", expired)
        if is_paused(ctx.conn):
            log.info("paused; skipping generation and uploads")
            return 0
        dry = not live and ctx.config.upload.dry_run
        (uploader or process_upload_queue)(ctx, dry_run=dry, now=now)
        if generate:
            n = top_up(ctx, client=client, use_whisper=use_whisper, fetcher=fetcher)
            log.info("rendered %d group(s) this run", n)
            ProceduralProvider.retire_worn(ctx)
            fill_pool(ctx, deadline=started + pool_budget_seconds)
        log.info("run finished in %.0fs: %s", time.monotonic() - started, svc.status_text(now).replace("\n", " | "))
        return 0
    finally:
        lock.release()
