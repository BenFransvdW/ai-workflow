"""Free, local TTS: Piper (neural, runs well on a Pi 5 CPU) and espeak-ng (robotic last resort)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from .base import TTSError, TTSProvider


class PiperTTS(TTSProvider):
    """Uses `python -m piper` from the piper-tts package with a downloaded .onnx voice.

    Voices go in <models_dir>/piper/, e.g. `python -m piper.download_voices en_US-ryan-high --data-dir models/piper`.
    """

    def synthesize(self, text: str, out_stem: Path) -> Path:
        data_dir = self.ctx.config.path(self.ctx.config.paths.models_dir) / "piper"
        model = self.opts["model"]
        model_path = Path(model) if model.endswith(".onnx") else data_dir / f"{model}.onnx"
        if not model_path.exists():
            raise TTSError(f"Piper voice {model_path} not found; download it first (see README)")
        out = out_stem.with_suffix(".wav")
        cmd = [sys.executable, "-m", "piper", "-m", str(model_path), "-f", str(out),
               "--sentence-silence", str(self.opts.get("sentence_silence", 0.25))]
        if self.opts.get("length_scale"):
            cmd += ["--length-scale", str(self.opts["length_scale"])]
        proc = subprocess.run(cmd, input=" ".join(text.split()), capture_output=True, text=True)
        if proc.returncode != 0 or not out.exists():
            raise TTSError(f"piper failed: {proc.stderr[-500:]}")
        return out


class EspeakTTS(TTSProvider):
    def synthesize(self, text: str, out_stem: Path) -> Path:
        exe = shutil.which("espeak-ng") or shutil.which("espeak")
        if not exe:
            raise TTSError("espeak-ng is not installed (sudo apt install espeak-ng)")
        out = out_stem.with_suffix(".wav")
        subprocess.run([exe, "-v", self.opts.get("voice", "en-us"), "-s", str(self.opts.get("speed", 165)),
                        "-w", str(out), text], check=True, capture_output=True)
        return out
