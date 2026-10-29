import json
import random

import numpy as np
import pytest

from shorts import media
from shorts.fixtures import fixture_speech, placeholder_speech
from shorts.stages.background.base import Piece, Segment
from shorts.stages.background.procedural import render_clip
from shorts.stages.captions.ass import build_ass
from shorts.stages.captions.transcribe import word_timings
from shorts.stages.compose import TooLong, _filter_path, compose, make_preview
from shorts.stages.music import MOODS, make_bed, synthesize
from tests.conftest import FIXTURES, requires_ffmpeg


@pytest.mark.parametrize("mood", MOODS)
def test_music_is_seeded_and_bounded(mood):
    a = synthesize(mood, 3, seed=1)
    b = synthesize(mood, 3, seed=1)
    c = synthesize(mood, 3, seed=2)
    assert a.shape == (2, 3 * 44100)
    assert np.array_equal(a, b) and not np.array_equal(a, c)
    assert np.abs(a).max() <= 1.0 and np.sqrt(np.mean(a ** 2)) > 0.02


def test_folder_bed(tmp_path):
    (tmp_path / "lofi").mkdir()
    (tmp_path / "lofi" / "a.mp3").write_bytes(b"x")
    assert make_bed("folder:lofi", 10, 1, tmp_path / "w", tmp_path).name == "a.mp3"
    with pytest.raises(FileNotFoundError):
        make_bed("folder:none", 10, 1, tmp_path / "w", tmp_path)


def test_filter_path_escaping():
    assert _filter_path("/a/b:c/it's,[x].ass") == "/a/b\\:c/it\\'s\\,\\[x\\].ass"


@requires_ffmpeg
def test_compose_end_to_end(cfg, tmp_path):
    """Fixture story → speech → captions → procedural background → final Short."""
    script = json.loads((FIXTURES / "story.json").read_text())["script"]
    text = " ".join([script["hook"], script["parts"][0]["narration"].split(". ")[0] + "."])
    voice, _ = fixture_speech(text, tmp_path / "voice.wav")
    words, _ = word_timings(voice, text, use_whisper=False)
    ass = tmp_path / "c.ass"
    ass.write_text(build_ass(words, cfg.captions.styles["karaoke_clean"], badge="Verdict Hour"))
    need = media.duration(voice) + 0.8
    bg = tmp_path / "bg.mp4"
    render_clip("runner", bg, width=180, height=320, fps=15, seconds=need + 0.5, seed=1, preset="ultrafast")
    cfg.compose.preset = "ultrafast"
    out = tmp_path / "out.mp4"
    res = compose(cfg, segment=Segment("procedural", [Piece(1, str(bg), 0.2, need)]), voice=voice, ass=ass,
                  music_spec="procedural:calm", music_seed=4, work=tmp_path, out=out)
    info = media.probe(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert (v["width"], v["height"]) == (1080, 1920)
    assert v["codec_name"] == "h264" and a["codec_name"] == "aac"
    assert res["duration"] < 60
    assert abs(res["duration"] - (media.duration(voice) + 0.6)) < 0.15
    assert abs(float(media.measure_loudness(out)["input_i"]) - cfg.compose.loudness_i) < 1.5
    assert res["preview"] is None


@requires_ffmpeg
def test_compose_chains_pieces_and_rejects_overlong(cfg, tmp_path):
    voice = placeholder_speech("short line here", tmp_path / "v.wav")
    clips = []
    for i in range(2):
        p = tmp_path / f"c{i}.mp4"
        media.ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=10:duration=2", "-pix_fmt", "yuv420p", p)
        clips.append(p)
    need = media.duration(voice) + 0.8
    seg = Segment("pexels", [Piece(1, str(clips[0]), 0, 1.5), Piece(2, str(clips[1]), 0, need - 1.5)])
    cfg.compose.preset = "ultrafast"
    res = compose(cfg, segment=seg, voice=voice, ass=None, music_spec=None, music_seed=0, work=tmp_path,
                  out=tmp_path / "o.mp4")
    assert media.video_size(tmp_path / "o.mp4") == (1080, 1920)
    assert res["duration"] < need + 0.2

    cfg.compose.max_seconds = 1.0
    with pytest.raises(TooLong):
        compose(cfg, segment=seg, voice=voice, ass=None, music_spec=None, music_seed=0, work=tmp_path,
                out=tmp_path / "o2.mp4")


@requires_ffmpeg
def test_preview_only_when_too_big(cfg, tmp_path):
    src = tmp_path / "s.mp4"
    media.ffmpeg("-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=10:duration=1", "-pix_fmt", "yuv420p", src)
    assert make_preview(cfg, src, tmp_path / "p.mp4") is None
    cfg.compose.telegram_max_mb = 0.00001
    assert make_preview(cfg, src, tmp_path / "p.mp4").exists()
    assert media.video_size(tmp_path / "p.mp4") == (720, 1280)
