import json
from types import SimpleNamespace

import pytest

from shorts import media, records
from shorts.fixtures import placeholder_speech
from shorts.stages.tts import get_provider, narration_text, postprocess, tts_video
from shorts.stages.tts.base import TTSError
from tests.conftest import insert_story, requires_ffmpeg


class FakeSession:
    def __init__(self, content=b"audio", status=200):
        self.content, self.status = content, status
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return SimpleNamespace(status_code=self.status, content=self.content, text="error body")


def test_elevenlabs_request(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
    s = FakeSession(b"ID3mp3")
    out = get_provider(ctx, "adam_eleven", s).synthesize("Hello there.", tmp_path / "v")
    url, kw = s.calls[0]
    assert url.endswith("/v1/text-to-speech/pNInz6obpgDQGcFmaJgB")
    assert kw["headers"]["xi-api-key"] == "el-key"
    assert kw["json"]["text"] == "Hello there." and kw["json"]["model_id"] == "eleven_multilingual_v2"
    assert out.suffix == ".mp3" and out.read_bytes() == b"ID3mp3"


def test_openai_request(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    s = FakeSession(b"RIFFwav")
    out = get_provider(ctx, "nova_openai", s).synthesize("Hi.", tmp_path / "v")
    url, kw = s.calls[0]
    assert url == "https://api.openai.com/v1/audio/speech"
    assert kw["headers"]["Authorization"] == "Bearer sk-test"
    assert kw["json"]["voice"] == "nova" and kw["json"]["response_format"] == "wav"
    assert "instructions" in kw["json"]
    assert out.read_bytes() == b"RIFFwav"


def test_http_error_raises(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(TTSError, match="401"):
        get_provider(ctx, "onyx_openai", FakeSession(status=401)).synthesize("Hi.", tmp_path / "v")


def test_missing_key_raises(ctx, tmp_path, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ELEVENLABS_API_KEY"):
        get_provider(ctx, "adam_eleven", FakeSession()).synthesize("Hi.", tmp_path / "v")


def test_piper_invocation(ctx, tmp_path, monkeypatch):
    model_dir = ctx.config.path(ctx.config.paths.models_dir) / "piper"
    model_dir.mkdir(parents=True)
    (model_dir / "en_US-ryan-high.onnx").write_bytes(b"x")
    seen = {}

    def fake_run(cmd, input, capture_output, text):
        seen["cmd"], seen["input"] = cmd, input
        out = cmd[cmd.index("-f") + 1]
        open(out, "wb").write(b"RIFF")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("shorts.stages.tts.piper.subprocess.run", fake_run)
    out = get_provider(ctx, "ryan_piper").synthesize("Line one.\nLine two.", tmp_path / "v")
    assert seen["cmd"][1:3] == ["-m", "piper"]
    assert seen["cmd"][seen["cmd"].index("-m", 3) + 1].endswith("en_US-ryan-high.onnx")
    assert seen["input"] == "Line one. Line two."
    assert out.exists()


def test_piper_missing_model(ctx, tmp_path):
    with pytest.raises(TTSError, match="not found"):
        get_provider(ctx, "amy_piper").synthesize("Hi", tmp_path / "v")


def test_narration_text_strips_emphasis():
    assert narration_text({"text": "He had a *plan*.\n Really."}) == "He had a plan. Really."


@requires_ffmpeg
def test_postprocess_trims_and_paces(tmp_path):
    import numpy as np
    from shorts.fixtures import write_wav
    from shorts.stages.captions.transcribe import read_wav_mono
    speech, rate = read_wav_mono(placeholder_speech("one two three four five six", tmp_path / "s.wav"))
    pad = np.zeros(rate)
    raw = tmp_path / "raw.wav"
    write_wav(raw, np.concatenate([pad, speech, pad]), rate)  # 1 s of silence either side
    base = postprocess(raw, tmp_path / "a.wav", 1.0)
    fast = postprocess(raw, tmp_path / "b.wav", 1.25)
    assert fast < base * 0.85
    assert base < media.duration(raw) - 1.5  # leading/trailing silence trimmed


@requires_ffmpeg
def test_tts_stage_with_stub_provider(ctx, tmp_path):
    sid = insert_story(ctx.conn)
    gid = records.create_group(ctx.conn, sid, "petty_revenge", voice="nova_openai")
    vid = records.group_videos(ctx.conn, gid)[0]["id"]
    ctx.conn.execute("UPDATE videos SET script_json=? WHERE id=?", (json.dumps({"text": "Short *test* line."}), vid))

    class Stub:
        def synthesize(self, text, out_stem):
            assert text == "Short test line."
            return placeholder_speech(text, out_stem.with_suffix(".wav"))

    out = tts_video(ctx, vid, provider=Stub())
    row = ctx.conn.execute("SELECT audio_path, duration FROM videos WHERE id=?", (vid,)).fetchone()
    assert row["audio_path"] == str(out) and row["duration"] > 0.5
