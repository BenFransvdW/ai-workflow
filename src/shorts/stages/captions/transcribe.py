"""Word-level timestamps via faster-whisper (CPU, int8), with an offline energy-based fallback."""

from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np

from .align import Word, align_by_energy, align_to_asr

log = logging.getLogger("shorts.captions")

_MODELS: dict[tuple[str, str], object] = {}


def read_wav_mono(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        rate, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"{path}: expected 16-bit PCM WAV")
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    return data, rate


def whisper_words(audio: str | Path, model: str = "small", compute_type: str = "int8",
                  download_root: str | Path | None = None, prompt: str | None = None) -> list[Word]:
    from faster_whisper import WhisperModel  # optional dependency

    key = (model, compute_type)
    if key not in _MODELS:
        log.info("loading faster-whisper %s (%s)…", model, compute_type)
        _MODELS[key] = WhisperModel(model, device="cpu", compute_type=compute_type,
                                    download_root=str(download_root) if download_root else None)
    segments, _ = _MODELS[key].transcribe(str(audio), language="en", word_timestamps=True, beam_size=1,
                                          vad_filter=False, initial_prompt=prompt[:800] if prompt else None)
    return [Word(w.word.strip(), float(w.start), float(w.end)) for s in segments for w in (s.words or [])]


def word_timings(audio: str | Path, script: str, *, model: str = "small", compute_type: str = "int8",
                 download_root: str | Path | None = None, use_whisper: bool = True) -> tuple[list[Word], str]:
    """Return (words spelled as in `script`, method) where method is 'whisper' or 'energy'."""
    if use_whisper:
        try:
            asr = whisper_words(audio, model, compute_type, download_root, prompt=script)
            if asr:
                return align_to_asr(script, asr), "whisper"
            log.warning("whisper returned no words; using energy alignment")
        except Exception as e:  # not installed, model download blocked, etc.
            log.warning("faster-whisper unavailable (%s); using energy alignment", e)
    samples, rate = read_wav_mono(audio)
    return align_by_energy(script, samples, rate), "energy"
