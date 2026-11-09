"""Parse unified diffs into files, hunks and valid new-file line numbers.

Extracted from L6 ``src/diff_tools/parser.py`` and hardened for:
- quoted paths (spaces) emitted by git
- fine-grained patches that only contain ``---`` / ``+++`` / ``@@``
- binary files
"""
from __future__ import annotations

import re

from codereview.models import FileDiff, Hunk

_DIFF_HEADER = re.compile(r'^diff --git (?P<a>"a/.+?"|a/\S+) (?P<b>"b/.+?"|b/\S+)$')
_HUNK_HEADER = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$')
_OLD_HEADER = re.compile(r'^--- (?:"a/|\w+/)?(.+?)(?:\t.*)?$')
_NEW_HEADER = re.compile(r'^\+\+\+ (?:"b/|\w+/)?(.+?)(?:\t.*)?$')


def _clean_path(raw: str) -> str:
    value = raw.strip()
    if value.startswith('"') and value.endswith('"'):
        value = value[1:-1]
    for prefix in ("a/", "b/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    return value


def parse_unified_diff(diff: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    current_hunk: Hunk | None = None
    pending_old: str | None = None

    for raw_line in diff.split("\n"):
        line = raw_line.rstrip("\r")

        match = _DIFF_HEADER.match(line)
        if match:
            current = FileDiff(
                path=_clean_path(match.group("b")),
                old_path=_clean_path(match.group("a")),
            )
            files.append(current)
            current_hunk = None
            pending_old = None
            continue

        if line.startswith("--- "):
            old_match = _OLD_HEADER.match(line)
            pending_old = _clean_path(old_match.group(1)) if old_match else None
            continue

        if line.startswith("+++ "):
            new_match = _NEW_HEADER.match(line)
            new_path = _clean_path(new_match.group(1)) if new_match else ""
            if new_path and new_path != "/dev/null" and current is None:
                current = FileDiff(path=new_path, old_path=pending_old)
                files.append(current)
                current_hunk = None
            pending_old = None
            continue

        if current is None:
            continue

        if line.startswith("new file mode"):
            current.is_new = True
            continue
        if line.startswith("deleted file mode"):
            current.is_deleted = True
            continue
        if line.startswith("rename from "):
            current.old_path = line[len("rename from "):]
            continue
        if line.startswith("rename to "):
            current.path = line[len("rename to "):]
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current_hunk = None
            continue

        hunk_match = _HUNK_HEADER.match(line)
        if hunk_match:
            current_hunk = Hunk(
                old_start=int(hunk_match.group(1)),
                old_count=int(hunk_match.group(2) or 1),
                new_start=int(hunk_match.group(3)),
                new_count=int(hunk_match.group(4) or 1),
                header=line,
            )
            current.hunks.append(current_hunk)
            continue

        if current_hunk is not None and line[:1] in (" ", "+", "-", "\\"):
            current_hunk.lines.append(line)

    return files


def normalize_diff(diff: str, default_path: str = "change.py") -> str:
    """Wrap a bare hunk in a valid diff header. Idempotent."""
    if not diff.strip() or parse_unified_diff(diff):
        return diff
    return (
        f"diff --git a/{default_path} b/{default_path}\n"
        f"--- a/{default_path}\n"
        f"+++ b/{default_path}\n"
    ) + diff


def changed_new_lines(file_diff: FileDiff) -> list[tuple[int, str]]:
    """Return (new-file line number, content without '+') for added lines."""
    changed: list[tuple[int, str]] = []
    for hunk in file_diff.hunks:
        new_line = hunk.new_start
        for line in hunk.lines:
            if line.startswith("+"):
                changed.append((new_line, line[1:]))
                new_line += 1
            elif line.startswith(" "):
                new_line += 1
    return changed


def valid_line_map(files: list[FileDiff]) -> dict[str, set[int]]:
    return {
        f.path: {line for line, _ in changed_new_lines(f)}
        for f in files
        if not f.is_deleted
    }


def changed_paths(files: list[FileDiff]) -> list[str]:
    return [f.path for f in files if not f.is_deleted]
