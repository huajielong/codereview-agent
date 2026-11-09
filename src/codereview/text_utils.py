"""Text helpers shared by prompts, policy, history and ensemble merging."""
from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[㐀-鿿]")
_STOP = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
    "is", "are", "be", "this", "that", "it", "as", "at", "by", "from",
    "not", "no", "we", "you", "use", "can", "may", "should", "must",
}


def escape_braces(text: object) -> str:
    return str(text).replace("{", "{{").replace("}", "}}")


def truncate(text: str, limit: int, suffix: str = "...") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))] + suffix


def token_set(text: str) -> set[str]:
    """Latin words (len>=3, no stopwords) + CJK unigrams."""
    tokens: set[str] = set()
    lowered = str(text or "").lower().replace("_", " ").replace("-", " ")
    for word in _WORD.findall(lowered):
        if len(word) >= 3 and word not in _STOP:
            tokens.add(word)
    tokens.update(_CJK.findall(lowered))
    return tokens


def overlap_ratio(left: str, right: str) -> float:
    left_tokens = token_set(left)
    right_tokens = token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / max(len(left_tokens), len(right_tokens))
