import json
from datetime import datetime, timezone

from shorts import db, media, records
from shorts.fixtures import placeholder_speech
from shorts.lock import FileLock
from shorts.run import run_once, top_up
from shorts.stages.fetch import store
from shorts.stages.filters import Post
from tests.conftest import FIXTURES, make_group, requires_ffmpeg
from tests.test_script import FakeClient, good_script

FIX = json.loads((FIXTURES / "story.json").read_text())
NOON = datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc)


def test_lock_contention_is_a_noop(ctx):
    calls = []
    with FileLock(ctx.data_dir / "run.lock"):
        assert run_once(ctx, uploader=lambda *a, **k: calls.append(1)) == 0
    assert calls == []


def test_paused_skips_everything(ctx):
    db.set_setting(ctx.conn, "paused", "1")
    calls = []
    assert run_once(ctx, uploader=lambda *a, **k: calls.append(1), generate=False) == 0
    assert calls == []


def test_run_expires_even_when_paused(ctx):
    gid = make_group(ctx, "rendered")
    ctx.conn.execute("UPDATE groups SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (gid,))
    db.set_setting(ctx.conn, "paused", "1")
    run_once(ctx, generate=False)
    assert records.group_videos(ctx.conn, gid)[0]["state"] == "expired"


def test_top_up_respects_backlog_and_render_cap(ctx, monkeypatch):
    produced = []
    monkeypatch.setattr("shorts.run.produce", lambda ctx_, gid, **kw: produced.append(gid) or True)
    for i in range(10):
        store(ctx, Post(id=f"s{i}", subreddit="AITAH", author="a", title=f"AITA {i}", body="w " * 200,
                        url=f"https://reddit.com/{i}", score=500))
    make_group(ctx, "awaiting_approval")  # already one in the backlog
    fetches = []
    n = top_up(ctx, fetcher=lambda c: fetches.append(1))
    assert n == 2 == len(produced)  # max_renders_per_run
    # backlog now 3 (1 awaiting + 2 new) → nothing more next time
    ctx.conn.execute("UPDATE videos SET state='awaiting_approval' WHERE state='candidate'")
    assert top_up(ctx, fetcher=lambda c: fetches.append(1)) == 0


def test_top_up_fetches_when_candidates_run_low(ctx, monkeypatch):
    monkeypatch.setattr("shorts.run.produce", lambda *a, **k: True)
    fetched = []

    def fetcher(c):
        fetched.append(1)
        store(c, Post(id="new1", subreddit="AITAH", author="a", title="AITA x", body="w " * 200, url="u", score=500))

    top_up(ctx, fetcher=fetcher)
    assert fetched and ctx.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] >= 1


@requires_ffmpeg
def test_end_to_end_run_with_mocks(ctx, monkeypatch):
    """fetch → script (mock LLM) → tts (stub) → captions → procedural bg → compose → Telegram approval → dry-run upload."""
    cfg = ctx.config
    cfg.compose.preset = "ultrafast"
    cfg.limits.max_renders_per_run = 1
    cfg.approval.backlog_target = 1
    post = FIX["post"]

    def fetcher(c):
        store(c, Post(id=post["id"], subreddit=post["subreddit"], author=post["author"], title=post["title"],
                      body=post["selftext"], url="https://www.reddit.com" + post["permalink"], score=post["score"]))

    short = good_script()
    short["parts"][0]["narration"] = "She paid for the *window* seat. A stranger took it anyway."

    class StubVoice:
        def synthesize(self, text, out_stem):
            return placeholder_speech(text, out_stem.with_suffix(".wav"))

    monkeypatch.setattr("shorts.stages.tts.get_provider", lambda *a, **k: StubVoice())
    assert run_once(ctx, client=FakeClient(short), use_whisper=False, fetcher=fetcher, now=NOON,
                    pool_budget_seconds=0) == 0

    [group] = ctx.conn.execute("SELECT * FROM groups").fetchall()
    [video] = records.group_videos(ctx.conn, group["id"])
    assert video["state"] == "rendered", video["error"]
    assert media.video_size(video["video_path"]) == (1080, 1920)
    assert media.duration(video["video_path"]) < 60
    assert "Story submitted by u/quiet_lighthouse_42" in video["description"]

    # Bot sends it, user approves; the next run uploads it (dry run) in the open slot.
    from shorts.approval.service import ApprovalService
    svc = ApprovalService(ctx)
    svc.mark_sent(group["id"], 1, [])
    svc.approve(group["id"])
    run_once(ctx, generate=False, now=NOON)
    row = ctx.conn.execute("SELECT state, dry_run FROM videos WHERE id=?", (video["id"],)).fetchone()
    assert tuple(row) == ("uploaded", 1)
