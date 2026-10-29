"""Background stage: choose copyright-safe footage for a video according to its series."""

from __future__ import annotations

import json
import logging
import random

from ... import media
from ...config import SeriesBackground
from ...context import Ctx
from ...db import update
from ...records import video_context
from .base import BackgroundProvider, NoBackgroundAvailable, Segment, record_usage

log = logging.getLogger("shorts.background")


def get_provider(ctx: Ctx, name: str, rng: random.Random | None = None) -> BackgroundProvider:
    if name == "procedural":
        from .pool import ProceduralProvider
        return ProceduralProvider(ctx, rng)
    if name == "local_folder":
        from .local_folder import LocalFolderProvider
        return LocalFolderProvider(ctx, rng)
    if name == "pexels":
        from .pexels import PexelsProvider
        return PexelsProvider(ctx, rng)
    raise ValueError(f"unknown background provider {name!r}")


def choose_segment(ctx: Ctx, series_name: str, duration: float, rng: random.Random | None = None) -> Segment:
    """Weighted choice among the series' providers; falls back to procedural if one is unavailable."""
    rng = rng or random.Random()
    specs = list(ctx.config.series_by_name(series_name).backgrounds)
    order: list[SeriesBackground] = []
    while specs:
        pick = rng.choices(specs, weights=[s.weight for s in specs])[0]
        order.append(pick)
        specs.remove(pick)
    errors = []
    for spec in order:
        try:
            return get_provider(ctx, spec.provider, rng).get_segment(duration, spec)
        except (NoBackgroundAvailable, RuntimeError, OSError) as e:  # missing key, network, empty folder…
            log.warning("background provider %s unavailable: %s", spec.provider, e)
            errors.append(f"{spec.provider}: {e}")
    if not any(s.provider == "procedural" for s in order):
        return get_provider(ctx, "procedural", rng).get_segment(duration, SeriesBackground(provider="procedural"))
    raise NoBackgroundAvailable("; ".join(errors))


def background_video(ctx: Ctx, video_id: int, rng: random.Random | None = None) -> Segment:
    video, group, _ = video_context(ctx.conn, video_id)
    if not video["audio_path"]:
        raise RuntimeError(f"video {video_id} has no narration yet; run tts first")
    need = media.duration(video["audio_path"]) + 0.8  # compose adds a short musical tail
    seg = choose_segment(ctx, group["series"], need, rng)
    record_usage(ctx.conn, seg, video_id)
    update(ctx.conn, "videos", video_id, bg_clip_id=seg.pieces[0].clip_id, bg_start=seg.pieces[0].start,
           bg_json=json.dumps(seg.to_json()))
    log.info("video %s background: %s %s", video_id, seg.provider, [(p.clip_id, round(p.start, 1)) for p in seg.pieces])
    return seg
