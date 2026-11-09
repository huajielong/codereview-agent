"""Split large diffs by file/hunk so each LLM call stays inside a budget.

Extracted from L6 ``src/diff_tools/chunker.py``.
"""
from __future__ import annotations

from codereview.models import FileDiff


def render_file(file_diff: FileDiff) -> str:
    old = file_diff.old_path or file_diff.path
    lines = [f"diff --git a/{old} b/{file_diff.path}"]
    if file_diff.is_new:
        lines.append("new file mode 100644")
    if file_diff.is_deleted:
        lines.append("deleted file mode 100644")
    for hunk in file_diff.hunks:
        lines.append(hunk.header)
        lines.extend(hunk.lines)
    return "\n".join(lines) + "\n"


def render_chunk(chunk: list[FileDiff]) -> str:
    return "".join(render_file(file_diff) for file_diff in chunk)


def _units(files: list[FileDiff], max_chars: int):
    for file_diff in files:
        rendered = render_file(file_diff)
        if len(rendered) <= max_chars:
            yield file_diff
            continue
        for hunk in file_diff.hunks:
            yield FileDiff(
                path=file_diff.path,
                old_path=file_diff.old_path,
                is_new=file_diff.is_new,
                is_deleted=file_diff.is_deleted,
                hunks=[hunk],
            )


def chunk_diff(files: list[FileDiff], max_chars: int = 12000) -> list[list[FileDiff]]:
    """Pack files/hunks into chunks. A single hunk is never split."""
    chunks: list[list[FileDiff]] = []
    current: list[FileDiff] = []
    current_chars = 0

    for unit in _units(files, max_chars):
        rendered = render_file(unit)
        if current and current_chars + len(rendered) > max_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(unit)
        current_chars += len(rendered)

    if current:
        chunks.append(current)
    return chunks
