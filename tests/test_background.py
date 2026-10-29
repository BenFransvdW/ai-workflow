import json
import random
from pathlib import Path

import numpy as np
import pytest

from shorts import media, records
from shorts.config import SeriesBackground
from shorts.stages.background import background_video, choose_segment
from shorts.stages.background.base import Segment, free_start, overlaps, record_usage
from shorts.stages.background.local_folder import LocalFolderProvider
from shorts.stages.background.pexels import LICENSE, PexelsProvider, pick_file
from shorts.stages.background.pool import ProceduralProvider, active_clips, fill_pool
from shorts.stages.background.procedural import STYLES, render_clip
from tests.conftest import FIXTURES, insert_story, requires_ffmpeg


# ---------------------------------------------------------------- procedural styles

@pytest.mark.parametrize("style", sorted(STYLES))
def test_style_frames_vary_with_seed(style):
    def frames(seed):
        anim = STYLES[style](90, 160, 10, np.random.default_rng(seed))
        return [anim.to_uint8(anim.frame(i)) for i in range(12)], anim.params

    (fa, pa), (fb, pb) = frames(1), frames(2)
    assert fa[-1].shape == (160, 90, 3) and fa[-1].dtype == np.uint8
    assert not np.array_equal(fa[-1], fb[-1]), "different seeds must look different"
    assert not np.array_equal(fa[0], fa[-1]), "the animation must move"
    assert pa  # randomised params are recorded


@requires_ffmpeg
@pytest.mark.parametrize("style", sorted(STYLES))
def test_render_clip_produces_video(style, tmp_path):
    out = tmp_path / f"{style}.mp4"
    meta = render_clip(style, out, width=90, height=160, fps=10, seconds=2, seed=3, preset="ultrafast")
    assert media.video_size(out) == (90, 160)
    assert abs(media.duration(out) - 2) < 0.2
    assert meta["params"] and meta["seed"] == 3


# ---------------------------------------------------------------- helpers

def test_free_start_avoids_used_intervals():
    rng = random.Random(0)
    for _ in range(50):
        s = free_start(60, 20, [(10, 35)], rng)
        assert s is not None and not overlaps(s, s + 20, 10, 35)
    assert free_start(10, 20, [], rng) is None
    assert free_start(30, 20, [(5, 25)], rng) is None


# ---------------------------------------------------------------- procedural pool

@requires_ffmpeg
def test_pool_rotation_and_retirement(ctx):
    ctx.config.background.max_uses_per_clip = 1
    assert fill_pool(ctx, styles=["plasma", "plinko"], count=2) == 4
    assert fill_pool(ctx, styles=["plasma", "plinko"], count=2) == 0  # already full
    spec = SeriesBackground(provider="procedural", styles=["plasma", "plinko"])
    prov = ProceduralProvider(ctx, random.Random(1))
    seen_styles, seen_clips = [], []
    for _ in range(4):
        seg = prov.get_segment(2.0, spec)
        record_usage(ctx.conn, seg, None)
        clip = ctx.conn.execute("SELECT * FROM bg_clips WHERE id=?", (seg.pieces[0].clip_id,)).fetchone()
        seen_styles.append(clip["style"])
        seen_clips.append(clip["id"])
    assert len(set(seen_clips)) == 4, "each clip used once with max_uses_per_clip=1"
    assert seen_styles[0] != seen_styles[1], "styles alternate"
    assert ProceduralProvider.retire_worn(ctx) == 4
    assert active_clips(ctx, "plasma") == []
    assert not any(Path(r["path"]).exists() for r in ctx.conn.execute("SELECT path FROM bg_clips"))


@requires_ffmpeg
def test_background_stage_renders_on_demand(ctx):
    sid = insert_story(ctx.conn)
    gid = records.create_group(ctx.conn, sid, "strange_encounters")
    vid = records.group_videos(ctx.conn, gid)[0]["id"]
    ctx.conn.execute("UPDATE videos SET duration = 2.0 WHERE id = ?", (vid,))
    # pexels has no API key → falls through to procedural, which renders on demand.
    seg = background_video(ctx, vid, random.Random(5))
    assert seg.provider == "procedural"
    row = ctx.conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    assert json.loads(row["bg_json"])["pieces"][0]["duration"] == pytest.approx(2.6)
    style = ctx.conn.execute("SELECT style FROM bg_clips WHERE id=?", (row["bg_clip_id"],)).fetchone()[0]
    assert style in ("plasma", "flowfield")


# ---------------------------------------------------------------- local folder

def _make_clip(path: Path, seconds: float, size="320x240"):
    media.ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={size}:rate=10:duration={seconds}", "-pix_fmt", "yuv420p", path)


