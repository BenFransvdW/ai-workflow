"""TTS stage: pluggable providers (ElevenLabs, OpenAI, Piper, espeak) → normalised narration WAV."""

from __future__ import annotations

import logging
from pathlib import Path

from ... import media
from ...context import Ctx
from ...db import jload, update
from ...records import video_context
from .base import TTSProvider

log = logging.getLogger("shorts.tts")


def get_provider(ctx: Ctx, voice_name: str, session=None) -> TTSProvider:
    vc = ctx.config.tts.voices[voice_name]
    opts = vc.model_dump()
    if vc.provider == "elevenlabs":
        from .elevenlabs import ElevenLabsTTS
        return ElevenLabsTTS(ctx, opts, session)
    if vc.provider == "openai":
        from .openai_tts import OpenAITTS
        return OpenAITTS(ctx, opts, session)
    if vc.provider == "piper":
        from .piper import PiperTTS
        return PiperTTS(ctx, opts)
    if vc.provider == "espeak":
        from .piper import EspeakTTS
        return EspeakTTS(ctx, opts)
    raise ValueError(vc.provider)


def postprocess(raw: Path, out: Path, tempo: float = 1.0) -> float:
    """Trim leading/trailing silence, apply pacing, write 44.1 kHz mono 16-bit WAV. Returns duration."""
    trim = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.05"
    chain = [trim, "areverse", trim, "areverse"]
    t = tempo
    while t > 2.0:  # atempo accepts 0.5–2.0 per instance
        chain.append("atempo=2.0")
        t /= 2.0
    if abs(t - 1.0) > 1e-3:
        chain.append(f"atempo={t:.4f}")
    chain.append("adelay=120:all=1")  # a breath before the first word
    media.ffmpeg("-i", raw, "-af", ",".join(chain), "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", out)
    return media.duration(out)


def narration_text(script: dict) -> str:
    return " ".join(script["text"].replace("*", "").split())


def tts_video(ctx: Ctx, video_id: int, extra_speed: float = 1.0, provider: TTSProvider | None = None) -> Path:
    video, group, _ = video_context(ctx.conn, video_id)
    script = jload(video["script_json"])
    if not script:
        raise RuntimeError(f"video {video_id} has no script yet")
    series = ctx.config.series_by_name(group["series"])
    work = ctx.work_dir("videos", video_id)
    provider = provider or get_provider(ctx, group["voice"])
    raw = provider.synthesize(narration_text(script), work / "voice_raw")
    out = work / "voice.wav"
    dur = postprocess(raw, out, series.pacing * extra_speed)
    update(ctx.conn, "videos", video_id, audio_path=str(out), duration=dur)
    log.info("video %s narrated with %s (%s): %.1fs", video_id, group["voice"], type(provider).__name__, dur)
    return out
