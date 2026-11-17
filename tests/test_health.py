from __future__ import annotations

import asyncio
import json
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from shorts import db, health, records
from shorts.approval.bot import ApprovalBot
from shorts.cli import main
from shorts.doctor import FAIL, OK, WARN
from shorts.run import run_once
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
    for _ in range(ctx.config.health.failure_threshold):
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


# ---------------------------------------------------------------- weekly/2026-11-17 additions
T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
Usage = namedtuple("Usage", "total used free")


def _conds(ctx, now=T0):
    return {c.name: c for c in health.condition_checks(ctx, now)}


def test_fresh_install_has_no_run_check(ctx):
    assert "pipeline run" not in _conds(ctx)


def test_stale_run_warns_unless_paused(ctx):
    health.mark_run(ctx, T0 - timedelta(hours=2))
    assert _conds(ctx)["pipeline run"].level == OK
    health.mark_run(ctx, T0 - timedelta(hours=7))
    c = _conds(ctx)["pipeline run"]
    assert c.level == WARN and "7h" in c.detail
    db.set_setting(ctx.conn, "paused", "1")
    assert "pipeline run" not in _conds(ctx)


def test_run_once_marks_completed_pass(ctx):
    run_once(ctx, uploader=lambda *a, **k: None, generate=False, now=T0)
    assert db.get_setting(ctx.conn, health.LAST_RUN_KEY) == T0.isoformat(timespec="seconds")


def test_low_disk_warns(ctx, monkeypatch):
    ctx.config.health.min_free_gb = 2
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: Usage(32e9, 31e9, 1e9))
    c = _conds(ctx)["disk space"]
    assert c.level == WARN and "1.0 GB" in c.detail
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: Usage(32e9, 2e9, 30e9))
    assert _conds(ctx)["disk space"].level == OK


def test_rejected_token_only_when_live(ctx):
    gid = make_group(ctx, "approved")
    vid = records.group_videos(ctx.conn, gid)[0]["id"]
    transition(ctx.conn, vid, State.FAILED, "upload: ('invalid_grant: Token has been expired or revoked.', {})")
    now = datetime.now(timezone.utc)
    assert "youtube token rejected" not in _conds(ctx, now)  # dry run
    ctx.config.upload.dry_run = False
    c = _conds(ctx, now)["youtube token rejected"]
    assert c.level == FAIL and "auth-youtube" in c.detail
    assert "youtube token rejected" in {x.name for x in health.run_health(ctx, now)}


def test_failure_threshold_is_configurable(ctx):
    ctx.config.health.failure_threshold = 1
    _fail(ctx)
    c = _by_name(health.run_health(ctx, datetime.now(timezone.utc)))
    assert c["repeated failures"].level == WARN


def test_condition_alert_debounce_and_all_clear(ctx):
    assert health.condition_alert(ctx, T0) is None
    health.mark_run(ctx, T0 - timedelta(hours=7))
    first = health.condition_alert(ctx, T0)
    assert first and "pipeline run" in first
    assert health.condition_alert(ctx, T0 + timedelta(minutes=5)) is None  # same problem: quiet
    assert health.condition_alert(ctx, T0 + timedelta(hours=1)) is None
    assert health.condition_alert(ctx, T0 + timedelta(hours=12))  # reminder
    health.mark_run(ctx, T0 + timedelta(hours=12))
    assert health.condition_alert(ctx, T0 + timedelta(hours=12)) == "✅ Health back to normal."
    assert health.condition_alert(ctx, T0 + timedelta(hours=13)) is None


def test_condition_alert_new_problem_bypasses_debounce(ctx, monkeypatch):
    health.mark_run(ctx, T0 - timedelta(hours=7))
    assert health.condition_alert(ctx, T0)
    ctx.config.health.min_free_gb = 2
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: Usage(32e9, 31e9, 1e9))
    text = health.condition_alert(ctx, T0 + timedelta(minutes=1))
    assert text and "disk space" in text and "pipeline run" in text


def test_condition_alerts_can_be_disabled(ctx):
    ctx.config.health.condition_alerts = False
    health.mark_run(ctx, T0 - timedelta(hours=7))
    assert health.condition_alert(ctx, T0) is None


def test_tick_pushes_condition_alert_once(ctx):
    bot = ApprovalBot(ctx, CHAT, run_command=["true"])
    health.mark_run(ctx, datetime.now(timezone.utc) - timedelta(hours=8))
    fake = FakeBot()
    asyncio.run(bot.tick(SimpleNamespace(bot=fake)))
    texts = [kw["text"] for k, kw in fake.sent if k == "message"]
    assert len(texts) == 1 and "no completed run" in texts[0] and fake.sent[0][1]["chat_id"] == CHAT
    asyncio.run(bot.tick(SimpleNamespace(bot=fake)))
    assert len(fake.sent) == 1


def test_cli_health_exit_code(tmp_path, capsys):
    import yaml

    from tests.conftest import ROOT
    raw = yaml.safe_load((ROOT / "config.yaml").read_text())
    raw["paths"]["data_dir"] = str(tmp_path / "data")
    raw["health"]["min_free_gb"] = 0
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(raw))
    rc = main(["--config", str(tmp_path / "config.yaml"), "--env", str(tmp_path / "none.env"), "health"])
    out = capsys.readouterr().out
    assert "pipeline run" not in out and out.splitlines()[0] in ("🔴 Problems found", "🟡 Warnings", "🟢 All good")
    assert rc == (1 if out.startswith("🔴") else 0)
