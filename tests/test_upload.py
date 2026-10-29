import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from shorts import db, records
from shorts.stages.upload import NotUploadable, process_upload_queue, request_body, upload_video
from shorts.state import State, transition
from tests.conftest import make_group

NOON = datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc)  # after the 12:40 UTC slot


def ids(ctx, gid):
    return [v["id"] for v in records.group_videos(ctx.conn, gid)]


def test_request_body_declares_synthetic_media(ctx):
    [vid] = ids(ctx, make_group(ctx, "approved"))
    body = request_body(ctx, vid)
    assert body["status"]["containsSyntheticMedia"] is True
    assert body["status"]["selfDeclaredMadeForKids"] is False
    assert body["status"]["privacyStatus"] == "public"
    assert body["snippet"]["categoryId"] == "24"
    assert "Story submitted by u/x" in body["snippet"]["description"]
    assert "#shorts" in body["snippet"]["description"]


def test_dry_run_upload(ctx):
    [vid] = ids(ctx, make_group(ctx, "approved"))
    yt = upload_video(ctx, vid, dry_run=True)
    assert yt == f"dryrun-{vid}"
    row = ctx.conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    assert row["state"] == "uploaded" and row["dry_run"] == 1 and row["uploaded_at"]
    saved = json.loads((ctx.data_dir / "dry_run" / f"video_{vid}.json").read_text())
    assert saved["body"]["status"]["containsSyntheticMedia"] is True
    assert ctx.conn.execute("SELECT status FROM stories").fetchone()[0] == "used"


@pytest.mark.parametrize("upto", ["scripted", "rendered", "awaiting_approval"])
def test_only_approved_videos_upload(ctx, upto):
    [vid] = ids(ctx, make_group(ctx, upto))
    with pytest.raises(NotUploadable):
        upload_video(ctx, vid, dry_run=True)


def test_part_two_waits_for_part_one(ctx):
    v1, v2 = ids(ctx, make_group(ctx, "approved", parts=2))
    with pytest.raises(NotUploadable, match="part 1"):
        upload_video(ctx, v2, dry_run=True)
    upload_video(ctx, v1, dry_run=True)
    upload_video(ctx, v2, dry_run=True)


class FakeYouTube:
    def __init__(self):
        self.bodies = []

    def videos(self):
        return self

    def insert(self, part, body, media_body):
        self.bodies.append((part, body, media_body))
        return SimpleNamespace(next_chunk=lambda: (None, {"id": "yt123"}))


def test_live_upload_uses_api(ctx):
    [vid] = ids(ctx, make_group(ctx, "approved"))
    yt = FakeYouTube()
    assert upload_video(ctx, vid, dry_run=False, service=yt) == "yt123"
    part, body, media = yt.bodies[0]
    assert part == "snippet,status" and body["status"]["containsSyntheticMedia"] is True
    assert media.mimetype() == "video/mp4" and media.resumable()
    row = ctx.conn.execute("SELECT youtube_id, dry_run FROM videos WHERE id=?", (vid,)).fetchone()
    assert tuple(row) == ("yt123", 0)


def test_live_failure_marks_failed_then_retries(ctx):
    [vid] = ids(ctx, make_group(ctx, "approved"))

    class Broken(FakeYouTube):
        def insert(self, **kw):
            raise RuntimeError("quotaExceeded")

    assert process_upload_queue(ctx, dry_run=False, now=NOON, service=Broken()) is None
    assert ctx.conn.execute("SELECT state FROM videos WHERE id=?", (vid,)).fetchone()[0] == "failed"
    assert process_upload_queue(ctx, dry_run=False, now=NOON, service=FakeYouTube()) == "yt123"


def test_queue_respects_slots_cap_and_pause(ctx):
    for _ in range(4):
        make_group(ctx, "approved")
    early = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
    assert process_upload_queue(ctx, now=early) is None                  # before first slot
    assert process_upload_queue(ctx, now=NOON) is not None               # slot 1
    assert process_upload_queue(ctx, now=NOON) is None                   # slot 1 used
    db.set_setting(ctx.conn, "paused", "1")
    assert process_upload_queue(ctx, now=datetime(2026, 5, 1, 19, 0, tzinfo=timezone.utc)) is None
    db.set_setting(ctx.conn, "paused", "0")
    assert process_upload_queue(ctx, now=datetime(2026, 5, 1, 19, 0, tzinfo=timezone.utc)) is not None
    assert process_upload_queue(ctx, now=datetime(2026, 5, 1, 23, 0, tzinfo=timezone.utc)) is None  # cap 2
    assert ctx.conn.execute("SELECT COUNT(*) FROM videos WHERE state='uploaded'").fetchone()[0] == 2


def test_expired_and_rejected_never_upload(ctx):
    g1 = make_group(ctx, "awaiting_approval")
    for v in ids(ctx, g1):
        transition(ctx.conn, v, State.EXPIRED)
    g2 = make_group(ctx, "awaiting_approval")
    for v in ids(ctx, g2):
        transition(ctx.conn, v, State.REJECTED)
    assert process_upload_queue(ctx, now=NOON) is None
