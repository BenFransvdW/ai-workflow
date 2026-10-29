"""Telegram approval bot (python-telegram-bot, long polling: no public URL or open ports).

Only messages from TELEGRAM_CHAT_ID are handled; everything else is ignored.
A repeating job sends newly rendered videos, expires stale ones, and the inline buttons
Approve / Reject / Regenerate act on a whole group (all parts of a story).
"""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime, timezone

from ..context import Ctx
from ..db import get_setting, set_setting
from .service import ApprovalError, ApprovalService

log = logging.getLogger("shorts.bot")

HELP = (
    "I send each rendered Short here for review.\n"
    "• Approve: queue it for the next posting slot\n"
    "• Reject: never use this story (reply with a reason if you like)\n"
    "• Regenerate: new script, voice and visuals for the same story\n"
    "Reply \"title: New title\" to a video's summary to rename it.\n"
    "/status · /pause · /resume"
)


def keyboard(group_id: int):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"a:{group_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"r:{group_id}"),
        InlineKeyboardButton("🔄 Regenerate", callback_data=f"g:{group_id}"),
    ]])


class ApprovalBot:
    def __init__(self, ctx: Ctx, chat_id: int, service: ApprovalService | None = None, run_command=None):
        self.ctx = ctx
        self.chat_id = int(chat_id)
        self.svc = service or ApprovalService(ctx)
        self.run_command = run_command or [sys.executable, "-m", "shorts", "--config",
                                           str(ctx.config.root / "config.yaml"), "run"]

    def authorized(self, update) -> bool:
        chat = getattr(update, "effective_chat", None)
        ok = chat is not None and chat.id == self.chat_id
        if not ok:
            log.warning("ignoring update from chat %s", getattr(chat, "id", None))
        return ok

    # ------------------------------------------------------------ periodic job
    async def tick(self, context) -> None:
        now = datetime.now(timezone.utc)
        for gid in self.svc.expire(now):
            await self._note_expired(context.bot, gid)
        for gid in self.svc.ready_to_send():
            try:
                await self.send_group(context.bot, gid)
            except Exception:
                log.exception("failed to send group %s", gid)

    async def send_group(self, bot, group_id: int) -> None:
        out = self.svc.outgoing(group_id)
        video_ids = []
        for part in out.parts:
            caption = f"#{group_id}" + (f" · Part {part.part_index}/{part.part_count}" if part.part_count > 1 else "")
            with open(part.path, "rb") as fh:
                msg = await bot.send_video(chat_id=self.chat_id, video=fh, caption=caption, supports_streaming=True,
                                           width=1080, height=1920, duration=int(part.duration),
                                           read_timeout=300, write_timeout=300)
            video_ids.append(msg.message_id)
        summary = await bot.send_message(chat_id=self.chat_id, text=out.summary, reply_markup=keyboard(group_id),
                                         disable_web_page_preview=True)
        self.svc.mark_sent(group_id, summary.message_id, video_ids)
        log.info("sent group %s for approval", group_id)

    async def _note_expired(self, bot, group_id: int) -> None:
        g = self.ctx.conn.execute("SELECT tg_message_id FROM groups WHERE id = ?", (group_id,)).fetchone()
        if g and g["tg_message_id"]:
            try:
                await bot.edit_message_reply_markup(chat_id=self.chat_id, message_id=g["tg_message_id"], reply_markup=None)
                await bot.send_message(chat_id=self.chat_id, text=f"⌛ #{group_id} expired (not approved in time).",
                                       reply_to_message_id=g["tg_message_id"])
            except Exception as e:
                log.debug("could not annotate expired group %s: %s", group_id, e)

    # ------------------------------------------------------------ handlers
    async def on_button(self, update, context) -> None:
        query = update.callback_query
        if not self.authorized(update):
            await query.answer()
            return
        action, _, gid = (query.data or "").partition(":")
        if not gid.isdigit():
            await query.answer("Unknown action")
            return
        group_id = int(gid)
        try:
            if action == "a":
                nxt = self.svc.approve(group_id)
                when = nxt.astimezone(self.ctx.config.tz).strftime("%a %H:%M") if nxt else "the next slot"
                text = f"✅ #{group_id} approved, queued for upload ({when})."
            elif action == "r":
                self.svc.reject(group_id)
                text = None
            elif action == "g":
                new = self.svc.regenerate(group_id)
                text = f"🔄 #{group_id} will be regenerated as #{new}. I'll send it when it's rendered."
                self.trigger_run()
            else:
                await query.answer("Unknown action")
                return
        except ApprovalError as e:
            await query.answer(str(e), show_alert=True)
            return
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        if action == "r":
            from telegram import ForceReply

            prompt = await query.message.reply_text(
                f"❌ #{group_id} rejected; the story won't be used. Reply with a reason (optional), or /skip.",
                reply_markup=ForceReply(selective=True))
            set_setting(self.ctx.conn, f"reason_prompt:{prompt.message_id}", str(group_id))
        else:
            await query.message.reply_text(text)

    async def on_reply(self, update, context) -> None:
        if not self.authorized(update):
            return
        msg = update.effective_message
        target = msg.reply_to_message
        if target is None or not msg.text:
            return
        text = msg.text.strip()
        reason_group = get_setting(self.ctx.conn, f"reason_prompt:{target.message_id}")
        if reason_group:
            self.svc.set_reject_reason(int(reason_group), text)
            set_setting(self.ctx.conn, f"reason_prompt:{target.message_id}", None)
            await msg.reply_text(f"Noted the reason for #{reason_group}.")
            return
        if text.lower().startswith("title:"):
            group_id = self.svc.group_for_message(target.message_id)
            if group_id is None:
                await msg.reply_text("Reply to a video or its summary message to rename it.")
                return
            try:
                title = self.svc.edit_title(group_id, text[6:])
            except ApprovalError as e:
                await msg.reply_text(str(e))
                return
            await msg.reply_text(f"✏️ #{group_id} title set to: {title}")

    async def on_status(self, update, context) -> None:
        if self.authorized(update):
            await update.effective_message.reply_text(self.svc.status_text())

    async def on_pause(self, update, context) -> None:
        if self.authorized(update):
            self.svc.pause()
            await update.effective_message.reply_text("⏸ Paused: no new videos will be generated or uploaded.")

    async def on_resume(self, update, context) -> None:
        if self.authorized(update):
            self.svc.resume()
            await update.effective_message.reply_text("▶️ Resumed.")

    async def on_help(self, update, context) -> None:
        if self.authorized(update):
            await update.effective_message.reply_text(HELP)

    async def on_skip(self, update, context) -> None:
        if self.authorized(update):
            await update.effective_message.reply_text("OK, no reason recorded.")

    # ------------------------------------------------------------ helpers
    def trigger_run(self) -> None:
        """Start a pipeline pass in the background so a regenerated video arrives quickly.

        Safe if a run is already going: the new process sees the lock and exits.
        """
        if not self.ctx.config.approval.trigger_run_on_regenerate:
            return
        try:
            subprocess.Popen(self.run_command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(self.ctx.config.root))
        except OSError as e:
            log.warning("could not start pipeline run: %s", e)

    def build_application(self, token: str):
        from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

        app = Application.builder().token(token).build()
        only_me = filters.Chat(chat_id=self.chat_id)
        app.add_handler(CommandHandler("status", self.on_status, filters=only_me))
        app.add_handler(CommandHandler("pause", self.on_pause, filters=only_me))
        app.add_handler(CommandHandler("resume", self.on_resume, filters=only_me))
        app.add_handler(CommandHandler(["start", "help"], self.on_help, filters=only_me))
        app.add_handler(CommandHandler("skip", self.on_skip, filters=only_me))
        app.add_handler(CallbackQueryHandler(self.on_button, pattern=r"^[arg]:\d+$"))
        app.add_handler(MessageHandler(only_me & filters.REPLY & filters.TEXT & ~filters.COMMAND, self.on_reply))
        app.job_queue.run_repeating(self.tick, interval=self.ctx.config.approval.poll_seconds, first=5)
        return app


def run_bot(ctx: Ctx) -> None:
    token = ctx.secrets.require("TELEGRAM_BOT_TOKEN")
    chat_id = int(ctx.secrets.require("TELEGRAM_CHAT_ID"))
    bot = ApprovalBot(ctx, chat_id)
    app = bot.build_application(token)
    log.info("approval bot polling (chat %s)", chat_id)
    app.run_polling(allowed_updates=["message", "callback_query"])
