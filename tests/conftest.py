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


def make_group(ctx, upto: str = "awaiting_approval", parts: int = 1, series: str = "verdict_hour",
               reddit_id: str | None = None, with_file: bool = True) -> int:
    """A story + group walked through the state machine up to `upto`, with a tiny dummy mp4 per part."""
    import json as _json

    from shorts import records
    from shorts.state import State, transition_group

    sid = insert_story(ctx.conn, reddit_id=reddit_id or f"t3_{ctx.conn.execute('SELECT COUNT(*) FROM stories').fetchone()[0]}")
    gid = records.create_group(ctx.conn, sid, series, voice="ryan_piper", caption_style="bold_pop", music_bed="tense")
    for i in range(2, parts + 1):
        records.create_video(ctx.conn, gid, i, parts)
    order = ["scripted", "rendered", "awaiting_approval", "approved"]
    for v in records.group_videos(ctx.conn, gid):
        path = ctx.work_dir("videos", v["id"]) / f"short_{v['id']}.mp4"
        if with_file:
            path.write_bytes(b"\x00" * 1024)
        suffix = f" (Part {v['part_index']}/{parts})" if parts > 1 else ""
        ctx.conn.execute(
            "UPDATE videos SET part_count=?, title=?, description=?, tags_json=?, video_path=?, duration=30 WHERE id=?",
            (parts, f"A Story Title{suffix}", "Teaser.\n\nStory submitted by u/x on r/AITAH, adapted.",
             _json.dumps(["aita", "reddit stories"]), str(path), v["id"]))
    for st in order[: order.index(upto) + 1] if upto in order else []:
        transition_group(ctx.conn, gid, State(st), "test setup")
    return gid