@requires_ffmpeg
def test_local_folder_never_repeats_segment_back_to_back(ctx, tmp_path):
    folder = tmp_path / "clips"
    folder.mkdir()
    _make_clip(folder / "parkour.mp4", 12)
    ctx.config.background.local_folder.path = folder
    prov = LocalFolderProvider(ctx, random.Random(3))
    spec = SeriesBackground(provider="local_folder")
    prev = None
    for _ in range(8):
        seg = prov.get_segment(5, spec)
        p = seg.pieces[0]
        if prev:
            assert not overlaps(p.start, p.start + 5, prev.start, prev.start + 5)
        record_usage(ctx.conn, seg, None)
        prev = p


@requires_ffmpeg
def test_local_folder_prefers_other_clip(ctx, tmp_path):
    folder = tmp_path / "clips"
    folder.mkdir()
    _make_clip(folder / "a.mp4", 6)
    _make_clip(folder / "b.mp4", 6)
    ctx.config.background.local_folder.path = folder
    prov = LocalFolderProvider(ctx, random.Random(0))
    spec = SeriesBackground(provider="local_folder")
    ids = []
    for _ in range(6):
        seg = prov.get_segment(5, spec)
        record_usage(ctx.conn, seg, None)
        ids.append(seg.pieces[0].clip_id)
    assert all(a != b for a, b in zip(ids, ids[1:]))


# ---------------------------------------------------------------- pexels

class FakeResponse:
    def __init__(self, payload=None, content=b""):
        self.payload, self.content = payload, content

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload

    def iter_content(self, n):
        yield self.content

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeSession:
    def __init__(self, search_payload, clip_bytes):
        self.search_payload, self.clip_bytes = search_payload, clip_bytes
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if "api.pexels.com" in url:
            page = kw["params"]["page"]
            return FakeResponse(self.search_payload if page == 1 else {"videos": []})
        return FakeResponse(content=self.clip_bytes)


def test_pick_file_prefers_smallest_tall_enough():
    payload = json.loads((FIXTURES / "pexels_search.json").read_text())
    assert pick_file(payload["videos"][0], 1280)["height"] == 1920
    assert pick_file(payload["videos"][1], 1280) is None  # landscape only
    assert pick_file(payload["videos"][2], 1920)["height"] == 1280  # tallest available


@requires_ffmpeg
def test_pexels_cache_licence_and_reuse_window(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("PEXELS_API_KEY", "test-key")
    clip = tmp_path / "src.mp4"
    _make_clip(clip, 4, size="180x320")
    session = FakeSession(json.loads((FIXTURES / "pexels_search.json").read_text()), clip.read_bytes())
    prov = PexelsProvider(ctx, random.Random(2), session=session)
    spec = SeriesBackground(provider="pexels", themes=["satisfying"])

    seg = prov.get_segment(6.0, spec)  # needs two 4 s clips chained
    assert seg.provider == "pexels" and len(seg.pieces) == 2
    assert seg.duration == pytest.approx(6.0)
    assert session.calls[0][1]["headers"]["Authorization"] == "test-key"
    assert session.calls[0][1]["params"]["orientation"] == "portrait"
    rows = ctx.conn.execute("SELECT * FROM bg_clips WHERE provider='pexels' ORDER BY external_id").fetchall()
    assert [r["external_id"] for r in rows] == ["1001", "1003"]  # landscape-only video skipped
    assert rows[0]["license"] == LICENSE and rows[0]["author"] == "Jane Maker"
    assert rows[0]["source_url"].startswith("https://www.pexels.com/video/")
    assert any("Jane Maker" in c for c in seg.credits)

    record_usage(ctx.conn, seg, None)
    # Both clips now have a used segment covering most of their length → can't reuse within window.
    with pytest.raises(Exception):
        prov.get_segment(6.0, spec)
    # Outside the reuse window they're available again.
    ctx.conn.execute("UPDATE bg_usage SET used_at = '2000-01-01T00:00:00+00:00'")
    assert prov.get_segment(6.0, spec).duration == pytest.approx(6.0)


def test_pexels_without_key_falls_back_to_procedural(ctx, monkeypatch):
    calls = []

    class Stub(ProceduralProvider):
        def get_segment(self, duration, spec):
            calls.append(spec.provider)
            return Segment("procedural", [])

    monkeypatch.setattr("shorts.stages.background.pool.ProceduralProvider", Stub)
    ctx.config.series_by_name("verdict_hour").backgrounds = [SeriesBackground(provider="pexels", themes=["slime"])]
    seg = choose_segment(ctx, "verdict_hour", 5.0, random.Random(0))
    assert seg.provider == "procedural" and calls == ["procedural"]
