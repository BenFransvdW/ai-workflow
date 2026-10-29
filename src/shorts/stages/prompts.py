"""Prompt text and output schema for the script (LLM rewrite) stage."""

from __future__ import annotations

SYSTEM = """You adapt Reddit stories into narration scripts for vertical short-form videos (YouTube Shorts).
Each video must be a genuinely transformed, original piece of writing, not the post read aloud.

Safety review first. Set "safe" to false (and explain in "reject_reason") if the story:
- involves minors in any sexual context, or describes abuse of a minor;
- names or clearly identifies a real person (celebrity, public figure, or a private person by full name,
  workplace, school, address or other identifying detail) in a way you cannot remove without losing the story;
- is sexually explicit, glorifies self-harm, contains graphic violence, or targets a protected group;
- is obviously an advertisement, a request for advice with no story, or makes no sense without other posts.
When "safe" is false, the other fields may be empty strings/lists.

Writing rules when safe:
- Paraphrase and tighten. Never copy a sentence or any run of more than five consecutive words from the post.
  Restructure the telling; keep only what matters for the story.
- Replace every personal name with a role ("her coworker", "the landlord"). Remove cities, employers, schools,
  usernames and any other identifying details. Keep ages vague.
- Third person by default ("she", "he", "they") unless the series tone calls for something else.
- "hook": one original line, at most 12 words, that makes a viewer stay in the first two seconds.
  It must not be a question you answer immediately, and must not start with "Imagine" or "POV".
- "parts": the story itself (do not repeat the hook or the commentary inside it). Use one part if the whole
  narration fits the word budget, otherwise split at a genuinely suspenseful moment. Every part except the last
  ends with a "cliffhanger": an original one-line teaser for the next part (empty string for the last part).
- "commentary": your own short take at the very end (1-2 sentences) following the series' ending style.
  It must add something new, an opinion, a lesson or a pointed question, and must not reuse phrasing from the
  recent lines listed by the user.
- Write for the ear: short sentences, contractions, spell out numbers and symbols ("five hundred dollars").
  No emojis, hashtags or stage directions in narration.
- Mark at most one key word per sentence for on-screen emphasis by wrapping it in *asterisks*.
- "title": at most 70 characters, honest, intriguing, no clickbait lies, may contain one emoji.
- "description": one or two sentences teasing the story. Do not credit the author (that is added automatically).
- "tags": 5 to 10 lowercase topical tags without '#'."""

SCHEMA = {
    "type": "object",
    "properties": {
        "safe": {"type": "boolean"},
        "reject_reason": {"type": "string"},
        "hook": {"type": "string"},
        "parts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"narration": {"type": "string"}, "cliffhanger": {"type": "string"}},
                "required": ["narration", "cliffhanger"],
                "additionalProperties": False,
            },
        },
        "commentary": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["safe", "reject_reason", "hook", "parts", "commentary", "title", "description", "tags"],
    "additionalProperties": False,
}


def user_prompt(*, subreddit: str, title: str, body: str, series_name: str, tone: str, ending: str,
                words_per_part: int, max_parts: int, recent_hooks: list[str], recent_commentary: list[str]) -> str:
    recent = "\n".join(f"- {line}" for line in (recent_hooks + recent_commentary)[-20:]) or "- (none yet)"
    return f"""Series: {series_name}
Narrator tone: {tone or "natural, engaging storyteller"}
Ending style: {ending}
Word budget: at most {words_per_part} words per part including hook (part 1) or commentary (last part), \
at most {max_parts} parts. Prefer one part when the story fits.

Recent hooks and closing lines on this channel. Do not resemble these in wording or structure:
{recent}

Source post from r/{subreddit}:
<title>{title}</title>
<post>
{body}
</post>"""


def retry_feedback(problems: list[str]) -> str:
    return ("Your draft has these problems:\n" + "\n".join(f"- {p}" for p in problems)
            + "\nRewrite the whole script fixing them, following every rule.")
