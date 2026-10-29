"""Offline fixture audio: espeak-ng speech if installed, otherwise speech-like tone bursts."""

from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from .stages.captions.align import syllables, tokenize


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm.tobytes())


def placeholder_speech(text: str, path: Path, rate: int = 22050, wpm: int = 170) -> Path:
    """Tone bursts (one per word, syllable-length) with pauses at punctuation."""
    rng = np.random.default_rng(0)
    syl = 60 / wpm / 1.4
    out = [np.zeros(int(0.15 * rate))]
    for tok in tokenize(text):
        n = int(syllables(tok) * syl * rate)
        t = np.arange(n) / rate
        f0 = 140 + 30 * rng.random()
        env = np.sin(np.pi * np.linspace(0, 1, n)) ** 0.5
        tone = (np.sin(2 * np.pi * f0 * t) + 0.4 * np.sin(4 * np.pi * f0 * t) + 0.2 * np.sin(6 * np.pi * f0 * t))
        out.append(0.3 * env * tone)
        gap = 0.4 if tok[-1] in ".!?" else 0.25 if tok[-1] in ",;:" else 0.04
        out.append(np.zeros(int(gap * rate)))
    write_wav(path, np.concatenate(out), rate)
    return path


def espeak_available() -> str | None:
    return shutil.which("espeak-ng") or shutil.which("espeak")


def fixture_speech(text: str, path: Path, voice: str = "en-us+m3", wpm: int = 165) -> tuple[Path, str]:
    """Return (wav path, method). Uses espeak-ng when available."""
    path.parent.mkdir(parents=True, exist_ok=True)
    exe = espeak_available()
    if exe:
        subprocess.run([exe, "-v", voice, "-s", str(wpm), "-w", str(path), text.replace("*", "")],
                       check=True, capture_output=True)
        return path, "espeak"
    return placeholder_speech(text, path), "placeholder"
