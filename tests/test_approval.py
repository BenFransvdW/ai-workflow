import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shorts import db, records
from shorts.approval.bot import ApprovalBot
from shorts.approval.service import ApprovalError, ApprovalService
from shorts.state import group_state
from tests.conftest import make_group

CHAT = 4242


def states(ctx, gid):
    return [v["state"] for v in records.group_videos(ctx.conn, gid)]


# ---------------------------------------------------------------- service

def test_ready_to_send_and_mark_sent(ctx):
    svc = ApprovalService(ctx)
    g_rendered = make_group(ctx, "rendered", parts=2)
    make_group(ctx, "scripted")
    assert svc.ready_to_send() == [g_rendered]
    out = svc.outgoing(g_rendered)
    assert len(out.parts) == 2 and "r/AmItheAsshole" in out.summary and "Verdict Hour" in out.summary
    svc.mark_sent(g_rendered, 100, [98, 99])
    assert states(ctx, g_rendered) == ["awaiting_approval"] * 2
    assert svc.ready_to_send() == []
    assert svc.group_for_message(100) == g_rendered
    assert svc.group_for_message(99) == g_rendered


def test_approve(ctx):
    svc = ApprovalService(ctx)
    gid = make_group(ctx, "awaiting_approval", parts=2)
    nxt = svc.approve(gid, now=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc))
    assert states(ctx, gid) == ["approved", "approved"]
    assert nxt.hour == 12
    with pytest.raises(ApprovalError):
        svc.approve(gid)  # double tap


def test_reject_with_reason_blocks_story(ctx):
    svc = ApprovalService(ctx)
    gid = make_group(ctx, "awaiting_approval")
    svc.reject(gid)
    svc.set_reject_reason(gid, "boring")
    assert states(ctx, gid) == ["rejected"]
    story = records.group_story(ctx.conn, gid)
    assert story["status"] == "rejected" and "boring" in story["filter_reason"]
    with pytest.raises(ApprovalError):
        svc.approve(gid)


def test_regenerate_creates_fresh_group(ctx):
    svc = ApprovalService(ctx)
    gid = make_group(ctx, "awaiting_approval", series="petty_revenge")
    new = svc.regenerate(gid)
    assert states(ctx, gid) == ["superseded"]
    old_g = ctx.conn.execute("SELECT * FROM groups WHERE id=?", (gid,)).fetchone()
    new_g = ctx.conn.execute("SELECT * FROM groups WHERE id=?", (new,)).fetchone()
    assert new_g["story_id"] == old_g["story_id"] and new_g["series"] == "petty_revenge"
    assert new_g["regen_of"] == gid and new_g["voice"] != old_g["voice"]
    assert states(ctx, new) == ["candidate"]


def test_edit_title_keeps_part_suffix(ctx):
    svc = ApprovalService(ctx)
    gid = make_group(ctx, "awaiting_approval", parts=2)
    svc.edit_title(gid, "  The Seat   Thief ")
    assert [v["title"] for v in records.group_videos(ctx.conn, gid)] == ["The Seat Thief (Part 1/2)",
                                                                         "The Seat Thief (Part 2/2)"]
    with pytest.raises(ApprovalError):
        svc.edit_title(gid, "   ")


def test_expiry_after_window(ctx):
    svc = ApprovalService(ctx)
    gid = make_group(ctx, "rendered")
    svc.mark_sent(gid, 1, [])
    sent = db.parse_iso(ctx.conn.execute("SELECT sent_at FROM groups WHERE id=?", (gid,)).fetchone()[0])
    assert svc.expire(sent + timedelta(hours=47)) == []
    assert svc.expire(sent + timedelta(hours=48, minutes=1)) == [gid]
    assert states(ctx, gid) == ["expired"]
    approved = make_group(ctx, "approved")
    assert svc.expire(sent + timedelta(days=30)) == []  # approved videos don't expire
    assert group_state(ctx.conn, approved).value == "approved"


def test_status_and_pause(ctx):
    svc = ApprovalService(ctx)
    make_group(ctx, "awaiting_approval")
    make_group(ctx, "approved")
    s = svc.status(datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc))
    assert s["awaiting_approval_groups"] == 1 and s["approved_queue"] == 1
    assert s["max_uploads_per_day"] == 2 and s["next_upload_slot"].endswith("12:40 UTC")
    svc.pause()
    assert db.is_paused(ctx.conn) and "PAUSED" in svc.status_text()
    svc.resume()
    assert not db.is_paused(ctx.conn)


# ---------------------------------------------------------------- bot handlers

class FakeBot:
    def __init__(self):
        self.sent = []
        self.next_id = 500

    async def _msg(self, kind, **kw):
        self.next_id += 1
        self.sent.append((kind, kw))
        return SimpleNamespace(message_id=self.next_id)

    async def send_video(self, **kw):
        return await self._msg("video", **kw)

    async def send_message(self, **kw):
        return await self._msg("message", **kw)

    async def edit_message_reply_markup(self, **kw):
        return await self._msg("edit", **kw)


