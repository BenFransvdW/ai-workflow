"""Fetch stage: Reddit (official API via PRAW) → candidate stories in SQLite."""

from __future__ import annotations

import logging

from ..context import Ctx
from ..db import now_iso
from .filters import Post, check

log = logging.getLogger("shorts.fetch")


def reddit_client(ctx: Ctx):
    import praw

    s = ctx.secrets
    return praw.Reddit(client_id=s.require("REDDIT_CLIENT_ID"), client_secret=s.require("REDDIT_CLIENT_SECRET"),
                       user_agent=s.require("REDDIT_USER_AGENT"), check_for_async=False)


def to_post(sub) -> Post:
    """Convert a PRAW Submission (or any object with the same attributes)."""
    author = getattr(sub, "author", None)
    return Post(
        id=sub.id, subreddit=str(sub.subreddit), author=getattr(author, "name", None) if author else None,
        title=sub.title, body=sub.selftext or "", url=f"https://www.reddit.com{sub.permalink}", score=int(sub.score),
        over_18=bool(sub.over_18), removed_by_category=getattr(sub, "removed_by_category", None),
        created_utc=float(sub.created_utc), is_self=bool(getattr(sub, "is_self", True)),
        stickied=bool(getattr(sub, "stickied", False)), distinguished=getattr(sub, "distinguished", None),
    )


def _listing(subreddit, cfg):
    if cfg.listing == "top":
        return subreddit.top(time_filter=cfg.time_filter, limit=cfg.limit_per_subreddit)
    if cfg.listing == "hot":
        return subreddit.hot(limit=cfg.limit_per_subreddit)
    return subreddit.new(limit=cfg.limit_per_subreddit)


def store(ctx: Ctx, post: Post) -> bool | None:
    """Insert a post. Returns True if new candidate, False if new but filtered, None if already known."""
    if ctx.conn.execute("SELECT 1 FROM stories WHERE reddit_id = ?", (post.id,)).fetchone():
        return None
    ok, reason = check(post, ctx.config.reddit)
    ctx.conn.execute(
        "INSERT INTO stories(reddit_id, subreddit, author, title, body, url, score, word_count, created_utc,"
        " fetched_at, status, filter_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (post.id, post.subreddit, post.author or "[deleted]", post.title, post.body, post.url, post.score,
         post.word_count, post.created_utc, now_iso(), "candidate" if ok else "filtered", reason),
    )
    return ok


def fetch(ctx: Ctx, reddit=None) -> int:
    reddit = reddit or reddit_client(ctx)
    cfg = ctx.config.reddit
    new = 0
    for name in cfg.subreddits:
        seen = filtered = 0
        try:
            for sub in _listing(reddit.subreddit(name), cfg):
                res = store(ctx, to_post(sub))
                seen += 1
                new += res is True
                filtered += res is False
        except Exception as e:  # one bad subreddit (private, banned, network) shouldn't stop the rest
            log.warning("fetch r/%s failed: %s", name, e)
            continue
        log.info("r/%s: %d seen, %d filtered", name, seen, filtered)
    log.info("fetch: %d new candidates", new)
    return new


def candidate_count(ctx: Ctx) -> int:
    return ctx.conn.execute("SELECT COUNT(*) FROM stories WHERE status = 'candidate'").fetchone()[0]
