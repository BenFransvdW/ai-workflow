"""Offline preflight checks: `python -m shorts doctor`.

Reports what is missing before a run fails halfway through (binaries, secrets, models, fonts).
Only inspects local state; never touches the network or any real API.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .context import Ctx

OK, WARN, FAIL = "ok", "warn", "fail"

_VOICE_SECRETS = {"elevenlabs": "ELEVENLABS_API_KEY", "openai": "OPENAI_API_KEY"}


@dataclass
class Check:
    level: str
    name: str
    detail: str = ""


def _used_voices(ctx: Ctx) -> dict:
    names = {v for s in ctx.config.series for v in s.voices}
    return {n: ctx.config.tts.voices[n] for n in sorted(names)}


def _uses_provider(ctx: Ctx, provider: str) -> bool:
    return any(b.provider == provider for s in ctx.config.series for b in s.backgrounds)


def run_checks(ctx: Ctx) -> list[Check]:
    cfg, sec = ctx.config, ctx.secrets
    out: list[Check] = []

    def add(level: str, name: str, detail: str = "") -> None:
        out.append(Check(level, name, detail))

    for exe in ("ffmpeg", "ffprobe"):
        found = shutil.which(exe)
        add(OK if found else FAIL, exe, "" if found else "not on PATH (sudo apt install ffmpeg)")

    for key in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "ANTHROPIC_API_KEY",
                "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        add(OK if sec.get(key) else FAIL, key, "" if sec.get(key) else "missing in .env")

    for vname, v in _used_voices(ctx).items():
        prov = v.provider
        if prov in _VOICE_SECRETS:
            key = _VOICE_SECRETS[prov]
            add(OK if sec.get(key) else WARN, f"voice {vname}", "" if sec.get(key) else f"{key} missing; voice will fail")
        elif prov == "piper":
            model = str(getattr(v, "model", "") or "")
            path = Path(model) if model.endswith(".onnx") else cfg.path(cfg.paths.models_dir) / "piper" / f"{model}.onnx"
            add(OK if path.exists() else WARN, f"voice {vname}", "" if path.exists() else f"Piper model {path} not downloaded")
        elif prov == "espeak":
            found = shutil.which("espeak-ng") or shutil.which("espeak")
            add(OK if found else WARN, f"voice {vname}", "" if found else "espeak-ng not installed")

    if _uses_provider(ctx, "pexels"):
        have = sec.get("PEXELS_API_KEY")
        add(OK if have else WARN, "PEXELS_API_KEY", "" if have else "missing; pexels backgrounds fall back to procedural")
    if _uses_provider(ctx, "local_folder"):
        p = cfg.background.local_folder.path
        ok = p is not None and cfg.path(p).is_dir()
        add(OK if ok else WARN, "local_folder background", "" if ok else "background.local_folder.path is unset or missing")

    fonts = cfg.path(cfg.paths.fonts_dir)
    add(OK if fonts.is_dir() else WARN, "fonts dir", "" if fonts.is_dir() else f"{fonts} not found; libass will use default fonts")

    live = not cfg.upload.dry_run
    level = FAIL if live else WARN
    for key in ("YOUTUBE_CLIENT_SECRETS", "YOUTUBE_TOKEN_FILE"):
        if sec.get(key):
            add(OK, key)
        else:
            add(level, key, "missing in .env" + ("" if live else " (needed before going live)"))
    token = sec.get("YOUTUBE_TOKEN_FILE")
    if token and not Path(token).exists():
        add(level, "youtube token", "token file missing; run `python -m shorts auth-youtube`")

    if live:
        add(WARN, "upload.dry_run", "false: uploads are LIVE")
        if cfg.upload.privacy_status == "public":
            add(WARN, "upload.privacy_status", "public; unverified API projects are forced to private by Google")
    else:
        add(OK, "upload.dry_run", "true: uploads only write request bodies")

    return out


def format_report(checks: list[Check]) -> str:
    icon = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]"}
    lines = [f"{icon[c.level]} {c.name}" + (f": {c.detail}" if c.detail else "") for c in checks]
    n = {lv: sum(c.level == lv for c in checks) for lv in (OK, WARN, FAIL)}
    lines.append(f"{n[OK]} ok, {n[WARN]} warnings, {n[FAIL]} failures")
    return "\n".join(lines)


def doctor(ctx: Ctx) -> int:
    """Print the report; return 1 if any check failed."""
    checks = run_checks(ctx)
    print(format_report(checks))
    return 1 if any(c.level == FAIL for c in checks) else 0
