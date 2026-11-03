from __future__ import annotations

from shorts import doctor as doc

ALL_CORE = {
    "REDDIT_CLIENT_ID": "x", "REDDIT_CLIENT_SECRET": "x", "ANTHROPIC_API_KEY": "x",
    "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "1",
}


def _levels(checks):
    return {c.name: c.level for c in checks}


def _clear(monkeypatch):
    for k in list(ALL_CORE) + ["PEXELS_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY",
                               "YOUTUBE_CLIENT_SECRETS", "YOUTUBE_TOKEN_FILE"]:
        monkeypatch.delenv(k, raising=False)


def test_missing_core_secrets_fail(ctx, monkeypatch):
    _clear(monkeypatch)
    lv = _levels(doc.run_checks(ctx))
    for k in ALL_CORE:
        assert lv[k] == doc.FAIL
    assert doc.doctor(ctx) == 1


def test_core_secrets_present_pass(ctx, monkeypatch):
    _clear(monkeypatch)
    for k, v in ALL_CORE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(doc.shutil, "which", lambda exe: f"/usr/bin/{exe}")
    checks = doc.run_checks(ctx)
    assert all(c.level != doc.FAIL for c in checks), doc.format_report(checks)
    assert doc.doctor(ctx) == 0


def test_missing_ffmpeg_fails(ctx, monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setattr(doc.shutil, "which", lambda exe: None)
    lv = _levels(doc.run_checks(ctx))
    assert lv["ffmpeg"] == lv["ffprobe"] == doc.FAIL


def test_paid_voice_without_key_warns_only(ctx, monkeypatch):
    _clear(monkeypatch)
    lv = _levels(doc.run_checks(ctx))
    paid = [n for n, v in doc._used_voices(ctx).items() if v.provider in ("openai", "elevenlabs")]
    assert paid
    assert all(lv[f"voice {n}"] == doc.WARN for n in paid)
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    lv = _levels(doc.run_checks(ctx))
    assert all(lv[f"voice {n}"] == doc.OK for n in paid)


def test_piper_model_presence(ctx, monkeypatch):
    _clear(monkeypatch)
    piper = [n for n, v in doc._used_voices(ctx).items() if v.provider == "piper"]
    assert piper
    assert _levels(doc.run_checks(ctx))[f"voice {piper[0]}"] == doc.WARN
    d = ctx.config.path(ctx.config.paths.models_dir) / "piper"
    d.mkdir(parents=True)
    for n in piper:
        (d / f"{ctx.config.tts.voices[n].model}.onnx").write_bytes(b"")
    lv = _levels(doc.run_checks(ctx))
    assert all(lv[f"voice {n}"] == doc.OK for n in piper)


def test_youtube_warn_in_dry_run_fail_when_live(ctx, monkeypatch):
    _clear(monkeypatch)
    assert ctx.config.upload.dry_run
    lv = _levels(doc.run_checks(ctx))
    assert lv["YOUTUBE_CLIENT_SECRETS"] == doc.WARN
    ctx.config.upload.dry_run = False
    lv = _levels(doc.run_checks(ctx))
    assert lv["YOUTUBE_CLIENT_SECRETS"] == lv["YOUTUBE_TOKEN_FILE"] == doc.FAIL
    assert lv["upload.dry_run"] == doc.WARN


def test_live_token_file_missing_fails(ctx, monkeypatch, tmp_path):
    _clear(monkeypatch)
    ctx.config.upload.dry_run = False
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRETS", str(tmp_path / "cs.json"))
    monkeypatch.setenv("YOUTUBE_TOKEN_FILE", str(tmp_path / "tok.json"))
    assert _levels(doc.run_checks(ctx))["youtube token"] == doc.FAIL
    (tmp_path / "tok.json").write_text("{}")
    assert "youtube token" not in _levels(doc.run_checks(ctx))


def test_report_format_and_summary(ctx, monkeypatch, capsys):
    _clear(monkeypatch)
    doc.doctor(ctx)
    out = capsys.readouterr().out
    assert "[FAIL] ANTHROPIC_API_KEY" in out
    assert out.strip().splitlines()[-1].endswith("failures")


def test_cli_doctor_exit_code(cfg, monkeypatch, tmp_path):
    import yaml

    from shorts import cli
    _clear(monkeypatch)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg.model_dump(mode="json", exclude={"root"})))
    assert cli.main(["--config", str(cfg_path), "--env", str(tmp_path / "none.env"), "doctor"]) == 1
