"""Thin ffmpeg/ffprobe helpers."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

log = logging.getLogger("shorts.media")


class FFmpegError(RuntimeError):
    pass


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess:
    log.debug("exec: %s", " ".join(map(str, cmd)))
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise FFmpegError(f"{cmd[0]} failed ({proc.returncode}):\n{tail}")
    return proc


def ffmpeg(*args: str | Path) -> subprocess.CompletedProcess:
    return run(["ffmpeg", "-y", "-hide_banner", "-nostdin", "-loglevel", "error", *args])


def probe(path: str | Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]).stdout
    return json.loads(out)


def duration(path: str | Path) -> float:
    return float(probe(path)["format"]["duration"])


def video_size(path: str | Path) -> tuple[int, int]:
    for s in probe(path)["streams"]:
        if s.get("codec_type") == "video":
            return int(s["width"]), int(s["height"])
    raise FFmpegError(f"no video stream in {path}")


def measure_loudness(path: str | Path) -> dict:
    """Integrated loudness etc. via loudnorm's analysis pass (JSON on stderr)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    err = proc.stderr
    start = err.rfind("{")
    end = err.rfind("}")
    if proc.returncode != 0 or start < 0:
        raise FFmpegError(f"loudness analysis failed for {path}: {err[-500:]}")
    return json.loads(err[start:end + 1])
