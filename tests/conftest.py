from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from shorts import db
from shorts.config import Config, Secrets
from shorts.context import Ctx

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


requires_ffmpeg = pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg not installed")


@pytest.fixture
def raw_config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text())


@pytest.fixture
def cfg(raw_config, tmp_path) -> Config:
    raw_config["paths"]["data_dir"] = str(tmp_path / "data")
    raw_config["paths"]["fonts_dir"] = str(ROOT / "assets/fonts")
    raw_config["paths"]["music_dir"] = str(tmp_path / "music")
    raw_config["paths"]["models_dir"] = str(tmp_path / "models")
    # Small, fast procedural renders for tests.
    raw_config["background"]["procedural"].update(width=180, height=320, fps=10, clip_seconds=3, preset="ultrafast")
    c = Config.model_validate(raw_config)
    c.root = tmp_path
    return c


@pytest.fixture
def ctx(cfg, monkeypatch) -> Ctx:
    for k in ("TELEGRAM_CHAT_ID", "TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "PEXELS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    conn = db.connect(cfg.db_path)
    yield Ctx(config=cfg, conn=conn, secrets=Secrets())
    conn.close()


@pytest.fixture
def story_fixture() -> dict:
    return json.loads((FIXTURES / "story.json").read_text())


def insert_story(conn, **overrides) -> int:
    base = dict(
        reddit_id="t3_abc", subreddit="AmItheAsshole", author="throwaway_writer", title="AITA for a thing?",
        body="word " * 200, url="https://www.reddit.com/r/AmItheAsshole/comments/abc/", score=500,
        word_count=200, created_utc=1.7e9, fetched_at=db.now_iso(), status="candidate",
    )
    base.update(overrides)
    cols = ",".join(base)
    cur = conn.execute(f"INSERT INTO stories({cols}) VALUES ({','.join('?' for _ in base)})", list(base.values()))
    return cur.lastrowid
