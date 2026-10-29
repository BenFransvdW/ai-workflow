"""Styled ASS subtitles: big, centred 1–3 word chunks with per-style animation."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ...config import CaptionStyle
from .align import Word

PLAY_W, PLAY_H = 1080, 1920
_BREAK_AFTER = re.compile(r"[.!?;:,—…]$")


@dataclass
class Chunk:
    words: list[Word]

    @property
    def start(self) -> float:
        return self.words[0].start

    @property
    def end(self) -> float:
        return self.words[-1].end


def display(token: str, upper: bool) -> tuple[str, bool]:
    """Caption text for a token (drops trailing commas/periods, keeps ?!) and whether it's emphasised."""
    emph = "*" in token
    t = token.replace("*", "")
    t = re.sub(r"[,.;:]+$", "", t)
    t = t.strip("\"“”()")
    return (t.upper() if upper else t), emph


def chunk_words(words: list[Word], max_words: int = 3, max_chars: int = 16, pause: float = 0.35) -> list[Chunk]:
    chunks: list[Chunk] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        text_len = sum(len(display(x.text, False)[0]) + 1 for x in cur) + len(display(w.text, False)[0])
        if cur and (len(cur) >= max_words or text_len > max_chars or w.start - cur[-1].end > pause):
            chunks.append(Chunk(cur))
            cur = []
        cur.append(w)
        if _BREAK_AFTER.search(w.text.replace("*", "")):
            chunks.append(Chunk(cur))
            cur = []
    if cur:
        chunks.append(Chunk(cur))
    return chunks


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


def header(style: CaptionStyle, name: str = "Caption") -> str:
    bold = -1 if style.bold else 0
    return "\n".join([
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {PLAY_W}",
        f"PlayResY: {PLAY_H}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: {name},{style.font},{style.size},{ass_color(style.primary)},{ass_color(style.highlight)},"
        f"{ass_color(style.outline)},{ass_color('#000000', 0x80)},{bold},0,0,0,100,100,0,0,1,{style.outline_width},"
        f"{style.shadow},5,90,90,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]) + "\n"


def _words_text(chunk: Chunk, style: CaptionStyle, current: int | None = None) -> str:
    parts = []
    for i, w in enumerate(chunk.words):
        text, emph = display(w.text, style.uppercase)
        text = _escape(text)
        if i == current:
            parts.append(f"{{\\c{ass_color(style.highlight)}\\fscx112\\fscy112}}{text}"
                         f"{{\\c{ass_color(style.primary)}\\fscx100\\fscy100}}")
        elif emph and current is None:
            parts.append(f"{{\\c{ass_color(style.highlight)}}}{text}{{\\c{ass_color(style.primary)}}}")
        else:
            parts.append(text)
    return " ".join(parts)


def build_ass(words: list[Word], style: CaptionStyle, *, max_words: int = 3, max_chars: int = 16,
              tail: float = 0.25) -> str:
    chunks = chunk_words(words, max_words=max_words, max_chars=max_chars)
    x, y = PLAY_W // 2, int(PLAY_H * style.position_y)
    lines = [header(style)]
    for k, ch in enumerate(chunks):
        nxt = chunks[k + 1].start if k + 1 < len(chunks) else None
        end = nxt if nxt is not None and nxt - ch.end < 0.5 else ch.end + tail
        pos = f"\\an5\\pos({x},{y})"
        if style.animation == "karaoke":
            for i, w in enumerate(ch.words):
                w_end = ch.words[i + 1].start if i + 1 < len(ch.words) else end
                lines.append(f"Dialogue: 0,{_ts(w.start)},{_ts(w_end)},Caption,,0,0,0,,{{{pos}}}"
                             f"{_words_text(ch, style, current=i)}")
            continue
        if style.animation == "pop":
            anim = "\\fscx65\\fscy65\\t(0,80,\\fscx114\\fscy114)\\t(80,150,\\fscx100\\fscy100)"
        elif style.animation == "bounce":
            pos = f"\\an5\\move({x},{y + 60},{x},{y},0,120)"
            anim = "\\alpha&HFF&\\t(0,90,\\alpha&H00&)\\t(120,200,\\fscy92)\\t(200,260,\\fscy100)"
        elif style.animation == "fade":
            anim = "\\fad(140,90)\\blur0.6"
        else:
            anim = ""
        lines.append(f"Dialogue: 0,{_ts(ch.start)},{_ts(end)},Caption,,0,0,0,,{{{pos}{anim}}}{_words_text(ch, style)}")
    return "\n".join(lines) + "\n"
