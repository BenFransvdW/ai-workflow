"""ElevenLabs text-to-speech (https://elevenlabs.io/docs/api-reference/text-to-speech/convert)."""

from __future__ import annotations

from pathlib import Path

from .base import TTSProvider

API = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"


class ElevenLabsTTS(TTSProvider):
    def synthesize(self, text: str, out_stem: Path) -> Path:
        key = self.ctx.secrets.require("ELEVENLABS_API_KEY")
        resp = self.session.post(
            API.format(voice_id=self.opts["voice_id"]),
            params={"output_format": self.opts.get("output_format", "mp3_44100_128")},
            headers={"xi-api-key": key, "accept": "audio/mpeg", "content-type": "application/json"},
            json={
                "text": text,
                "model_id": self.opts.get("model", "eleven_multilingual_v2"),
                "voice_settings": {
                    "stability": self.opts.get("stability", 0.45),
                    "similarity_boost": self.opts.get("similarity_boost", 0.8),
                    "style": self.opts.get("style", 0.2),
                    "use_speaker_boost": True,
                },
            },
            timeout=180,
        )
        self._check(resp, "ElevenLabs")
        out = out_stem.with_suffix(".mp3")
        out.write_bytes(resp.content)
        return out
