"""Content filters applied before a post can become a candidate.

These are deliberately conservative keyword/regex checks. Detecting *real people* reliably needs
language understanding, so the script stage runs a second, LLM-based safety review as well.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import RedditConfig


@dataclass
class Post:
    id: str
    subreddit: str
    author: str | None
    title: str
    body: str
    url: str
    score: int = 0
    over_18: bool = False
    removed_by_category: str | None = None
    created_utc: float | None = None
    is_self: bool = True
    stickied: bool = False
    distinguished: str | None = None

    @property
    def word_count(self) -> int:
        return len(self.body.split())


# Ages under 18 written the Reddit way: "15F", "(M14)", "14 year old", "14yo", "age 12".
_AGE_PATTERNS = [
    re.compile(r"\b(\d{1,2})\s?[MFmf]\b"),
    re.compile(r"\b[MFmf]\s?(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})\s?(?:yo|y/o|yrs? old|years? old|year-old)\b", re.I),
    re.compile(r"\b(?:aged?|when I was)\s(\d{1,2})\b", re.I),
]
_MINOR_WORDS = re.compile(
    r"\b(child|children|kid|kids|minor|minors|teen|teens|teenager|teenagers|toddler|baby|infant|preteen|"
    r"underage|middle school|elementary school|high school(?:er)?|stepdaughter|stepson|niece|nephew|"
    r"my daughter|my son|little girl|little boy)\b", re.I)
_SEXUAL = re.compile(
    r"\b(sex|sexual(?:ly)?|nude|nudes|naked|porn\w*|molest\w*|rape\w*|raping|grope\w*|groom\w*|explicit|"
    r"intercourse|fondl\w*|predator|pedo\w*|inappropriate(?:ly)? touch\w*|touch\w* (?:\w+ )?inappropriately|"
    r"onlyfans|hook(?:ed)? up)\b", re.I)
_ABUSE = re.compile(
    r"\b(abus\w*|beat(?:en|ing|s)? (?:me|him|her|them)|hit (?:me|him|her|them)|slapp?\w* (?:me|him|her)|"
    r"assault\w*|neglect\w*|starv\w*|lock(?:ed)? (?:me|him|her) in|cps|child protective|self[- ]harm|"
    r"suicid\w*|kill (?:my|him|her)self|trafficking|kidnap\w*)\b", re.I)

_PII = {
    "email address": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "phone number": re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
    "url": re.compile(r"(?:https?://|www\.)\S+", re.I),
    "social handle": re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,}"),
    "reddit username": re.compile(r"(?<![\w/])/?u/[A-Za-z0-9_-]{3,}"),
    "street address": re.compile(
        r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|"
        r"Way|Place|Pl)\b"),
    "licence plate / id number": re.compile(r"\b(?:license plate|licence plate|ssn|social security number)\b", re.I),
}

_UPDATE_TITLE = re.compile(r"\b(update|part \d|pt\.? ?\d)\b", re.I)


def _ages(text: str) -> list[int]:
    out = []
    for pat in _AGE_PATTERNS:
        out += [int(m) for m in pat.findall(text)]
    return out


def mentions_minor(text: str) -> bool:
    return bool(_MINOR_WORDS.search(text)) or any(a < 18 for a in _ages(text) if a > 0)


def check(post: Post, cfg: RedditConfig) -> tuple[bool, str | None]:
    """Return (ok, reason-if-rejected)."""
    body = post.body or ""
    text = f"{post.title}\n{body}"
    if post.over_18:
        return False, "nsfw"
    if post.removed_by_category or body.strip() in ("[removed]", "[deleted]") or not body.strip():
        return False, "removed or deleted"
    if not post.author or post.author in ("[deleted]", "AutoModerator"):
        return False, "deleted author"
    if not post.is_self:
        return False, "not a text post"
    if post.stickied or post.distinguished:
        return False, "mod/stickied post"
    if _UPDATE_TITLE.search(post.title):
        return False, "update or multi-part post (needs context)"
    if not cfg.min_words <= post.word_count <= cfg.max_words:
        return False, f"length {post.word_count} words outside {cfg.min_words}-{cfg.max_words}"
    if post.score < cfg.min_score:
        return False, f"score {post.score} below {cfg.min_score}"
    if mentions_minor(text) and (_SEXUAL.search(text) or _ABUSE.search(text)):
        return False, "minor in sexual or abusive context"
    for name, pat in _PII.items():
        if pat.search(body):
            return False, f"identifying detail ({name})"
    for word in cfg.blocklist:
        if re.search(rf"\b{re.escape(word)}\b", text, re.I):
            return False, f"blocklisted term {word!r}"
    return True, None
