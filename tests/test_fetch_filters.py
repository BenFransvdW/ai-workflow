from types import SimpleNamespace

import pytest

from shorts.config import RedditConfig
from shorts.stages.fetch import fetch, to_post
from shorts.stages.filters import Post, check, mentions_minor

CFG = RedditConfig(subreddits=["AITAH"], min_words=20, max_words=400, min_score=10, blocklist=["Voldemort"])
BODY = ("I lent my neighbour my lawnmower last spring and every week since then he has found a new reason to keep it. "
        "First the blade was dull, then it rained, then his cousin needed it. Yesterday I saw it at a yard sale.")


def post(**kw) -> Post:
    base = dict(id="a1", subreddit="AITAH", author="writer", title="AITA for taking my mower back?", body=BODY,
                url="https://www.reddit.com/r/AITAH/comments/a1/", score=50)
    base.update(kw)
    return Post(**base)


def test_clean_post_passes():
    assert check(post(), CFG) == (True, None)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"over_18": True}, "nsfw"),
        ({"removed_by_category": "moderator"}, "removed"),
        ({"body": "[removed]"}, "removed"),
        ({"body": "[deleted]"}, "removed"),
        ({"author": None}, "deleted author"),
        ({"author": "[deleted]"}, "deleted author"),
        ({"is_self": False}, "not a text post"),
        ({"stickied": True}, "mod"),
        ({"title": "UPDATE: the mower saga"}, "update"),
        ({"body": "too short"}, "length"),
        ({"body": "word " * 500}, "length"),
        ({"score": 1}, "score"),
        ({"body": BODY + " Email me at someone@example.com"}, "email"),
        ({"body": BODY + " Call 555-123-4567 now"}, "phone"),
        ({"body": BODY + " Pics at https://imgur.com/abc"}, "url"),
        ({"body": BODY + " Follow @mowerguy99 for more"}, "social handle"),
        ({"body": BODY + " It was u/mower_thief all along"}, "reddit username"),
        ({"body": BODY + " He lives at 42 Maple Grove Street"}, "street address"),
        ({"body": BODY + " Voldemort did it"}, "blocklisted"),
        ({"body": BODY + " My stepdaughter (14F) told me he touched her inappropriately"}, "minor"),
        ({"body": BODY + " When I was 12 my dad would beat me for less"}, "minor"),
    ],
)
def test_filters_reject(overrides, reason):
    ok, why = check(post(**overrides), CFG)
    assert not ok and reason in why


def test_minor_without_abuse_is_allowed():
    ok, _ = check(post(body=BODY + " My son (8M) helped me carry it home, he thought it was hilarious."), CFG)
    assert ok
    assert mentions_minor("my son (8M)")
    assert not mentions_minor("I (29F) and my husband (31M)")


class FakeSubreddit:
    def __init__(self, name, subs):
        self.name, self.subs, self.calls = name, subs, []

    def top(self, time_filter, limit):
        self.calls.append(("top", time_filter, limit))
        return iter(self.subs)


class FakeReddit:
    def __init__(self, subs):
        self.subs = subs

    def subreddit(self, name):
        if name == "private":
            raise RuntimeError("403 private")
        return FakeSubreddit(name, self.subs)


def submission(i, **kw):
    base = dict(id=f"p{i}", subreddit="AITAH", author=SimpleNamespace(name=f"user{i}"), title=f"AITA number {i}?",
                selftext=BODY, permalink=f"/r/AITAH/comments/p{i}/x/", score=500, over_18=False,
                removed_by_category=None, created_utc=1.7e9, is_self=True, stickied=False, distinguished=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_fetch_stores_candidates_and_dedupes(ctx):
    ctx.config.reddit = CFG.model_copy(update={"subreddits": ["AITAH", "private"]})
    subs = [submission(1), submission(2, over_18=True), submission(3, author=None)]
    assert fetch(ctx, FakeReddit(subs)) == 1
    rows = {r["reddit_id"]: r for r in ctx.conn.execute("SELECT * FROM stories")}
    assert rows["p1"]["status"] == "candidate" and rows["p1"]["author"] == "user1"
    assert rows["p1"]["url"] == "https://www.reddit.com/r/AITAH/comments/p1/x/"
    assert rows["p2"]["status"] == "filtered" and rows["p2"]["filter_reason"] == "nsfw"
    assert rows["p3"]["filter_reason"] == "deleted author"
    # Second fetch of the same posts adds nothing (dedupe on reddit_id).
    assert fetch(ctx, FakeReddit(subs)) == 0
    assert ctx.conn.execute("SELECT COUNT(*) FROM stories").fetchone()[0] == 3


def test_to_post_handles_missing_author():
    p = to_post(submission(9, author=None))
    assert p.author is None and p.id == "p9"
