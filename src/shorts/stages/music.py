"""Music beds: procedurally synthesised per video (copyright-safe), or tracks from your own folder."""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np

from ..fixtures import write_wav

RATE = 44100
MOODS = ("calm", "upbeat", "tense", "eerie")

# Scale degrees (semitones) of chords in a progression, relative to the key root.
PROGRESSIONS = {
    "calm": [[[0, 4, 7, 11], [7, 11, 14], [9, 12, 16], [5, 9, 12, 16]],
             [[0, 4, 7], [5, 9, 12], [9, 12, 16], [7, 11, 14]]],
    "upbeat": [[[0, 4, 7], [7, 11, 14], [9, 12, 16], [5, 9, 12]],
               [[0, 4, 7], [5, 9, 12], [7, 11, 14], [5, 9, 12]]],
    "tense": [[[0, 3, 7], [8, 12, 15], [3, 7, 10], [10, 14, 17]],
              [[0, 3, 7], [0, 3, 8], [0, 3, 7], [11, 14, 17]]],
    "eerie": [[[0, 3, 6], [1, 4, 8], [0, 3, 6, 9], [2, 6, 10]],
              [[0, 6, 11], [1, 7, 10], [0, 4, 8], [-1, 5, 10]]],
}


def _hz(midi: float) -> float:
    return 440.0 * 2 ** ((midi - 69) / 12)


def _lowpass(x: np.ndarray, cutoff: float) -> np.ndarray:
    """FFT brick-ish lowpass with a soft roll-off (fast for a few minutes of audio)."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / RATE)
    spec *= 1 / (1 + (f / cutoff) ** 4)
    return np.fft.irfft(spec, len(x))


def synthesize(mood: str, seconds: float, seed: int) -> np.ndarray:
    """Stereo float32 bed, peak-normalised to -1 dBFS."""
    if mood not in MOODS:
        raise ValueError(f"unknown mood {mood!r}; choose from {MOODS}")
    rng = np.random.default_rng(seed)
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    out = np.zeros((2, n))
    tempo = {"calm": 72, "upbeat": 104, "tense": 92, "eerie": 60}[mood] * rng.uniform(0.92, 1.08)
    beat = 60 / tempo
    chord_len = beat * 4 * (2 if mood in ("calm", "eerie") else 1)
    root = int(rng.integers(45, 53))  # A2..C#3
    prog = PROGRESSIONS[mood][int(rng.integers(len(PROGRESSIONS[mood])))]
    rot = int(rng.integers(len(prog)))
    prog = prog[rot:] + prog[:rot]

    # Pad: additive, slightly detuned per channel, crossfaded chords.
    fade = min(0.8, chord_len / 3)
    for k in range(int(np.ceil(seconds / chord_len)) + 1):
        c0 = k * chord_len
        i0, i1 = int(max(0, c0 - fade) * RATE), int(min(seconds, c0 + chord_len + fade) * RATE)
        if i0 >= n:
            break
        tt = t[i0:i1]
        env = np.clip(np.minimum(tt - (c0 - fade), (c0 + chord_len + fade) - tt) / fade, 0, 1) ** 2
        for deg in prog[k % len(prog)]:
            f = _hz(root + 12 + deg)
            for ch, det in ((0, -0.15), (1, 0.15)):
                ff = f * 2 ** (det / 1200 * rng.uniform(5, 12))
                ph = rng.uniform(0, 2 * np.pi)
                tone = sum(np.sin(2 * np.pi * h * ff * tt + ph * h) / h ** 1.6 for h in (1, 2, 3, 4))
                out[ch, i0:i1] += 0.06 * env * tone
        # Bass.
        fb = _hz(root + prog[k % len(prog)][0] - 12)
        out[:, i0:i1] += 0.10 * env * np.sin(2 * np.pi * fb * tt)

    # Rhythm: plucked arpeggio (upbeat/calm) or a pulsing ostinato (tense).
    if mood in ("upbeat", "tense", "calm"):
        step = beat / (2 if mood != "calm" else 1)
        decay = {"upbeat": 9, "tense": 14, "calm": 4}[mood]
        level = {"upbeat": 0.10, "tense": 0.08, "calm": 0.05}[mood]
        pl = int(min(1.5, step * 3) * RATE)
        pt = np.arange(pl) / RATE
        for k in range(int(seconds / step)):
            chord = prog[int(k * step // chord_len) % len(prog)]
            deg = chord[k % len(chord)] if mood != "tense" else chord[0] + (12 if k % 4 == 3 else 0)
            f = _hz(root + 24 + deg)
            i0 = int(k * step * RATE)
            seg = level * np.exp(-pt * decay) * (np.sin(2 * np.pi * f * pt) + 0.3 * np.sin(4 * np.pi * f * pt))
            seg = seg[: n - i0]
            pan = 0.5 + 0.35 * np.sin(k * 0.7)
            out[0, i0:i0 + len(seg)] += seg * (1 - pan) * 2
            out[1, i0:i0 + len(seg)] += seg * pan * 2
        if mood == "upbeat":  # soft kick on the beat
            kl = int(0.25 * RATE)
            kt = np.arange(kl) / RATE
            kick = 0.18 * np.exp(-kt * 18) * np.sin(2 * np.pi * (50 + 60 * np.exp(-kt * 30)) * kt)
            for k in range(int(seconds / beat)):
                i0 = int(k * beat * RATE)
                seg = kick[: n - i0]
                out[:, i0:i0 + len(seg)] += seg

    # Air: filtered noise with a slow swell (stronger for eerie).
    noise = _lowpass(rng.normal(0, 1, n), {"eerie": 700, "tense": 400}.get(mood, 2500))
    noise /= np.abs(noise).max() + 1e-9
    swell = 0.5 + 0.5 * np.sin(2 * np.pi * t / rng.uniform(6, 14) + rng.uniform(0, 6))
    air = {"eerie": 0.12, "tense": 0.05}.get(mood, 0.025) * swell * noise
    out[0] += air
    out[1] += np.roll(air, int(0.013 * RATE))

    out = np.stack([_lowpass(out[0], 5000), _lowpass(out[1], 5000)])
    fade_n = min(n // 4, int(1.0 * RATE))
    ramp = np.linspace(0, 1, fade_n)
    out[:, :fade_n] *= ramp
    out[:, n - fade_n:] *= ramp[::-1]
    peak = np.abs(out).max() or 1
    return (out / peak * 10 ** (-1 / 20)).astype(np.float32)


def write_stereo(path: Path, stereo: np.ndarray) -> None:
    import wave

    pcm = (np.clip(stereo.T, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(pcm.tobytes())


AUDIO_EXT = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".opus"}


def make_bed(spec: str, seconds: float, seed: int, out_dir: Path, music_dir: Path) -> Path:
    """Render or pick a bed for `spec` ('procedural:<mood>' or 'folder:<name>'). Returns an audio path."""
    kind, _, name = spec.partition(":")
    out_dir.mkdir(parents=True, exist_ok=True)
    if kind == "procedural":
        path = out_dir / f"music_{name}_{seed}.wav"
        write_stereo(path, synthesize(name, seconds, seed))
        return path
    if kind == "folder":
        tracks = sorted(p for p in (music_dir / name).glob("*") if p.suffix.lower() in AUDIO_EXT)
        if not tracks:
            raise FileNotFoundError(f"no audio files in {music_dir / name}")
        return random.Random(seed).choice(tracks)
    raise ValueError(f"bad music spec {spec!r}")


__all__ = ["synthesize", "make_bed", "write_wav", "MOODS"]
