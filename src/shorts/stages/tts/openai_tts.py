"""OpenAI text-to-speech (https://platform.openai.com/docs/api-reference/audio/createSpeech)."""

from __future__ import annotations

from pathlib import Path

from .base import TTSProvider

API = "https://api.openai.com/v1/audio/speech"


class OpenAITTS(TTSProvider):
    def synthesize(self, text: str, out_stem: Path) -> Path:
        key = self.ctx.secrets.require("OPENAI_API_KEY")
        body = {
            "model": self.opts.get("model", "gpt-4o-mini-tts"),
            "voice": self.opts.get("voice", "onyx"),
            "input": text,
            "response_format": "wav",
        }
        if self.opts.get("instructions"):
            body["instructions"] = self.opts["instructions"]
        resp = self.session.post(API, headers={"Authorization": f"Bearer {key}"}, json=body, timeout=180)
        self._check(resp, "OpenAI")
        out = out_stem.with_suffix(".wav")
        out.write_bytes(resp.content)
        return out
