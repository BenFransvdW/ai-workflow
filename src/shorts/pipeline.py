"""Glue between stages: create a group, script it, render every part, handle failures."""

from __future__ import annotations

import logging
import random
import traceback

from . import media
from .context import Ctx
from .records import create_group, group_videos
from .series import choose
from .stages.compose import TAIL, TooLong
from .state import State, transition

log = logging.getLogger("shorts.pipeline")


def new_group(ctx: Ctx, series_name: str | None = None, rng: random.Random | None = None) -> int | None:
    """Pick a series + unused story and create a candidate group. None when no story is available."""
    sel = choose(ctx.conn, ctx.config, rng or random.Random(), only=[series_name] if series_name else None)
    if sel is None:
        return None
    gid = create_group(ctx.conn, sel.story_id, sel.series.name, voice=sel.voice, caption_style=sel.caption_style,
                       music_bed=sel.music_bed)
    log.info("group %s: series=%s story=%s voice=%s style=%s music=%s", gid, sel.series.name, sel.story_id,
             sel.voice, sel.caption_style, sel.music_bed)
    return gid


def regenerate_group(ctx: Ctx, old_group_id: int, rng: random.Random | None = None) -> int:
    """New group for the same story and series, rotating voice/caption style/music away from the old one."""
    rng = rng or random.Random()
    old = ctx.conn.execute("SELECT * FROM groups WHERE id = ?", (old_group_id,)).fetchone()
    series = ctx.config.series_by_name(old["series"])

    def pick(options: list[str], current: str | None) -> str:
        others = [o for o in options if o != current]
        return rng.choice(others or options)

    return create_group(ctx.conn, old["story_id"], old["series"], voice=pick(series.voices, old["voice"]),
                        caption_style=pick(series.caption_styles, old["caption_style"]),
                        music_bed=pick(series.music, old["music_bed"]), regen_of=old_group_id)


def _fail_group(ctx: Ctx, group_id: int, err: str) -> None:
    for v in group_videos(ctx.conn, group_id):
        if State(v["state"]) in (State.CANDIDATE, State.SCRIPTED, State.RENDERED):
            transition(ctx.conn, v["id"], State.FAILED, err[:200], error=err[-2000:], attempts=v["attempts"] + 1)


def render_video(ctx: Ctx, video_id: int, use_whisper: bool = True) -> None:
    from .stages.background import background_video
    from .stages.captions import captions_video
    from .stages.compose import compose_video
    from .stages.tts import tts_video

    audio = tts_video(ctx, video_id)
    limit = ctx.config.compose.max_seconds - TAIL
    dur = media.duration(audio)
    if dur > limit:
        factor = dur / limit * 1.01
        if factor > ctx.config.compose.max_speedup:
            raise TooLong(f"narration {dur:.1f}s needs {factor:.2f}x speed-up (max {ctx.config.compose.max_speedup})")
        log.info("video %s: %.1fs narration, speeding up %.3fx", video_id, dur, factor)
        tts_video(ctx, video_id, extra_speed=factor)
    captions_video(ctx, video_id, use_whisper=use_whisper)
    background_video(ctx, video_id)
    compose_video(ctx, video_id)


def render_group(ctx: Ctx, group_id: int, use_whisper: bool = True) -> None:
    for v in group_videos(ctx.conn, group_id):
        if State(v["state"]) != State.SCRIPTED:
            continue
        render_video(ctx, v["id"], use_whisper)
        transition(ctx.conn, v["id"], State.RENDERED, "composed")


def produce(ctx: Ctx, group_id: int, *, client=None, use_whisper: bool = True, shrink: float = 1.0) -> bool:
    """Script (if needed) and render a group. Returns True when it reached `rendered`.

    Failures leave the parts in `failed` with the error recorded; a story that is too long is
    re-scripted once in a new group with a smaller word budget.
    """
    from .stages.script import ScriptRejected, script_group

    try:
        if any(State(v["state"]) == State.CANDIDATE for v in group_videos(ctx.conn, group_id)):
            script_group(ctx, group_id, client, shrink=shrink)
        render_group(ctx, group_id, use_whisper)
        return True
    except ScriptRejected as e:
        log.warning("group %s rejected by safety review: %s", group_id, e)
        return False
    except TooLong as e:
        _fail_group(ctx, group_id, f"too long: {e}")
        if shrink > 0.7:
            log.info("group %s too long; re-scripting with a smaller budget", group_id)
            new = regenerate_group(ctx, group_id)
            for v in group_videos(ctx.conn, group_id):
                transition(ctx.conn, v["id"], State.SUPERSEDED, f"too long, replaced by group {new}")
            return produce(ctx, new, client=client, use_whisper=use_whisper, shrink=shrink * 0.8)
        return False
    except Exception as e:
        log.exception("group %s failed", group_id)
        _fail_group(ctx, group_id, f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        return False


def retry_failed(ctx: Ctx) -> list[int]:
    """Groups whose render failed (but not too often) are reset to their last good state."""
    max_attempts = ctx.config.limits.max_attempts
    groups = []
    rows = ctx.conn.execute(
        "SELECT v.id, v.group_id, v.attempts, (SELECT from_state FROM video_events e WHERE e.video_id = v.id"
        " ORDER BY e.id DESC LIMIT 1) AS prev FROM videos v WHERE v.state = 'failed'"
    ).fetchall()
    for r in rows:
        if r["attempts"] >= max_attempts:
            continue
        if r["prev"] == State.CANDIDATE.value:
            transition(ctx.conn, r["id"], State.CANDIDATE, "retry")
        elif r["prev"] in (State.SCRIPTED.value, State.RENDERED.value):
            transition(ctx.conn, r["id"], State.SCRIPTED, "retry render")
        else:
            continue
        groups.append(r["group_id"])
    return sorted(set(groups))
