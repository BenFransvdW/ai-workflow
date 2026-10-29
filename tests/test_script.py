import copy
import json
from types import SimpleNamespace

import pytest

from shorts import records
from shorts.metadata import final_metadata
from shorts.stages.script import (
    ScriptInvalid, ScriptRejected, part_texts, script_group, shared_ngrams, too_similar, trigram_similarity,
)
from tests.conftest import FIXTURES, insert_story

FIX = json.loads((FIXTURES / "story.json").read_text())


def good_script(**overrides) -> dict:
    s = copy.deepcopy(FIX["script"])
    s["parts"][0]["cliffhanger"] = ""
    s.update(safe=True, reject_reason="", **overrides)
    return s


class FakeClient:
    """Mimics anthropic.Anthropic for structured-output calls (both .messages and .beta.messages)."""

    def __init__(self, *responses, stop_reason="end_turn"):
        self.responses = list(responses)
        self.calls = []
        self.stop_reason = stop_reason
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        payload = self.responses.pop(0)
        return SimpleNamespace(stop_reason=self.stop_reason, stop_details=SimpleNamespace(category="cyber"),
                               content=[SimpleNamespace(type="text", text=json.dumps(payload))])


def _group(ctx, series="verdict_hour"):
    p = FIX["post"]
    sid = insert_story(ctx.conn, reddit_id=p["id"], subreddit=p["subreddit"], author=p["author"], title=p["title"],
                       body=p["selftext"], url="https://www.reddit.com" + p["permalink"])
    return records.create_group(ctx.conn, sid, series, voice="ryan_piper", caption_style="bold_pop", music_bed="tense")


def test_helpers():
    assert shared_ngrams("one two three four five", "x one two three four y", 4) == ["one two three four"]
    assert shared_ngrams("one two three", "four five six", 2) == []
    assert trigram_similarity("What would you have done?", "What would you have done?") == 1.0
    assert trigram_similarity("Verdict: not the jerk.", "The landlord kept the deposit.") < 0.3
    assert too_similar("What would you do here, honestly?", ["What would you do here?"], 0.35)


def test_script_group_single_part(ctx):
    gid = _group(ctx)
    client = FakeClient(good_script())
    [vid] = script_group(ctx, gid, client)
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert "quiet_lighthouse_42" not in call["messages"][0]["content"]  # author handle not sent to the model
    v = ctx.conn.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
    assert v["state"] == "scripted"
    script = json.loads(v["script_json"])
    assert script["text"].startswith(FIX["script"]["hook"]) and script["text"].endswith(FIX["script"]["commentary"])
    meta = final_metadata(ctx.conn, vid)
    assert "Story submitted by u/quiet_lighthouse_42 on r/AmItheAsshole" in meta["description"]
    assert "adapted" in meta["description"] and "AI-generated voice" in meta["description"]
    assert "#shorts" in meta["description"]
    assert "reddit stories" in meta["tags"]
    assert ctx.conn.execute("SELECT COUNT(*) FROM recent_lines").fetchone()[0] == 3


def test_multi_part_creates_rows_with_cliffhanger(ctx):
    gid = _group(ctx)
    s = good_script()
    s["parts"] = [{"narration": "First half of a rewritten story with tension.", "cliffhanger": "Then the door opened."},
                  {"narration": "Second half where it resolves.", "cliffhanger": ""}]
    ids = script_group(ctx, gid, FakeClient(s))
    assert len(ids) == 2
    rows = records.group_videos(ctx.conn, gid)
    assert [r["part_count"] for r in rows] == [2, 2]
    assert rows[0]["title"].endswith("(Part 1/2)") and rows[1]["title"].endswith("(Part 2/2)")
    t1, t2 = (json.loads(r["script_json"])["text"] for r in rows)
    assert t1.endswith("Then the door opened.") and FIX["script"]["commentary"] not in t1
    assert t2.endswith(FIX["script"]["commentary"]) and FIX["script"]["hook"] not in t2


def test_verbatim_copy_triggers_rewrite(ctx):
    gid = _group(ctx)
    copied = good_script()
    copied["parts"][0]["narration"] += " I politely showed him my boarding pass and he said he was a photographer."
    client = FakeClient(copied, good_script())
    script_group(ctx, gid, client)
    assert len(client.calls) == 2
    feedback = client.calls[1]["messages"][-1]["content"]
    assert "copied wording" in feedback
    assert client.calls[1]["messages"][-2]["role"] == "assistant"


def test_repeated_commentary_triggers_rewrite(ctx):
    ctx.conn.execute("INSERT INTO recent_lines(kind, text, at) VALUES ('commentary', ?, 'x')",
                     (FIX["script"]["commentary"],))
    gid = _group(ctx)
    fresh = good_script(commentary="Honestly? Paying for a seat buys you the shade too. Fair is fair.")
    client = FakeClient(good_script(), fresh)
    script_group(ctx, gid, client)
    assert "commentary resembles" in client.calls[1]["messages"][-1]["content"]


def test_gives_up_after_retries(ctx):
    gid = _group(ctx)
    bad = good_script(hook="")
    with pytest.raises(ScriptInvalid):
        script_group(ctx, gid, FakeClient(bad, bad, bad))
    assert records.group_videos(ctx.conn, gid)[0]["state"] == "candidate"  # caller marks failure


def test_safety_rejection_marks_story(ctx):
    gid = _group(ctx)
    with pytest.raises(ScriptRejected):
        script_group(ctx, gid, FakeClient({"safe": False, "reject_reason": "names a real person", "hook": "",
                                           "parts": [], "commentary": "", "title": "", "description": "",
                                           "tags": []}))
    assert records.group_videos(ctx.conn, gid)[0]["state"] == "rejected"
    assert ctx.conn.execute("SELECT status FROM stories").fetchone()[0] == "rejected"


def test_model_refusal_is_rejection(ctx):
    gid = _group(ctx)
    with pytest.raises(ScriptRejected, match="declined"):
        script_group(ctx, gid, FakeClient(good_script(), stop_reason="refusal"))


def test_word_budget_enforced(ctx):
    gid = _group(ctx)
    long = good_script()
    long["parts"][0]["narration"] = " ".join(["pad"] * 400)
    client = FakeClient(long, good_script())
    script_group(ctx, gid, client)
    assert "budget" in client.calls[1]["messages"][-1]["content"]


def test_part_texts_order():
    s = {"hook": "H.", "commentary": "C.", "parts": [{"narration": "A.", "cliffhanger": "X?"},
                                                     {"narration": "B.", "cliffhanger": ""}]}
    assert part_texts(s) == ["H. A. X?", "B. C."]
