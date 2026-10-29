"""Script stage: an LLM rewrites the post into an original narration (hook + story + own take).

Guards against "inauthentic content":
- verbatim check: no run of `verbatim_ngram` words shared with the source post;
- variety check: hook/commentary must not resemble recent ones (character-trigram Jaccard);
- an LLM safety review that can reject the story outright.
Failed checks are fed back to the model for a rewrite (up to `llm.max_retries` times).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..context import Ctx
from ..db import now_iso, update
from ..records import create_video, group_story, group_videos
from ..state import State, transition
from . import prompts

log = logging.getLogger("shorts.script")
_WORD = re.compile(r"[a-z0-9']+")


class ScriptRejected(RuntimeError):
    """The story failed the LLM safety review (or was refused); it must not be used."""


class ScriptInvalid(RuntimeError):
    """The model could not produce a script that passes validation within the retry budget."""


# ---------------------------------------------------------------- checks

def words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("’", "'").replace("*", ""))


def shared_ngrams(a: str, b: str, n: int) -> list[str]:
    """Runs of n words that appear in both texts (verbatim copying detector)."""
    wb = words(b)
    grams = {tuple(wb[i:i + n]) for i in range(len(wb) - n + 1)}
    wa = words(a)
    return [" ".join(wa[i:i + n]) for i in range(len(wa) - n + 1) if tuple(wa[i:i + n]) in grams]


def trigram_similarity(a: str, b: str) -> float:
    def grams(s: str) -> set[str]:
        s = " ".join(words(s))
        return {s[i:i + 3] for i in range(len(s) - 2)}

    ga, gb = grams(a), grams(b)
    return len(ga & gb) / len(ga | gb) if ga and gb else 0.0


def too_similar(line: str, recent: list[str], threshold: float) -> str | None:
    first = " ".join(words(line)[:4])
    for r in recent:
        if trigram_similarity(line, r) >= threshold or (len(first.split()) == 4 and first == " ".join(words(r)[:4])):
            return r
    return None


def part_texts(script: dict) -> list[str]:
    parts = script["parts"]
    out = []
    for i, p in enumerate(parts):
        bits = []
        if i == 0:
            bits.append(script["hook"].strip())
        bits.append(p["narration"].strip())
        if i < len(parts) - 1:
            bits.append(p["cliffhanger"].strip())
        else:
            bits.append(script["commentary"].strip())
        out.append(" ".join(b for b in bits if b))
    return out


def validate(ctx: Ctx, script: dict, source: str, words_per_part: int, recent_hooks: list[str],
             recent_commentary: list[str]) -> list[str]:
    llm = ctx.config.llm
    problems = []
    parts = script.get("parts") or []
    if not parts:
        problems.append("there are no parts")
    if len(parts) > llm.max_parts:
        problems.append(f"{len(parts)} parts; the maximum is {llm.max_parts}")
    for i, p in enumerate(parts[:-1]):
        if not p.get("cliffhanger", "").strip():
            problems.append(f"part {i + 1} needs a cliffhanger line")
    for i, text in enumerate(part_texts(script) if parts else []):
        n = len(words(text))
        if n > words_per_part * 1.1:
            problems.append(f"part {i + 1} has {n} words; the budget is {words_per_part}")
    if len(words(script.get("hook", ""))) > 14:
        problems.append("the hook is longer than 12 words")
    if not script.get("hook", "").strip() or not script.get("commentary", "").strip():
        problems.append("hook and commentary are required")
    if len(script.get("title", "")) > 90:
        problems.append("the title is longer than 70 characters")
    full = " ".join([script.get("hook", ""), *(p.get("narration", "") for p in parts), script.get("commentary", "")])
    copied = shared_ngrams(full, source, llm.verbatim_ngram)
    if copied:
        problems.append(f"copied wording from the post, e.g. \"{copied[0]}\"; paraphrase it")
    if (r := too_similar(script.get("hook", ""), recent_hooks, llm.similarity_threshold)):
        problems.append(f"the hook resembles a recent one (\"{r}\"); write a different kind of hook")
    if (r := too_similar(script.get("commentary", ""), recent_commentary, llm.similarity_threshold)):
        problems.append(f"the commentary resembles a recent one (\"{r}\"); take a different angle")
    return problems


# ---------------------------------------------------------------- LLM call

def anthropic_client(ctx: Ctx):
    import anthropic

    return anthropic.Anthropic(api_key=ctx.secrets.require("ANTHROPIC_API_KEY"), max_retries=3)


def call_model(ctx: Ctx, client: Any, messages: list[dict]) -> tuple[dict, list]:
    """One structured-output request. Returns (parsed JSON, assistant content to append on retry)."""
    llm = ctx.config.llm
    kwargs = dict(
        model=llm.model, max_tokens=llm.max_tokens, system=prompts.SYSTEM, messages=messages,
        output_config={"effort": llm.effort, "format": {"type": "json_schema", "schema": prompts.SCHEMA}},
    )
    if llm.server_fallbacks:
        resp = client.beta.messages.create(**kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    else:
        resp = client.messages.create(**kwargs)
    if resp.stop_reason == "refusal":
        details = getattr(resp, "stop_details", None)
        raise ScriptRejected(f"model declined: {getattr(details, 'category', None)}")
    if resp.stop_reason == "max_tokens":
        raise ScriptInvalid("response hit max_tokens")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text), resp.content


# ---------------------------------------------------------------- stage

def recent_lines(ctx: Ctx, kind: str) -> list[str]:
    n = ctx.config.llm.history_size
    return [r["text"] for r in ctx.conn.execute(
        "SELECT text FROM recent_lines WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, n))][::-1]


def words_budget(ctx: Ctx, pacing: float, shrink: float = 1.0) -> int:
    llm = ctx.config.llm
    return int(llm.words_per_minute * pacing * llm.max_part_seconds / 60 * shrink)


def generate(ctx: Ctx, story, series, client: Any, shrink: float = 1.0) -> dict:
    budget = words_budget(ctx, series.pacing, shrink)
    hooks, comms = recent_lines(ctx, "hook"), recent_lines(ctx, "commentary")
    messages: list[dict] = [{"role": "user", "content": prompts.user_prompt(
        subreddit=story["subreddit"], title=story["title"], body=story["body"], series_name=series.display_name,
        tone=series.tone, ending=series.ending, words_per_part=budget, max_parts=ctx.config.llm.max_parts,
        recent_hooks=hooks, recent_commentary=comms)}]
    for attempt in range(ctx.config.llm.max_retries + 1):
        script, content = call_model(ctx, client, messages)
        if not script.get("safe", False):
            raise ScriptRejected(script.get("reject_reason") or "failed safety review")
        problems = validate(ctx, script, f"{story['title']} {story['body']}", budget, hooks, comms)
        if not problems:
            return script
        log.info("script attempt %d rejected: %s", attempt + 1, "; ".join(problems))
        messages += [{"role": "assistant", "content": content},
                     {"role": "user", "content": prompts.retry_feedback(problems)}]
    raise ScriptInvalid("; ".join(problems))


def credit_block(story) -> str:
    return (f"Story submitted by u/{story['author']} on r/{story['subreddit']}, adapted and rewritten for narration. "
            f"Original post: {story['url']}\nNarrated with an AI-generated voice.")


def part_title(series, title: str, i: int, n: int) -> str:
    base = f"{series.title_prefix}{title}".strip()
    suffix = f" (Part {i}/{n})" if n > 1 else ""
    return (base[: 100 - len(suffix)].rstrip() + suffix)[:100]


def script_group(ctx: Ctx, group_id: int, client: Any = None, shrink: float = 1.0) -> list[int]:
    """Write the script for a group, creating one video row per part. Returns the video ids."""
    group = ctx.conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
    story = group_story(ctx.conn, group_id)
    series = ctx.config.series_by_name(group["series"])
    videos = group_videos(ctx.conn, group_id)
    try:
        script = generate(ctx, story, series, client or anthropic_client(ctx), shrink)
    except ScriptRejected as e:
        for v in videos:
            transition(ctx.conn, v["id"], State.REJECTED, f"llm safety: {e}")
        ctx.conn.execute("UPDATE stories SET status = 'rejected', filter_reason = ? WHERE id = ?",
                         (f"llm safety: {e}", story["id"]))
        raise
    texts = part_texts(script)
    n = len(texts)
    ids = [v["id"] for v in videos]
    while len(ids) < n:
        ids.append(create_video(ctx.conn, group_id, len(ids) + 1, n))
    tags = list(dict.fromkeys([*(t.strip().lower().lstrip("#") for t in script["tags"]), series.name.replace("_", " "),
                               "reddit stories", "shorts"]))
    for i, (vid, text) in enumerate(zip(ids, texts), start=1):
        part = script["parts"][i - 1]
        payload = {"text": text, "hook": script["hook"] if i == 1 else None, "narration": part["narration"],
                   "cliffhanger": part["cliffhanger"] if i < n else None,
                   "commentary": script["commentary"] if i == n else None, "llm_description": script["description"]}
        update(ctx.conn, "videos", vid, part_count=n, script_json=json.dumps(payload),
               title=part_title(series, script["title"], i, n),
               description=f"{script['description'].strip()}\n\n{credit_block(story)}",
               tags_json=json.dumps(tags[:15]))
        transition(ctx.conn, vid, State.SCRIPTED, "script written")
    ts = now_iso()
    for kind, text in (("hook", script["hook"]), ("commentary", script["commentary"]), ("title", script["title"])):
        ctx.conn.execute("INSERT INTO recent_lines(kind, text, at) VALUES (?, ?, ?)", (kind, text, ts))
    log.info("group %s scripted: %d part(s), title %r", group_id, n, script["title"])
    return ids
