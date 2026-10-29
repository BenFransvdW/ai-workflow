"""Configuration (config.yaml) and secrets (.env) loading."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PROCEDURAL_STYLES = ("bouncing", "plinko", "runner", "plasma", "flowfield")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Model):
    data_dir: Path = Path("data")
    fonts_dir: Path = Path("assets/fonts")
    music_dir: Path = Path("assets/music")
    models_dir: Path = Path("models")


class RedditConfig(_Model):
    subreddits: list[str]
    listing: Literal["top", "hot", "new"] = "top"
    time_filter: Literal["hour", "day", "week", "month", "year", "all"] = "week"
    limit_per_subreddit: int = 40
    min_score: int = 100
    min_words: int = 120
    max_words: int = 1100
    min_candidates: int = 8
    blocklist: list[str] = Field(default_factory=list)


class LLMConfig(_Model):
    model: str = "claude-sonnet-5-5"
    max_tokens: int = 4000
    max_retries: int = 2
    words_per_minute: int = 165
    max_part_seconds: int = 52
    max_parts: int = 3
    verbatim_ngram: int = 8
    similarity_threshold: float = 0.35
    history_size: int = 60


class VoiceConfig(BaseModel):
    model_config = ConfigDict(extra="allow")
    provider: Literal["elevenlabs", "openai", "piper", "espeak"]


class TTSConfig(_Model):
    voices: dict[str, VoiceConfig]


HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class CaptionStyle(_Model):
    font: str = "DejaVu Sans"
    bold: bool = True
    size: int = 96
    primary: str = "#FFFFFF"
    highlight: str = "#FFD60A"
    outline: str = "#000000"
    outline_width: float = 8
    shadow: float = 2
    uppercase: bool = True
    animation: Literal["pop", "fade", "bounce", "karaoke", "none"] = "pop"
    position_y: float = Field(0.5, ge=0.1, le=0.9)

    @field_validator("primary", "highlight", "outline")
    @classmethod
    def _hex(cls, v: str) -> str:
        if not HEX.match(v):
            raise ValueError(f"colour must be #RRGGBB, got {v!r}")
        return v


class CaptionsConfig(_Model):
    whisper_model: str = "small"
    whisper_compute_type: str = "int8"
    max_words: int = Field(3, ge=1, le=5)
    max_chars: int = 16
    styles: dict[str, CaptionStyle]


class ProceduralRenderConfig(_Model):
    width: int = 540
    height: int = 960
    fps: int = 30
    clip_seconds: float = 62
    crf: int = 20
    preset: str = "veryfast"


class PexelsConfig(_Model):
    themes: list[str] = Field(default_factory=lambda: ["satisfying"])
    per_page: int = 10
    max_cached_clips: int = 60
    reuse_days: int = 30
    min_height: int = 1280


class LocalFolderConfig(_Model):
    path: Path | None = None


class BackgroundConfig(_Model):
    pool_size_per_style: int = 2
    max_uses_per_clip: int = 2  # procedural clips are retired (and re-rendered) after this many uses
    procedural: ProceduralRenderConfig = ProceduralRenderConfig()
    pexels: PexelsConfig = PexelsConfig()
    local_folder: LocalFolderConfig = LocalFolderConfig()


class MusicConfig(_Model):
    volume_db: float = -22
    beds: dict[str, str]

    @field_validator("beds")
    @classmethod
    def _beds(cls, v: dict[str, str]) -> dict[str, str]:
        for name, spec in v.items():
            if not re.match(r"^(procedural|folder):[\w-]+$", spec):
                raise ValueError(f"music bed {name!r}: expected 'procedural:<mood>' or 'folder:<dir>'")
        return v


class ComposeConfig(_Model):
    width: int = 1080
    height: int = 1920
    fps: int = 30
    crf: int = 21
    preset: str = "veryfast"
    max_seconds: float = Field(59, le=60)
    max_speedup: float = 1.12
    loudness_i: float = -14
    loudness_tp: float = -1.5
    loudness_lra: float = 11
    telegram_max_mb: float = 48


class SeriesBackground(_Model):
    provider: Literal["procedural", "pexels", "local_folder"]
    styles: list[str] = Field(default_factory=list)
    themes: list[str] = Field(default_factory=list)
    weight: float = 1

    @model_validator(mode="after")
    def _check(self) -> "SeriesBackground":
        bad = [s for s in self.styles if s not in PROCEDURAL_STYLES]
        if bad:
            raise ValueError(f"unknown procedural styles {bad}; known: {PROCEDURAL_STYLES}")
        if self.provider == "procedural" and not self.styles:
            self.styles = list(PROCEDURAL_STYLES)
        return self


class SeriesConfig(_Model):
    name: str
    display_name: str
    subreddits: list[str] = Field(default_factory=list)  # empty = any
    voices: list[str]
    caption_styles: list[str]
    backgrounds: list[SeriesBackground]
    music: list[str]
    pacing: float = Field(1.0, ge=0.8, le=1.25)
    tone: str = ""
    ending: str = "End with your own short take on the story."
    title_prefix: str = ""


class LimitsConfig(_Model):
    max_uploads_per_day: int = 2
    posting_times: list[str] = Field(default_factory=lambda: ["12:40", "18:40"])
    max_renders_per_run: int = 2
    max_attempts: int = 3

    @field_validator("posting_times")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        for t in v:
            if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", t):
                raise ValueError(f"posting time must be HH:MM, got {t!r}")
        return sorted(v)


class ApprovalConfig(_Model):
    window_hours: float = 48
    backlog_target: int = 3
    poll_seconds: int = 30
    trigger_run_on_regenerate: bool = True


class UploadConfig(_Model):
    dry_run: bool = True
    privacy_status: Literal["public", "unlisted", "private"] = "public"
    category_id: str = "24"
    default_language: str = "en"


class Config(_Model):
    timezone: str = "UTC"
    paths: PathsConfig = PathsConfig()
    reddit: RedditConfig
    llm: LLMConfig = LLMConfig()
    tts: TTSConfig
    captions: CaptionsConfig
    background: BackgroundConfig = BackgroundConfig()
    music: MusicConfig
    compose: ComposeConfig = ComposeConfig()
    series: list[SeriesConfig]
    limits: LimitsConfig = LimitsConfig()
    approval: ApprovalConfig = ApprovalConfig()
    upload: UploadConfig = UploadConfig()

    root: Path = Path(".")  # set by load_config; not read from YAML

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        ZoneInfo(v)  # raises if unknown
        return v

    @model_validator(mode="after")
    def _cross_refs(self) -> "Config":
        names = set()
        for s in self.series:
            if s.name in names:
                raise ValueError(f"duplicate series name {s.name!r}")
            names.add(s.name)
            for v in s.voices:
                if v not in self.tts.voices:
                    raise ValueError(f"series {s.name}: unknown voice {v!r}")
            for c in s.caption_styles:
                if c not in self.captions.styles:
                    raise ValueError(f"series {s.name}: unknown caption style {c!r}")
            for m in s.music:
                if m not in self.music.beds:
                    raise ValueError(f"series {s.name}: unknown music bed {m!r}")
        if not self.series:
            raise ValueError("at least one series is required")
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def path(self, p: Path | str) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (self.root / p)

    @property
    def data_dir(self) -> Path:
        return self.path(self.paths.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "shorts.db"

    def series_by_name(self, name: str) -> SeriesConfig:
        for s in self.series:
            if s.name == name:
                return s
        raise KeyError(name)


def load_config(path: str | Path = "config.yaml") -> Config:
    path = Path(path).resolve()
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = Config.model_validate(raw)
    cfg.root = path.parent
    return cfg


class Secrets:
    """Read-only view over environment variables (populated from .env)."""

    def __init__(self, env_file: str | Path | None = None):
        if env_file is not None:
            load_dotenv(env_file, override=False)

    def get(self, key: str, default: str | None = None) -> str | None:
        val = os.environ.get(key, default)
        return val if val not in ("", None) else default

    def require(self, key: str) -> str:
        val = self.get(key)
        if not val:
            raise RuntimeError(f"missing secret {key}; set it in .env (see .env.example)")
        return val
