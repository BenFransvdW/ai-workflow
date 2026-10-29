import pytest

from shorts import records
from shorts.state import TRANSITIONS, InvalidTransition, State, can_transition, group_state, history, transition, transition_group
from tests.conftest import insert_story

S = State


def _group(ctx, parts=1):
    sid = insert_story(ctx.conn)
    gid = records.create_group(ctx.conn, sid, "verdict_hour")
    for i in range(2, parts + 1):
        records.create_video(ctx.conn, gid, i, parts)
    return gid


def _walk(ctx, vid, *states):
    for s in states:
        transition(ctx.conn, vid, s)


def test_happy_path_and_audit_log(ctx):
    gid = _group(ctx)
    vid = records.group_videos(ctx.conn, gid)[0]["id"]
    _walk(ctx, vid, S.SCRIPTED, S.RENDERED, S.AWAITING_APPROVAL, S.APPROVED, S.UPLOADED)
    assert history(ctx.conn, vid) == [
        (None, "candidate"), ("candidate", "scripted"), ("scripted", "rendered"),
        ("rendered", "awaiting_approval"), ("awaiting_approval", "approved"), ("approved", "uploaded"),
    ]
    assert ctx.conn.execute("SELECT status FROM stories").fetchone()[0] == "in_use"


@pytest.mark.parametrize("src", list(State))
@pytest.mark.parametrize("dst", list(State))
def test_transition_table_is_enforced(src, dst):
    assert can_transition(src, dst) == (dst in TRANSITIONS[src])


@pytest.mark.parametrize(
    "path,bad",
    [
        ([], S.APPROVED),                       # can't approve an unrendered candidate
        ([S.SCRIPTED], S.UPLOADED),             # can't upload without approval
        ([S.SCRIPTED, S.RENDERED], S.APPROVED),  # must be sent for approval first
        ([S.SCRIPTED, S.RENDERED, S.AWAITING_APPROVAL, S.REJECTED], S.APPROVED),  # rejected is terminal
        ([S.SCRIPTED, S.RENDERED, S.AWAITING_APPROVAL, S.EXPIRED], S.APPROVED),   # expired is terminal
    ],
)
def test_invalid_transitions_raise(ctx, path, bad):
    vid = records.group_videos(ctx.conn, _group(ctx))[0]["id"]
    _walk(ctx, vid, *path)
    with pytest.raises(InvalidTransition):
        transition(ctx.conn, vid, bad)


def test_failed_upload_can_be_retried(ctx):
    vid = records.group_videos(ctx.conn, _group(ctx))[0]["id"]
    _walk(ctx, vid, S.SCRIPTED, S.RENDERED, S.AWAITING_APPROVAL, S.APPROVED)
    transition(ctx.conn, vid, S.FAILED, error="quota")
    assert ctx.conn.execute("SELECT error FROM videos WHERE id=?", (vid,)).fetchone()[0] == "quota"
    transition(ctx.conn, vid, S.APPROVED)
    assert ctx.conn.execute("SELECT error FROM videos WHERE id=?", (vid,)).fetchone()[0] is None


def test_group_transition_moves_all_parts(ctx):
    gid = _group(ctx, parts=2)
    transition_group(ctx.conn, gid, S.SCRIPTED)
    transition_group(ctx.conn, gid, S.RENDERED)
    transition_group(ctx.conn, gid, S.AWAITING_APPROVAL)
    moved = transition_group(ctx.conn, gid, S.APPROVED, only_from={S.AWAITING_APPROVAL})
    assert len(moved) == 2
    assert group_state(ctx.conn, gid) == S.APPROVED
    transition(ctx.conn, moved[0], S.UPLOADED)
    assert group_state(ctx.conn, gid) == S.APPROVED  # least-advanced part


def test_concurrent_change_detected(ctx):
    vid = records.group_videos(ctx.conn, _group(ctx))[0]["id"]
    ctx.conn.execute("UPDATE videos SET state='scripted' WHERE id=?", (vid,))
    # The stale read sees 'scripted' now, so candidate→scripted is invalid.
    with pytest.raises(InvalidTransition):
        transition(ctx.conn, vid, S.SCRIPTED)
