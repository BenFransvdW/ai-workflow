"""Captions stage: word-level timestamps → styled ASS subtitles."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...context import Ctx
from ...db import jload, update
from ...records import video_context
from .align import Word
from .ass import build_ass
from .transcribe import word_timings

log = logging.getLogger("shorts.captions")


def make_captions(ctx: Ctx, audio: Path, script_text: str, style_name: str, out: Path,
                  use_whisper: bool = True, badge: str | None = None) -> tuple[list[Word], str]:
    cc = ctx.config.captions
    words, method = word_timings(audio, script_text, model=cc.whisper_model, compute_type=cc.whisper_compute_type,
                                 download_root=ctx.config.path(ctx.config.paths.models_dir) / "whisper",
                                 use_whisper=use_whisper)
    out.write_text(build_ass(words, cc.styles[style_name], max_words=cc.max_words, max_chars=cc.max_chars,
                             badge=badge),
                   encoding="utf-8")
    return words, method


def captions_video(ctx: Ctx, video_id: int, use_whisper: bool = True) -> Path:
    video, group, _ = video_context(ctx.conn, video_id)
    if not video["audio_path"]:
        raise RuntimeError(f"video {video_id} has no audio yet; run tts first")
    script = jload(video["script_json"], {})
    out = ctx.work_dir("videos", video_id) / "captions.ass"
    badge = ctx.config.series_by_name(group["series"]).display_name
    if video["part_count"] > 1:
        badge += f" · Part {video['part_index']}/{video['part_count']}"
    words, method = make_captions(ctx, Path(video["audio_path"]), script["text"], group["caption_style"], out,
                                  use_whisper, badge=badge)
    update(ctx.conn, "videos", video_id, ass_path=str(out), words_json=json.dumps([w.to_dict() for w in words]))
    log.info("video %s: %d caption words via %s", video_id, len(words), method)
    return out
