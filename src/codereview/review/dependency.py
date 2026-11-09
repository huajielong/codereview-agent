"""Static Python import graph.

Two uses:

- ``build_dependency_graph`` keeps L6's multi-repo mapping (module -> repos that
  import it) for deployments that span repositories.
- ``render_changed_dependency_section`` builds an intra-repo graph from the
  checked-out workspace and tells the reviewer which repository files import
  the changed modules (and which repository modules the changed files import).
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


def index_repo(
    name: str,
    root: str | Path,
    *,
    max_files: int = 2000,
    max_file_bytes: int = 1_000_000,
) -> RepoIndex:
    root_path = Path(root)
    index = RepoIndex(name=name, root=root_path)
    for path in sorted(root_path.rglob("*.py")):
        if len(index.files) >= max_files:
            break
        try:
            parts = path.relative_to(root_path).parts
        except ValueError:
            continue
        if EXCLUDED_DIRS.intersection(parts):
            continue
        relative = path.relative_to(root_path).as_posix()
        index.files.append(relative)
        try:
            if path.stat().st_size > max_file_bytes:
                index.imports[relative] = set()
                continue
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
                    modules.add(alias.name)
                    modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module)
                modules.add(node.module.split(".")[0])
                for alias in node.names:
                    if alias.name and alias.name != "*":
                        modules.add(f"{node.module}.{alias.name}")
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


def module_candidates(relative_path: str) -> tuple[str, ...]:
    """Python module names a repository file can be imported as.

    ``pkg/api.py`` -> ``("pkg.api", "api")``; ``pkg/__init__.py`` -> ``("pkg",)``.
    The short name is kept because some repositories run with the package
    directory itself on ``sys.path``.
    """
    path = Path(relative_path)
    if path.suffix != ".py" or path.is_absolute() or ".." in path.parts:
        return ()
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return ()
    candidates = [".".join(parts)]
    if len(parts) > 1:
        candidates.append(parts[-1])
    return tuple(dict.fromkeys(candidates))


def build_import_map(index: RepoIndex) -> dict[str, set[str]]:
    """Map imported module name -> repository files that import it."""
    importers: dict[str, set[str]] = {}
    for relative, modules in index.imports.items():
        for module in modules:
            importers.setdefault(module, set()).add(relative)
    return importers


def _resolve_module(module: str, module_files: dict[str, str]) -> str | None:
    """Resolve an imported module name to a repository file (longest match)."""
    best: tuple[int, str] | None = None
    for candidate, path in module_files.items():
        if module == candidate or module.startswith(candidate + "."):
            if best is None or len(candidate) > best[0]:
                best = (len(candidate), path)
    return best[1] if best else None


def dependents_of(importers: dict[str, set[str]], relative_path: str) -> list[str]:
    """Repository files that import the given file."""
    candidates = module_candidates(relative_path)
    found: set[str] = set()
    for module, files in importers.items():
        if any(module == c or module.startswith(c + ".") for c in candidates):
            found.update(files)
    found.discard(relative_path)
    return sorted(found)


def render_changed_dependency_section(
    root: str | Path,
    changed_paths: list[str],
    *,
    repo_name: str = "repo",
    max_files: int = 2000,
    max_related: int = 6,
) -> str:
    """Render repository import relationships for the changed Python files."""
    index = index_repo(repo_name, root, max_files=max_files)
    if not index.files:
        return ""

    importers = build_import_map(index)
    module_files: dict[str, str] = {}
    for relative in index.files:
        for candidate in module_candidates(relative):
            module_files.setdefault(candidate, relative)

    lines: list[str] = []
    for relative in sorted(set(changed_paths)):
        if not relative.endswith(".py"):
            continue
        if not module_candidates(relative):
            continue

        imports: set[str] = set()
        for module in index.imports.get(relative, set()):
            target = _resolve_module(module, module_files)
            if target and target != relative:
                imports.add(target)
        if imports:
            ordered = sorted(imports)
            shown = ", ".join(ordered[:max_related])
            if len(ordered) > max_related:
                shown += f", ... and {len(ordered) - max_related} more"
            lines.append(f"- {relative} imports: {shown}")

        dependents = dependents_of(importers, relative)
        if dependents:
            shown = ", ".join(dependents[:max_related])
            if len(dependents) > max_related:
                shown += f", ... and {len(dependents) - max_related} more"
            lines.append(f"- {relative} is imported by: {shown}")

    if not lines:
        return ""
    return "\n".join(
        ["IMPACT CONTEXT (repository import relationships for changed files):"]
        + [escape_braces(line) for line in lines]
    )
