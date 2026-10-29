"""Render one sample Short per procedural style from the fixture story (offline, no API keys)."""

from __future__ import annotations

import json
import logging
import random
import re
from pathlib import Path

from . import media
from .context import Ctx
from .fixtures import fixture_speech
from .stages.background.base import Piece, Segment
from .stages.background.procedural import STYLES, render_clip
from .stages.captions.ass import build_ass
from .stages.captions.transcribe import word_timings
from .stages.compose import compose
from .stages.tts import postprocess

log = logging.getLogger("shorts.samples")
FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "story.json"

# Each sample shows a different combination so the formats are visibly distinct.
LOOKS = {
    "bouncing": ("Verdict Hour", "bold_pop", "procedural:tense", 1.05),
    "plinko": ("Verdict Hour", "neon_bounce", "procedural:upbeat", 1.05),
    "runner": ("Petty Revenge", "karaoke_clean", "procedural:upbeat", 1.0),
    "plasma": ("Strange Encounters", "soft_fade", "procedural:eerie", 0.97),
    "flowfield": ("Strange Encounters", "soft_fade", "procedural:calm", 0.97),
}


def sample_text(script: dict, max_sentences: int = 4) -> str:
    sentences = re.split(r"(?<=[.!?])\s+", script["parts"][0]["narration"])
    return " ".join([script["hook"], *sentences[:max_sentences], script["commentary"]])


def render_samples(ctx: Ctx, styles: list[str] | None = None, seconds: float | None = None,
                   out_dir: Path | None = None, use_whisper: bool = True, crf: int = 26) -> list[Path]:
    script = json.loads(FIXTURE.read_text())["script"]
    text = sample_text(script)
    out_dir = out_dir or ctx.config.root / "samples"
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = ctx.config.model_copy(deep=True)
    cfg.root = ctx.config.root
    cfg.compose.crf = crf
    pc = cfg.background.procedural
    work = ctx.work_dir("samples")
    raw, method = fixture_speech(text, work / "voice_raw.wav")
    log.info("fixture narration via %s", method)
    outputs = []
    for i, style in enumerate(styles or list(STYLES)):
        badge, caption_style, music, pacing = LOOKS[style]
        voice = work / f"voice_{style}.wav"
        dur = postprocess(raw, voice, pacing)
        if seconds and dur > seconds:
            media.ffmpeg("-i", voice, "-t", f"{seconds:.2f}", "-c:a", "pcm_s16le", work / "cut.wav")
            (work / "cut.wav").replace(voice)
            dur = seconds
        words, how = word_timings(voice, text, model=cfg.captions.whisper_model,
                                  compute_type=cfg.captions.whisper_compute_type,
                                  download_root=cfg.path(cfg.paths.models_dir) / "whisper", use_whisper=use_whisper)
        ass = work / f"{style}.ass"
        ass.write_text(build_ass(words, cfg.captions.styles[caption_style], badge=badge), encoding="utf-8")
        bg = work / f"bg_{style}.mp4"
        meta = render_clip(style, bg, width=pc.width, height=pc.height, fps=pc.fps, seconds=dur + 1.5,
                           seed=random.Random(style).randrange(1 << 31), crf=pc.crf, preset=pc.preset)
        out = out_dir / f"sample_{style}.mp4"
        res = compose(cfg, segment=Segment("procedural", [Piece(0, str(bg), 0.3, dur + 0.8)]), voice=voice,
                      ass=ass, music_spec=music, music_seed=11 + i, work=work, out=out)
        log.info("%s: %.1fs, bg %.0fs render, captions via %s → %s", style, res["duration"], meta["render_seconds"],
                 how, out)
        outputs.append(out)
    return outputs
