"""Word timing: map the script's own words onto timestamps from ASR (or an energy-based estimate).

Captions are always spelled exactly like the script; ASR is only used for *when* words are spoken.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass

import numpy as np

_NORM = re.compile(r"[^a-z0-9']+")


@dataclass
class Word:
    text: str
    start: float
    end: float

    def to_dict(self) -> dict:
        return asdict(self)


def tokenize(script: str) -> list[str]:
    return [t for t in script.split() if _NORM.sub("", t.lower())]


def norm(token: str) -> str:
    return _NORM.sub("", token.lower().replace("’", "'"))


def syllables(word: str) -> int:
    w = norm(word)
    if not w:
        return 1
    if w.isdigit():
        return max(1, len(w))  # numbers are spoken long
    groups = re.findall(r"[aeiouy]+", w)
    n = len(groups) - (1 if w.endswith("e") and len(groups) > 1 and not w.endswith("le") else 0)
    return max(1, n)


def _distribute(tokens: list[str], t0: float, t1: float) -> list[Word]:
    """Spread tokens across [t0, t1] proportionally to syllable count."""
    if not tokens:
        return []
    weights = np.array([syllables(t) for t in tokens], float)
    edges = t0 + (t1 - t0) * np.concatenate([[0], np.cumsum(weights)]) / weights.sum()
    return [Word(tok, float(edges[i]), float(edges[i + 1])) for i, tok in enumerate(tokens)]


def align_to_asr(script: str, asr: list[Word]) -> list[Word]:
    """Give every script token a time using difflib matches against the ASR words; interpolate gaps."""
    tokens = tokenize(script)
    if not asr:
        return []
    a = [norm(t) for t in tokens]
    b = [norm(w.text) for w in asr]
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                times[i1 + k] = (asr[j1 + k].start, asr[j1 + k].end)
        elif tag == "replace" and j2 > j1:
            # Same region spoken differently (numbers, contractions, misspellings): use ASR span.
            for k, w in enumerate(_distribute(tokens[i1:i2], asr[j1].start, asr[j2 - 1].end)):
                times[i1 + k] = (w.start, w.end)
    # Interpolate tokens the ASR missed entirely ("delete" opcodes) between their neighbours.
    out: list[Word] = []
    i = 0
    end_time = asr[-1].end
    while i < len(tokens):
        if times[i] is not None:
            out.append(Word(tokens[i], *times[i]))
            i += 1
            continue
        j = i
        while j < len(tokens) and times[j] is None:
            j += 1
        t0 = out[-1].end if out else asr[0].start
        t1 = times[j][0] if j < len(tokens) else end_time
        if t1 <= t0:
            t1 = t0 + 0.08 * (j - i)
        out.extend(_distribute(tokens[i:j], t0, t1))
        i = j
    return _monotonic(out)


def _monotonic(words: list[Word]) -> list[Word]:
    last = 0.0
    for w in words:
        w.start = max(w.start, last)
        w.end = max(w.end, w.start + 0.04)
        last = w.end
    return words


def speech_intervals(samples: np.ndarray, rate: int, frame_ms: float = 10, min_gap: float = 0.12,
                     threshold_db: float = -38) -> list[tuple[float, float]]:
    """Voice-activity intervals from short-time RMS energy (relative to the loudest frame)."""
    hop = max(1, int(rate * frame_ms / 1000))
    n = len(samples) // hop
    if n == 0:
        return []
    frames = samples[: n * hop].astype(np.float64).reshape(n, hop)
    rms = np.sqrt(np.mean(frames ** 2, axis=1)) + 1e-12
    db = 20 * np.log10(rms / rms.max())
    active = db > threshold_db
    intervals: list[list[float]] = []
    for k, on in enumerate(active):
        t = k * hop / rate
        if on:
            if intervals and t - intervals[-1][1] <= min_gap:
                intervals[-1][1] = t + hop / rate
            else:
                intervals.append([t, t + hop / rate])
    return [(a, b) for a, b in intervals if b - a >= 0.05]


def _match_pauses(est: list[float], gaps: list[float], miss_b: float = 0.6, miss_g: float = 0.4) -> list[tuple[int, int]]:
    """Monotonic DP matching of estimated phrase-boundary times to detected pause times."""
    nb, ng = len(est), len(gaps)
    cost = np.zeros((nb + 1, ng + 1))
    cost[:, 0] = np.arange(nb + 1) * miss_b
    cost[0, :] = np.arange(ng + 1) * miss_g
    for i in range(1, nb + 1):
        for j in range(1, ng + 1):
            cost[i, j] = min(cost[i - 1, j] + miss_b, cost[i, j - 1] + miss_g,
                             cost[i - 1, j - 1] + abs(est[i - 1] - gaps[j - 1]))
    pairs, i, j = [], nb, ng
    while i and j:
        if np.isclose(cost[i, j], cost[i - 1, j - 1] + abs(est[i - 1] - gaps[j - 1])):
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif np.isclose(cost[i, j], cost[i - 1, j] + miss_b):
            i -= 1
        else:
            j -= 1
    return pairs[::-1]


def align_by_energy(script: str, samples: np.ndarray, rate: int, min_pause: float = 0.18) -> list[Word]:
    """Fallback without ASR: estimate word times from the audio's energy envelope.

    1. Spread words over *speech* time only (silences skipped), weighted by syllables.
    2. Snap clause/sentence punctuation to detected pauses with a monotonic DP match, then
       redistribute words between those anchors. Keeps captions in sync sentence by sentence.
    """
    tokens = tokenize(script)
    spans = speech_intervals(samples, rate)
    if not tokens:
        return []
    total_len = len(samples) / rate
    if not spans:
        return _distribute(tokens, 0.0, total_len)
    speech = sum(b - a for a, b in spans)
    weights = np.array([syllables(t) for t in tokens], float)
    cum = np.concatenate([[0], np.cumsum(weights)]) / weights.sum() * speech
    starts = np.array([a for a, _ in spans])
    lens = np.array([b - a for a, b in spans])
    offsets = np.concatenate([[0], np.cumsum(lens)])

    def to_time(x: float) -> float:
        k = int(np.clip(np.searchsorted(offsets, x, side="right") - 1, 0, len(spans) - 1))
        return float(starts[k] + min(x - offsets[k], lens[k]))

    rough = [Word(t, to_time(cum[i]), to_time(cum[i + 1])) for i, t in enumerate(tokens)]
    pauses = [(spans[k][1], spans[k + 1][0]) for k in range(len(spans) - 1) if spans[k + 1][0] - spans[k][1] > min_pause]
    bounds = [i for i, t in enumerate(tokens[:-1]) if re.search(r"[.!?;:,—…]\*?$", t)]
    pairs = _match_pauses([rough[i].end for i in bounds], [p[0] for p in pauses])
    if not pairs:
        return _monotonic(rough)
    out: list[Word] = []
    seg_start_tok, seg_start_t = 0, spans[0][0]
    for bi, pj in pairs:
        last_tok = bounds[bi]
        out.extend(_distribute(tokens[seg_start_tok:last_tok + 1], seg_start_t, pauses[pj][0]))
        seg_start_tok, seg_start_t = last_tok + 1, pauses[pj][1]
    out.extend(_distribute(tokens[seg_start_tok:], seg_start_t, spans[-1][1]))
    return _monotonic(out)
