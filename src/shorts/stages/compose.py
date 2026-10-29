"""Compose stage: background + narration + music bed + burned-in captions → 1080x1920 MP4 (< 60 s).

Audio: the music bed is ducked under the voice (sidechain compression), then the mix goes through
two-pass EBU R128 loudness normalisation (-14 LUFS integrated by default, YouTube's reference).
"""

from __future__ import annotations

import json
import logging
import random
from pathlib import Path

from .. import media
from ..config import Config
from ..context import Ctx
from ..db import jload, update
from ..records import video_context
from .background.base import Segment
from .music import make_bed

log = logging.getLogger("shorts.compose")
TAIL = 0.6  # seconds of music after the last word, faded out


class TooLong(RuntimeError):
    """Narration doesn't fit in max_seconds even with the allowed speed-up."""


def _filter_path(p: Path | str) -> str:
    """Escape a path for use inside an ffmpeg filtergraph option value."""
    s = str(p)
    for ch in ("\\", ":", "'", ",", "[", "]", ";"):
        s = s.replace(ch, "\\" + ch)
    return s


def mix_audio(cfg: Config, voice: Path, music: Path | None, out: Path, total: float) -> dict:
    """Voice + ducked music → loudness-normalised 48 kHz stereo WAV of exactly `total` seconds."""
    c = cfg.compose
    pre = out.with_name("premix.wav")
    if music is not None:
        fc = (f"[0:a]aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={total:.3f},asplit=2[v1][v2];"
              f"[1:a]aresample=48000,aformat=channel_layouts=stereo,volume={cfg.music.volume_db}dB,"
              f"atrim=0:{total:.3f},afade=t=out:st={max(0, total - TAIL):.3f}:d={TAIL}[m];"
              f"[m][v1]sidechaincompress=threshold=0.02:ratio=6:attack=15:release=350:makeup=1[duck];"
              f"[v2][duck]amix=inputs=2:duration=first:normalize=0[mix]")
        media.ffmpeg("-i", voice, "-stream_loop", "-1", "-i", music, "-filter_complex", fc, "-map", "[mix]",
                     "-t", f"{total:.3f}", "-c:a", "pcm_s16le", pre)
    else:
        media.ffmpeg("-i", voice, "-af", f"aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={total:.3f}",
                     "-t", f"{total:.3f}", "-c:a", "pcm_s16le", pre)
    m = media.measure_loudness(pre)
    ln = (f"loudnorm=I={c.loudness_i}:TP={c.loudness_tp}:LRA={c.loudness_lra}:measured_I={m['input_i']}:"
          f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
          f"offset={m['target_offset']}:linear=true")
    media.ffmpeg("-i", pre, "-af", f"{ln},aresample=48000", "-ar", "48000", "-c:a", "pcm_s16le", out)
    pre.unlink(missing_ok=True)
    return m


def render_video(cfg: Config, *, segment: Segment, audio: Path, ass: Path | None, out: Path, total: float) -> None:
    c = cfg.compose
    args: list[str | Path] = []
    chains = []
    for i, p in enumerate(segment.pieces):
        args += ["-ss", f"{p.start:.3f}", "-t", f"{p.duration + 0.1:.3f}", "-i", p.path]
        chains.append(
            f"[{i}:v]fps={c.fps},scale={c.width}:{c.height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={c.width}:{c.height},setsar=1,trim=duration={p.duration:.3f},setpts=PTS-STARTPTS[b{i}]"
        )
    n = len(segment.pieces)
    concat = "".join(f"[b{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[bg]" if n > 1 else "[b0]null[bg]"
    vf = [f"[bg]tpad=stop_mode=clone:stop_duration=2,trim=duration={total:.3f}"]
    if ass is not None:
        fonts = cfg.path(cfg.paths.fonts_dir)
        vf.append(f"ass={_filter_path(ass)}:fontsdir={_filter_path(fonts)}")
    vf.append(f"fade=t=out:st={max(0, total - 0.4):.3f}:d=0.4,format=yuv420p[v]")
    fc = ";".join(chains + [concat, ",".join(vf)])
    audio_idx = n
    media.ffmpeg(
        *args, "-i", audio, "-filter_complex", fc, "-map", "[v]", "-map", f"{audio_idx}:a",
        "-c:v", "libx264", "-preset", c.preset, "-crf", str(c.crf), "-profile:v", "high", "-r", str(c.fps),
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-t", f"{total:.3f}", "-movflags", "+faststart", out,
    )


def make_preview(cfg: Config, src: Path, out: Path) -> Path | None:
    """Telegram bots can send ≤ 50 MB; produce a smaller copy when needed."""
    if src.stat().st_size <= cfg.compose.telegram_max_mb * 1024 * 1024:
        return None
    media.ffmpeg("-i", src, "-vf", "scale=720:1280:flags=bicubic", "-c:v", "libx264", "-preset", "veryfast",
                 "-crf", "30", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out)
    return out


def compose(cfg: Config, *, segment: Segment, voice: Path, ass: Path | None, music_spec: str | None, music_seed: int,
            work: Path, out: Path) -> dict:
    voice_len = media.duration(voice)
    total = round(voice_len + TAIL, 3)
    if total > cfg.compose.max_seconds:
        raise TooLong(f"narration {voice_len:.1f}s + tail exceeds {cfg.compose.max_seconds}s")
    music = None
    if music_spec:
        music = make_bed(music_spec, total + 1, music_seed, work, cfg.path(cfg.paths.music_dir))
    audio = work / "final_audio.wav"
    loud = mix_audio(cfg, voice, music, audio, total)
    render_video(cfg, segment=segment, audio=audio, ass=ass, out=out, total=total)
    if music is not None and music.parent == work:
        music.unlink(missing_ok=True)
    preview = make_preview(cfg, out, out.with_name(out.stem + "_preview.mp4"))
    return {"duration": media.duration(out), "preview": str(preview) if preview else None, "input_loudness": loud}


def compose_video(ctx: Ctx, video_id: int) -> Path:
    video, group, _ = video_context(ctx.conn, video_id)
    for col in ("audio_path", "ass_path", "bg_json"):
        if not video[col]:
            raise RuntimeError(f"video {video_id} is missing {col}; run the earlier stages first")
    work = ctx.work_dir("videos", video_id)
    out = work / f"short_{video_id}.mp4"
    seed = video["music_seed"] if video["music_seed"] is not None else random.SystemRandom().randrange(1 << 31)
    bed = ctx.config.music.beds.get(group["music_bed"]) if group["music_bed"] else None
    res = compose(ctx.config, segment=Segment.from_json(jload(video["bg_json"])), voice=Path(video["audio_path"]),
                  ass=Path(video["ass_path"]), music_spec=bed, music_seed=seed, work=work, out=out)
    update(ctx.conn, "videos", video_id, video_path=str(out), preview_path=res["preview"], duration=res["duration"],
           music_seed=seed)
    log.info("video %s composed: %.1fs %s", video_id, res["duration"], json.dumps(res["input_loudness"])[:80])
    return out
