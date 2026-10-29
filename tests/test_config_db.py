import sqlite3
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from shorts import db, schedule
from shorts.config import Config, load_config
from shorts.lock import FileLock, LockHeld
from tests.conftest import ROOT, insert_story


def test_repo_config_loads():
    cfg = load_config(ROOT / "config.yaml")
    assert cfg.upload.dry_run is True
    assert cfg.limits.max_uploads_per_day == 2
    assert cfg.approval.window_hours == 48
    assert cfg.approval.backlog_target == 3
    assert len(cfg.series) >= 3


def test_unknown_voice_in_series_rejected(raw_config):
    raw_config["series"][0]["voices"] = ["nope"]
    with pytest.raises(ValidationError, match="unknown voice"):
        Config.model_validate(raw_config)


def test_unknown_procedural_style_rejected(raw_config):
    raw_config["series"][0]["backgrounds"] = [{"provider": "procedural", "styles": ["minecraft"]}]
    with pytest.raises(ValidationError, match="unknown procedural styles"):
        Config.model_validate(raw_config)


def test_bad_colour_and_time_rejected(raw_config):
    raw_config["captions"]["styles"]["bold_pop"]["primary"] = "white"
    with pytest.raises(ValidationError):
        Config.model_validate(raw_config)


def test_story_dedupe(ctx):
    insert_story(ctx.conn, reddit_id="t3_x")
    with pytest.raises(sqlite3.IntegrityError):
        insert_story(ctx.conn, reddit_id="t3_x")


def test_settings_roundtrip(ctx):
    assert not db.is_paused(ctx.conn)
    db.set_setting(ctx.conn, "paused", "1")
    assert db.is_paused(ctx.conn)


def test_lock_is_exclusive(tmp_path):
    with FileLock(tmp_path / "run.lock"):
        with pytest.raises(LockHeld):
            FileLock(tmp_path / "run.lock").acquire()
    FileLock(tmp_path / "run.lock").acquire().release()


def _uploaded_at(ctx, when: datetime):
    from shorts import records
    sid = insert_story(ctx.conn, reddit_id=f"t3_{when.timestamp()}")
    gid = records.create_group(ctx.conn, sid, "verdict_hour")
    ctx.conn.execute("UPDATE videos SET state='uploaded', uploaded_at=? WHERE group_id=?",
                     (when.astimezone(timezone.utc).isoformat(timespec="seconds"), gid))


def test_posting_slots_and_cap(ctx):
    cfg = ctx.config  # UTC, slots 12:40 and 18:40, cap 2
    morning = datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)
    ok, why = schedule.can_upload_now(ctx.conn, cfg, morning)
    assert not ok and "no posting slot" in why
    assert schedule.next_upload_time(ctx.conn, cfg, morning).hour == 12

    noon = datetime(2026, 5, 1, 13, 0, tzinfo=timezone.utc)
    assert schedule.can_upload_now(ctx.conn, cfg, noon)[0]
    _uploaded_at(ctx, noon)
    ok, why = schedule.can_upload_now(ctx.conn, cfg, datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc))
    assert not ok and "used" in why

    evening = datetime(2026, 5, 1, 19, 0, tzinfo=timezone.utc)
    assert schedule.can_upload_now(ctx.conn, cfg, evening)[0]
    _uploaded_at(ctx, evening)
    ok, why = schedule.can_upload_now(ctx.conn, cfg, datetime(2026, 5, 1, 23, 0, tzinfo=timezone.utc))
    assert not ok and "cap" in why
    nxt = schedule.next_upload_time(ctx.conn, cfg, datetime(2026, 5, 1, 23, 0, tzinfo=timezone.utc))
    assert (nxt.day, nxt.hour) == (2, 12)


def test_missed_slots_do_not_burst(ctx):
    cfg = ctx.config
    late = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)  # both slots passed, nothing uploaded
    assert schedule.can_upload_now(ctx.conn, cfg, late)[0]
    _uploaded_at(ctx, late)
    assert not schedule.can_upload_now(ctx.conn, cfg, datetime(2026, 5, 1, 20, 30, tzinfo=timezone.utc))[0]
