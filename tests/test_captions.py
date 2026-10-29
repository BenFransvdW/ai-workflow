import re

import numpy as np
import pytest

from shorts import records
from shorts.config import CaptionStyle
from shorts.fixtures import placeholder_speech
from shorts.stages.captions import captions_video
from shorts.stages.captions.align import Word, align_by_energy, align_to_asr, speech_intervals, syllables, tokenize
from shorts.stages.captions.ass import ass_color, build_ass, chunk_words, display
from shorts.stages.captions.transcribe import read_wav_mono
from tests.conftest import insert_story

SCRIPT = "She paid *extra* for the window seat, and he took it anyway. Rude? Absolutely."


def test_align_uses_script_spelling_and_asr_timing():
    asr = [Word("she", 0.0, 0.2), Word("payed", 0.2, 0.5), Word("extra", 0.5, 0.9), Word("for", 0.9, 1.0),
           Word("the", 1.0, 1.1), Word("windo", 1.1, 1.4), Word("seat", 1.4, 1.8),
           Word("and", 2.0, 2.1), Word("he", 2.1, 2.2), Word("took", 2.2, 2.4),
           # "it anyway" missing from ASR entirely
           Word("rude", 3.2, 3.6), Word("absolutely", 4.0, 4.8)]
    out = align_to_asr(SCRIPT, asr)
    assert [w.text for w in out] == tokenize(SCRIPT)
    by = {w.text: w for w in out}
    assert by["*extra*"].start == pytest.approx(0.5)
    assert by["paid"].start == pytest.approx(0.2)        # replaced word keeps ASR time
    assert 2.4 <= by["it"].start < by["anyway."].start < 3.2  # interpolated gap
    assert all(a.end <= b.start + 1e-9 for a, b in zip(out, out[1:]))


def test_syllables():
    assert syllables("cat") == 1
    assert syllables("window") == 2
    assert syllables("absolutely") >= 3
    assert syllables("make") == 1


def test_chunking_rules():
    words = [Word(t, i * 0.3, i * 0.3 + 0.25) for i, t in enumerate(tokenize(SCRIPT))]
    chunks = chunk_words(words, max_words=3, max_chars=16)
    assert all(1 <= len(c.words) <= 3 for c in chunks)
    # Punctuation ends a chunk.
    for c in chunks:
        for w in c.words[:-1]:
            assert not re.search(r"[.!?,]$", w.text)
    assert max(len(" ".join(display(w.text, False)[0] for w in c.words)) for c in chunks) <= 16 or \
        any(len(c.words) == 1 for c in chunks)


def test_chunk_breaks_on_pause():
    words = [Word("one", 0, 0.2), Word("two", 0.25, 0.4), Word("three", 1.5, 1.7)]
    assert [len(c.words) for c in chunk_words(words)] == [2, 1]


def test_display_and_colours():
    assert display("*extra*", True) == ("EXTRA", True)
    assert display("seat,", False) == ("seat", False)
    assert display("Rude?", False) == ("Rude?", False)
    assert ass_color("#FFD60A") == "&H000AD6FF"


@pytest.mark.parametrize("animation", ["pop", "fade", "bounce", "karaoke", "none"])
def test_build_ass(animation):
    style = CaptionStyle(animation=animation, font="Inter Black", primary="#FFFFFF", highlight="#FFD60A")
    words = [Word(t, i * 0.3, i * 0.3 + 0.25) for i, t in enumerate(tokenize(SCRIPT))]
    ass = build_ass(words, style)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert "Style: Caption,Inter Black," in ass
    events = [l for l in ass.splitlines() if l.startswith("Dialogue:")]
    if animation == "karaoke":
        assert len(events) == len(words)  # one event per spoken word
        assert all("\\c&H000AD6FF" in e for e in events)
    else:
        assert len(events) == len(chunk_words(words))
        assert "EXTRA" in ass if style.uppercase else "extra" in ass
        assert "\\c&H000AD6FF" in ass  # *emphasis* coloured
    assert "{" not in "".join(e.split("}")[-1] for e in events)  # no stray override braces in text


def test_energy_alignment_tracks_placeholder_speech(tmp_path):
    wav = placeholder_speech(SCRIPT, tmp_path / "p.wav")
    samples, rate = read_wav_mono(wav)
    spans = speech_intervals(samples, rate)
    assert len(spans) == 4  # word gaps merge; punctuation pauses split ("seat," / "anyway." / "Rude?")
    words = align_by_energy(SCRIPT, samples, rate)
    assert [w.text for w in words] == tokenize(SCRIPT)
    total = len(samples) / rate
    assert words[0].start < 0.4 and words[-1].end <= total + 1e-6
    # Sentence starts land near the start of a speech burst after a pause.
    rude = next(w for w in words if w.text == "Rude?")
    assert rude.start == pytest.approx(spans[2][0], abs=0.02)


def test_captions_stage_writes_ass(ctx, tmp_path):
    sid = insert_story(ctx.conn)
    gid = records.create_group(ctx.conn, sid, "verdict_hour", caption_style="bold_pop")
    vid = records.group_videos(ctx.conn, gid)[0]["id"]
    wav = placeholder_speech(SCRIPT, tmp_path / "a.wav")
    import json
    ctx.conn.execute("UPDATE videos SET audio_path=?, script_json=? WHERE id=?",
                     (str(wav), json.dumps({"text": SCRIPT}), vid))
    out = captions_video(ctx, vid, use_whisper=False)
    assert out.exists() and "Dialogue:" in out.read_text()
    row = ctx.conn.execute("SELECT words_json FROM videos WHERE id=?", (vid,)).fetchone()
    assert len(json.loads(row[0])) == len(tokenize(SCRIPT))