def make_bot(ctx):
    bot = ApprovalBot(ctx, CHAT, run_command=["true"])
    bot.trigger_run = lambda: setattr(bot, "triggered", True)
    return bot


def button(data, chat=CHAT):
    reply = AsyncMock(return_value=SimpleNamespace(message_id=777))
    query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_reply_markup=AsyncMock(),
                            message=SimpleNamespace(reply_text=reply))
    return SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=chat)), query


def text_reply(text, reply_to_id, chat=CHAT):
    msg = SimpleNamespace(text=text, reply_to_message=SimpleNamespace(message_id=reply_to_id), reply_text=AsyncMock())
    return SimpleNamespace(effective_message=msg, effective_chat=SimpleNamespace(id=chat)), msg


def run(coro):
    return asyncio.run(coro)


def test_tick_sends_rendered_groups(ctx):
    bot = make_bot(ctx)
    gid = make_group(ctx, "rendered", parts=2)
    fake = FakeBot()
    run(bot.tick(SimpleNamespace(bot=fake)))
    kinds = [k for k, _ in fake.sent]
    assert kinds == ["video", "video", "message"]
    assert all(kw["chat_id"] == CHAT for _, kw in fake.sent)
    markup = fake.sent[-1][1]["reply_markup"]
    assert [b.callback_data for b in markup.inline_keyboard[0]] == [f"a:{gid}", f"r:{gid}", f"g:{gid}"]
    assert states(ctx, gid) == ["awaiting_approval"] * 2
    run(bot.tick(SimpleNamespace(bot=fake)))
    assert len(fake.sent) == 3  # not re-sent


def test_foreign_chat_is_ignored(ctx):
    bot = make_bot(ctx)
    gid = make_group(ctx, "awaiting_approval")
    upd, query = button(f"a:{gid}", chat=999)
    run(bot.on_button(upd, None))
    assert states(ctx, gid) == ["awaiting_approval"]
    upd, msg = text_reply("title: hacked", 1, chat=999)
    run(bot.on_reply(upd, None))
    msg.reply_text.assert_not_called()


def test_approve_button(ctx):
    bot = make_bot(ctx)
    gid = make_group(ctx, "awaiting_approval")
    upd, query = button(f"a:{gid}")
    run(bot.on_button(upd, None))
    assert states(ctx, gid) == ["approved"]
    query.edit_message_reply_markup.assert_awaited_once()
    assert "approved" in query.message.reply_text.call_args.args[0]


def test_reject_button_then_reason(ctx):
    bot = make_bot(ctx)
    gid = make_group(ctx, "awaiting_approval")
    upd, query = button(f"r:{gid}")
    run(bot.on_button(upd, None))
    assert states(ctx, gid) == ["rejected"]
    prompt_id = 777
    upd, msg = text_reply("too similar to yesterday's", prompt_id)
    run(bot.on_reply(upd, None))
    assert ctx.conn.execute("SELECT reject_reason FROM groups WHERE id=?", (gid,)).fetchone()[0] == \
        "too similar to yesterday's"


def test_regenerate_button_triggers_run(ctx):
    bot = make_bot(ctx)
    gid = make_group(ctx, "awaiting_approval")
    upd, query = button(f"g:{gid}")
    run(bot.on_button(upd, None))
    assert states(ctx, gid) == ["superseded"] and getattr(bot, "triggered", False)
    assert ctx.conn.execute("SELECT COUNT(*) FROM groups WHERE regen_of=?", (gid,)).fetchone()[0] == 1


def test_stale_button_shows_alert(ctx):
    bot = make_bot(ctx)
    gid = make_group(ctx, "approved")
    upd, query = button(f"a:{gid}")
    run(bot.on_button(upd, None))
    assert query.answer.call_args.kwargs.get("show_alert") is True


def test_title_reply(ctx):
    bot = make_bot(ctx)
    gid = make_group(ctx, "rendered")
    ApprovalService(ctx).mark_sent(gid, 321, [320])
    upd, msg = text_reply("Title: Window Seat Wars", 320)  # reply to the video message works too
    run(bot.on_reply(upd, None))
    assert records.group_videos(ctx.conn, gid)[0]["title"] == "Window Seat Wars"
    assert "Window Seat Wars" in msg.reply_text.call_args.args[0]


def test_status_pause_resume_commands(ctx):
    bot = make_bot(ctx)
    upd = SimpleNamespace(effective_chat=SimpleNamespace(id=CHAT),
                          effective_message=SimpleNamespace(reply_text=AsyncMock()))
    run(bot.on_pause(upd, None))
    assert db.is_paused(ctx.conn)
    run(bot.on_status(upd, None))
    assert "PAUSED" in upd.effective_message.reply_text.call_args.args[0]
    run(bot.on_resume(upd, None))
    assert not db.is_paused(ctx.conn)


def test_application_builds(ctx):
    app = make_bot(ctx).build_application("123456:TEST-token")
    assert app.job_queue is not None and len(app.handlers[0]) == 7
