from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from shorts import health, records
from shorts.approval.bot import ApprovalBot
from shorts.doctor import FAIL, OK, WARN
from shorts.state import State, transition
from tests.conftest import make_group
from tests.test_approval import CHAT, FakeBot

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _fail(ctx, attempts=1, reason="RuntimeError: ffmpeg exited 1"):
    gid = make_group(ctx, "rendered")
    vid = records.group_videos(ctx.conn, gid)[0]["id"]
    transition(ctx.conn, vid, State.FAILED, reason, attempts=attempts)
    return gid, vid


def _by_name(checks):
    return {c.name: c for c in checks}


def test_no_failures_is_ok(ctx):
    c = _by_name(health.run_health(ctx, datetime.now(timezone.utc)))
    assert c["recent failures"].level == OK
    assert "gave up" not in c


def test_exhausted_retries_fail(ctx):
    gid, _ = _fail(ctx, attempts=ctx.config.limits.max_attempts)
    c = _by_name(health.run_health(ctx, datetime.now(timezone.utc)))
    assert c["gave up"].level == FAIL and f"#{gid}" in c["gave up"].detail


def test_repeated_failures_warn(ctx):
    for _ in range(health.REPEATED_FAILURES):
        _fail(ctx)
    c = _by_name(health.run_health(ctx, datetime.now(timezone.utc)))
    assert c["repeated failures"].level == WARN
    # Outside the 24h window they no longer count.
    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert _by_name(health.run_health(ctx, later))["recent failures"].level == OK


def _token(tmp_path, monkeypatch, **data):
    p = tmp_path / "token.json"
    p.write_text(json.dumps(data))
    monkeypatch.setenv("YOUTUBE_TOKEN_FILE", str(p))
    return p


def test_token_with_refresh_is_ok(ctx, tmp_path, monkeypatch):
    _token(tmp_path, monkeypatch, token="t", refresh_token="r", expiry="2020-01-01T00:00:00Z")
    assert health._token_check(ctx, NOW).level == OK


def test_expired_token_without_refresh(ctx, tmp_path, monkeypatch):
    _token(tmp_path, monkeypatch, token="t", expiry="2026-11-01T00:00:00.000000Z")
    ctx.config.upload.dry_run = True
    assert health._token_check(ctx, NOW).level == WARN
    ctx.config.upload.dry_run = False
    chk = health._token_check(ctx, NOW)
    assert chk.level == FAIL and "auth-youtube" in chk.detail


def test_unreadable_token(ctx, tmp_path, monkeypatch):
    p = tmp_path / "token.json"
    p.write_text("{not json")
    monkeypatch.setenv("YOUTUBE_TOKEN_FILE", str(p))
    assert health._token_check(ctx, NOW).level == WARN


def test_missing_token_left_to_doctor(ctx, monkeypatch):
    monkeypatch.delenv("YOUTUBE_TOKEN_FILE", raising=False)
    assert health._token_check(ctx, NOW) is None


def test_health_text_orders_problems_first(ctx):
    _fail(ctx, attempts=ctx.config.limits.max_attempts)
    text = health.health_text(ctx)
    lines = text.splitlines()
    assert lines[0] == "🔴 Problems found"
    assert lines[1].startswith("❌ gave up")
    assert lines[-1].endswith("check(s) ok")


def test_failure_alert_baseline_then_new_only(ctx):
    _fail(ctx)  # history before the bot started: not replayed
    assert health.new_failure_alert(ctx) is None
    assert health.new_failure_alert(ctx) is None
    gid, vid = _fail(ctx, attempts=ctx.config.limits.max_attempts, reason="upload: quota exceeded\ntrace")
    text = health.new_failure_alert(ctx)
    assert text and f"#{gid}" in text and "giving up" in text and "quota exceeded" in text
    assert "trace" not in text
    assert health.new_failure_alert(ctx) is None  # not repeated


def _upd():
    return SimpleNamespace(effective_chat=SimpleNamespace(id=CHAT),
                           effective_message=SimpleNamespace(reply_text=AsyncMock()))


def test_health_command(ctx):
    bot = ApprovalBot(ctx, CHAT, run_command=["true"])
    upd = _upd()
    asyncio.run(bot.on_health(upd, None))
    assert "check(s) ok" in upd.effective_message.reply_text.call_args.args[0]


def test_health_command_ignores_foreign_chat(ctx):
    bot = ApprovalBot(ctx, CHAT, run_command=["true"])
    upd = _upd()
    upd.effective_chat.id = 999
    asyncio.run(bot.on_health(upd, None))
    upd.effective_message.reply_text.assert_not_called()


def test_tick_sends_failure_alert_once(ctx):
    bot = ApprovalBot(ctx, CHAT, run_command=["true"])
    fake = FakeBot()
    asyncio.run(bot.tick(SimpleNamespace(bot=fake)))  # baseline
    assert fake.sent == []
    gid, _ = _fail(ctx)
    asyncio.run(bot.tick(SimpleNamespace(bot=fake)))
    msgs = [kw["text"] for k, kw in fake.sent if k == "message"]
    assert len(msgs) == 1 and f"#{gid}" in msgs[0] and "will retry" in msgs[0]
    asyncio.run(bot.tick(SimpleNamespace(bot=fake)))
    assert len(fake.sent) == 1
