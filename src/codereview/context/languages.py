"""Language registry shared by chunking, retrieval and dependency graphs."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Iterable

EXTENSION_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".rs": "rust",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell",
    ".sql": "sql",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
}

SUPPORTED_EXTENSIONS = frozenset(EXTENSION_LANGUAGES)


LANGUAGE_GROUPS = {
    "c": ("c", "cpp"),
    "cpp": ("c", "cpp"),
    "javascript": ("javascript", "typescript"),
    "typescript": ("javascript", "typescript"),
}


def language_for(path: str) -> str:
    """Return the language name for a path, or ``text`` when unknown."""
    return EXTENSION_LANGUAGES.get(Path(path).suffix.lower(), "text")


def expand_languages(languages: Iterable[str]) -> list[str]:
    """Expand languages to retrieval families (C/C++, JS/TS)."""
    expanded: set[str] = set()
    for language in languages:
        expanded.update(LANGUAGE_GROUPS.get(language, (language,)))
    return sorted(expanded)


def retrieval_groups(languages: Iterable[str]) -> list[set[str]]:
    """Distinct retrieval families, e.g. {python}, {c, cpp}, {js, ts}."""
    groups: list[set[str]] = []
    seen: set[frozenset[str]] = set()
    for language in sorted(set(languages)):
        group = frozenset(LANGUAGE_GROUPS.get(language, (language,)))
        if group not in seen:
            seen.add(group)
            groups.append(set(group))
    return groups


def languages_for_paths(paths: Iterable[str]) -> list[str]:
    """Unique known languages in ``paths``, sorted for stable prompts."""
    return sorted(
        {
            language
            for path in paths
            if (language := language_for(path)) != "text"
        }
    )


def render_language_section(paths: Iterable[str]) -> str:
    """Small prompt section listing the languages changed by this PR."""
    counts = Counter(
        language
        for path in paths
        if (language := language_for(path)) != "text"
    )
    if not counts:
        return ""
    parts = []
    for language, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        parts.append(f"{language} ({count} file{'s' if count != 1 else ''})")
    return "CHANGED LANGUAGES: " + ", ".join(parts)
