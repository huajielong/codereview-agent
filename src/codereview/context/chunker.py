"""Chunk repository files for retrieval.

Python files use AST chunks (functions/classes/methods), matching L6. Other
languages fall back to line windows so the agent still gets cross-file context
without pretending to understand their syntax.
"""
from __future__ import annotations

import ast
import hashlib
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path

EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "env", "__pycache__", "site-packages",
    "node_modules", "build", "dist", ".tox", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", "vendor", ".idea", ".vscode",
}

SUPPORTED_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".rb",
    ".php", ".cs", ".rs", ".swift", ".scala", ".sh", ".sql", ".yml", ".yaml",
}

_LANGUAGE = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".go": "go", ".java": "java",
    ".kt": "kotlin", ".rb": "ruby", ".php": "php", ".cs": "csharp",
    ".rs": "rust", ".swift": "swift", ".scala": "scala", ".sh": "shell",
    ".sql": "sql", ".yml": "yaml", ".yaml": "yaml",
}


@dataclass(frozen=True)
class Chunk:
    content: str
    type: str
    name: str
    file_path: str
    line_start: int
    line_end: int
    language: str = "python"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Chunk":
        return cls(
            content=data.get("content", ""),
            type=data.get("type", "function"),
            name=data.get("name", ""),
            file_path=data.get("file_path", ""),
            line_start=int(data.get("line_start") or 1),
            line_end=int(data.get("line_end") or 1),
            language=data.get("language", "python"),
        )


def blob_sha(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()


def language_for(path: str) -> str:
    return _LANGUAGE.get(Path(path).suffix.lower(), "text")


def _segment(source_lines: list[str], start: int, end: int) -> str:
    return "\n".join(source_lines[max(0, start - 1):end])


class _PythonVisitor(ast.NodeVisitor):
    def __init__(self, source: str, path: str):
        self.source = source
        self.path = path
        self.lines = source.splitlines()
        self.chunks: list[Chunk] = []

    def _add(self, node: ast.AST, chunk_type: str, name: str) -> None:
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start) or start
        self.chunks.append(
            Chunk(
                content=_segment(self.lines, start, end),
                type=chunk_type,
                name=name,
                file_path=self.path,
                line_start=start,
                line_end=end,
                language="python",
            )
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        name = node.name
        self._add(node, "test" if name.startswith("test_") else "function", name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._add(node, "function", node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node, "class", node.name)
        self.generic_visit(node)


def _module_chunk(source: str, path: str, tree: ast.Module | None) -> Chunk | None:
    if not source.strip():
        return None
    lines = source.splitlines()
    excluded: set[int] = set()
    if tree is not None:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = getattr(node, "lineno", None)
                end = getattr(node, "end_lineno", None)
                if start and end:
                    excluded.update(range(start, end + 1))
    kept = [line for number, line in enumerate(lines, 1) if number not in excluded]
    text = "\n".join(kept).strip()
    # Pure definitions need no extra module chunk.
    if len(text) < 20 or all(not line.strip() for line in text.splitlines()):
        return None
    if len(text) > 4000:
        return None
    return Chunk(
        content=text,
        type="module",
        name="module",
        file_path=path,
        line_start=1,
        line_end=len(lines),
        language="python",
    )


def _window_chunks(source: str, path: str, max_chars: int = 8000) -> list[Chunk]:
    language = language_for(path)
    if len(source) <= max_chars:
        return [
            Chunk(
                content=source,
                type="file",
                name=Path(path).stem,
                file_path=path,
                line_start=1,
                line_end=max(1, len(source.splitlines())),
                language=language,
            )
        ]

    lines = source.splitlines()
    chunks: list[Chunk] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines) and (size < max_chars or end == start):
            size += len(lines[end]) + 1
            end += 1
        chunks.append(
            Chunk(
                content="\n".join(lines[start:end]),
                type="file",
                name=f"lines_{start + 1}_{end}",
                file_path=path,
                line_start=start + 1,
                line_end=end,
                language=language,
            )
        )
        if end >= len(lines):
            break
        start = max(start + 1, end - 20)  # 20-line overlap
    return chunks


def chunk_file(source: str, path: str) -> list[Chunk]:
    """Chunk one file. Python -> AST; everything else -> line windows."""
    if Path(path).suffix.lower() != ".py":
        return _window_chunks(source, path)

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return _window_chunks(source, path)

    visitor = _PythonVisitor(source, path)
    visitor.visit(tree)
    chunks = visitor.chunks
    module = _module_chunk(source, path, tree)
    if module:
        chunks.append(module)
    return chunks or _window_chunks(source, path)


def _read_source(path: Path) -> str | None:
    try:
        if path.suffix.lower() == ".py":
            with tokenize.open(path) as fh:
                return fh.read()
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError, SyntaxError, LookupError):
        return None


def iter_repo_files(root: str | Path, max_bytes: int = 200_000):
    """Yield (relative path, source) for supported, non-ignored files."""
    root_path = Path(root)
    for path in sorted(root_path.rglob("*")):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root_path)
        except OSError:
            continue
        if EXCLUDED_DIRS.intersection(relative.parts):
            continue
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            if path.stat().st_size > max_bytes:
                continue
        except OSError:
            continue
        source = _read_source(path)
        if source is None:
            continue
        yield relative.as_posix(), source
