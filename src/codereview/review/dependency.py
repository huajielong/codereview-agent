"""Static multi-repo Python import graph.

Kept from L6 for deployments that actually span repositories. It is not wired
into the default pipeline because a single-repo installation does not need it.
"""
from __future__ import annotations

import ast
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from codereview.text_utils import escape_braces

EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "site-packages",
    "node_modules", "build", "dist", ".tox", ".mypy_cache",
}
_BAD_SOURCE = (SyntaxError, UnicodeDecodeError, ValueError, OSError, LookupError)


@dataclass
class RepoIndex:
    name: str
    root: Path
    files: list[str] = field(default_factory=list)
    imports: dict[str, set[str]] = field(default_factory=dict)


def index_repo(name: str, root: str | Path) -> RepoIndex:
    root_path = Path(root)
    index = RepoIndex(name=name, root=root_path)
    for path in sorted(root_path.rglob("*.py")):
        try:
            parts = path.relative_to(root_path).parts
        except ValueError:
            continue
        if EXCLUDED_DIRS.intersection(parts):
            continue
        relative = path.relative_to(root_path).as_posix()
        index.files.append(relative)
        try:
            with tokenize.open(path) as fh:
                source = fh.read()
            tree = ast.parse(source)
        except _BAD_SOURCE:
            index.imports[relative] = set()
            continue

        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
        index.imports[relative] = modules
    return index


def build_dependency_graph(
    indexes: list[RepoIndex], package_map: dict[str, str]
) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for index in indexes:
        for modules in index.imports.values():
            for module in modules:
                target = package_map.get(module)
                if target and target != index.name:
                    graph.setdefault(target, set()).add(index.name)
    return graph


def query_dependents(graph: dict[str, set[str]], module: str) -> list[str]:
    return sorted(graph.get(module, set()))


def render_dependency_section(graph: dict[str, set[str]], module: str) -> str:
    dependents = query_dependents(graph, module)
    if not dependents:
        return ""
    lines = ["CROSS-REPO CONTEXT:", f"- {escape_braces(module)} is imported by:"]
    for dependent in dependents:
        lines.append(f"  - {escape_braces(dependent)}")
    return "\n".join(lines)
