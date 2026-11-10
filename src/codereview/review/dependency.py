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
import posixpath
import re
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from codereview.context.languages import CPP_EXTENSIONS, JS_EXTENSIONS
from codereview.text_utils import escape_braces

EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "site-packages",
    "node_modules", "build", "dist", ".tox", ".mypy_cache",
    "vendor", "third_party", "third-party", "external", "deps",
}
_BAD_SOURCE = (SyntaxError, UnicodeDecodeError, ValueError, OSError, LookupError)

WORKSPACE_MANIFESTS = (
    "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "tsconfig.json",
    "CMakeLists.txt", "Makefile", "meson.build",
    "BUILD", "BUILD.bazel",
    "go.mod", "Cargo.toml",
)


@dataclass
class RepoIndex:
    name: str
    root: Path
    files: list[str] = field(default_factory=list)
    imports: dict[str, set[str]] = field(default_factory=dict)


def _absolute_import_base(relative_path: str, level: int) -> str | None:
    """Resolve a relative ``ImportFrom`` base against the file's package."""
    parts = list(Path(relative_path).parts[:-1])
    if level > 1:
        trim = level - 1
        if trim > len(parts):
            return None
        parts = parts[: len(parts) - trim]
    return ".".join(parts) if parts else None


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
            elif isinstance(node, ast.ImportFrom):
                base: str | None = None
                if node.level == 0 and node.module:
                    base = node.module
                elif node.level > 0:
                    prefix = _absolute_import_base(relative, node.level)
                    if prefix:
                        base = f"{prefix}.{node.module}" if node.module else prefix
                if base:
                    modules.add(base)
                    modules.add(base.split(".")[0])
                    for alias in node.names:
                        if alias.name and alias.name != "*":
                            modules.add(f"{base}.{alias.name}")
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


def render_python_dependency_section(
    root: str | Path,
    changed_paths: list[str],
    *,
    repo_name: str = "repo",
    max_files: int = 2000,
    max_related: int = 6,
) -> str:
    """Render repository import relationships for the changed Python files."""
    python_paths = [path for path in changed_paths if path.endswith(".py")]
    if not python_paths:
        return ""

    index = index_repo(repo_name, root, max_files=max_files)
    if not index.files:
        return ""

    importers = build_import_map(index)
    module_files: dict[str, str] = {}
    for relative in index.files:
        for candidate in module_candidates(relative):
            module_files.setdefault(candidate, relative)

    lines: list[str] = []
    for relative in sorted(set(python_paths)):
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


def find_workspace_root(
    root: str | Path,
    relative_path: str,
    manifests: tuple[str, ...] = WORKSPACE_MANIFESTS,
) -> Path:
    """Nearest ancestor with a build/package manifest, else the repo root."""
    root_path = Path(root).resolve()
    current = (root_path / relative_path).resolve().parent
    while True:
        if any((current / manifest).is_file() for manifest in manifests):
            return current
        if current == root_path or root_path not in current.parents:
            return root_path
        current = current.parent


def _workspace_relative(root: Path, relative_path: str) -> str:
    workspace = find_workspace_root(root, relative_path)
    try:
        value = workspace.relative_to(root.resolve()).as_posix()
    except ValueError:
        return ""
    return "" if value == "." else value


def _iter_language_files(
    root: str | Path,
    extensions: set[str],
    max_files: int,
    max_file_bytes: int,
) -> list[tuple[str, Path]]:
    root_path = Path(root)
    found: list[tuple[str, Path]] = []
    for path in sorted(root_path.rglob("*")):
        if len(found) >= max_files:
            break
        try:
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root_path)
        except OSError:
            continue
        if EXCLUDED_DIRS.intersection(relative.parts):
            continue
        if path.suffix.lower() not in extensions:
            continue
        try:
            if path.stat().st_size > max_file_bytes:
                continue
        except OSError:
            continue
        found.append((relative.as_posix(), path))
    return found


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


_CPP_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)


def _resolve_cpp_include(
    files: set[str],
    relative: str,
    include: str,
    workspace_relative: str,
) -> str | None:
    include_path = posixpath.normpath(include)
    candidates = [
        posixpath.normpath(posixpath.join(posixpath.dirname(relative), include)),
        include_path,
    ]
    for prefix in ("", "include", "src", "inc"):
        base = (
            posixpath.join(workspace_relative, prefix)
            if workspace_relative
            else prefix
        )
        candidates.append(posixpath.normpath(posixpath.join(base, include)))
    for candidate in candidates:
        if candidate in files:
            return candidate
    suffix = "/" + include_path
    matches = [
        item for item in files if item == include_path or item.endswith(suffix)
    ]
    return sorted(matches, key=len)[0] if matches else None


def index_cpp_includes(
    repo_name: str,
    root: str | Path,
    *,
    max_files: int = 2000,
    max_file_bytes: int = 1_000_000,
) -> dict[str, set[str]]:
    """Map C/C++ file -> resolved local headers it includes."""
    root_path = Path(root)
    entries = _iter_language_files(
        root_path, CPP_EXTENSIONS, max_files, max_file_bytes
    )
    files = {relative for relative, _ in entries}
    includes: dict[str, set[str]] = {relative: set() for relative in files}
    for relative, path in entries:
        source = _read_text(path)
        if source is None:
            continue
        workspace_relative = _workspace_relative(root_path, relative)
        for match in _CPP_INCLUDE_RE.finditer(source):
            target = _resolve_cpp_include(
                files, relative, match.group(1), workspace_relative
            )
            if target and target != relative:
                includes[relative].add(target)
    return includes


def render_cpp_dependency_section(
    root: str | Path,
    changed_paths: list[str],
    *,
    repo_name: str = "repo",
    max_files: int = 2000,
    max_related: int = 6,
) -> str:
    """Render C/C++ ``#include`` relationships for changed files."""
    changed = [
        path
        for path in changed_paths
        if Path(path).suffix.lower() in CPP_EXTENSIONS
    ]
    if not changed:
        return ""
    includes = index_cpp_includes(repo_name, root, max_files=max_files)
    if not includes:
        return ""

    included_by: dict[str, set[str]] = {}
    for source, targets in includes.items():
        for target in targets:
            included_by.setdefault(target, set()).add(source)

    lines: list[str] = []
    for relative in sorted(set(changed)):
        if relative not in includes:
            continue
        imports = sorted(includes[relative])
        if imports:
            shown = ", ".join(imports[:max_related])
            if len(imports) > max_related:
                shown += f", ... and {len(imports) - max_related} more"
            lines.append(f"- {relative} includes: {shown}")
        dependents = sorted(included_by.get(relative, set()))
        if dependents:
            shown = ", ".join(dependents[:max_related])
            if len(dependents) > max_related:
                shown += f", ... and {len(dependents) - max_related} more"
            lines.append(f"- {relative} is included by: {shown}")

    if not lines:
        return ""
    return "\n".join(
        ["IMPACT CONTEXT (C/C++ include relationships for changed files):"]
        + [escape_braces(line) for line in lines]
    )


_JS_IMPORT_RE = re.compile(
    r"""(?:import|export)\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)['"]"""
)
_JS_REQUIRE_RE = re.compile(r"""require\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_DYNAMIC_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]\s*\)""")
_JS_RESOLVE_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _resolve_js_import(
    files: set[str], relative: str, spec: str
) -> str | None:
    base = posixpath.normpath(
        posixpath.join(posixpath.dirname(relative), spec)
    )
    candidates = [base]
    suffix = Path(base).suffix.lower()
    if suffix in JS_EXTENSIONS:
        stem = base[: -len(suffix)]
        candidates.extend(stem + ext for ext in _JS_RESOLVE_EXTENSIONS)
    for extension in _JS_RESOLVE_EXTENSIONS:
        candidates.append(base + extension)
        candidates.append(base + "/index" + extension)
    for candidate in candidates:
        if candidate in files:
            return candidate
    return None


def index_js_imports(
    repo_name: str,
    root: str | Path,
    *,
    max_files: int = 2000,
    max_file_bytes: int = 1_000_000,
) -> dict[str, set[str]]:
    """Map JS/TS file -> resolved relative imports."""
    entries = _iter_language_files(
        root, JS_EXTENSIONS, max_files, max_file_bytes
    )
    files = {relative for relative, _ in entries}
    imports: dict[str, set[str]] = {relative: set() for relative in files}
    for relative, path in entries:
        source = _read_text(path)
        if source is None:
            continue
        specs: list[str] = []
        for pattern in (_JS_IMPORT_RE, _JS_REQUIRE_RE, _JS_DYNAMIC_RE):
            specs.extend(match.group(1) for match in pattern.finditer(source))
        for spec in specs:
            if not spec.startswith("."):
                continue
            target = _resolve_js_import(files, relative, spec)
            if target and target != relative:
                imports[relative].add(target)
    return imports


def render_js_dependency_section(
    root: str | Path,
    changed_paths: list[str],
    *,
    repo_name: str = "repo",
    max_files: int = 2000,
    max_related: int = 6,
) -> str:
    """Render JS/TS relative import relationships for changed files."""
    changed = [
        path
        for path in changed_paths
        if Path(path).suffix.lower() in JS_EXTENSIONS
    ]
    if not changed:
        return ""
    imports = index_js_imports(repo_name, root, max_files=max_files)
    if not imports:
        return ""

    imported_by: dict[str, set[str]] = {}
    for source, targets in imports.items():
        for target in targets:
            imported_by.setdefault(target, set()).add(source)

    lines: list[str] = []
    for relative in sorted(set(changed)):
        if relative not in imports:
            continue
        targets = sorted(imports[relative])
        if targets:
            shown = ", ".join(targets[:max_related])
            if len(targets) > max_related:
                shown += f", ... and {len(targets) - max_related} more"
            lines.append(f"- {relative} imports: {shown}")
        dependents = sorted(imported_by.get(relative, set()))
        if dependents:
            shown = ", ".join(dependents[:max_related])
            if len(dependents) > max_related:
                shown += f", ... and {len(dependents) - max_related} more"
            lines.append(f"- {relative} is imported by: {shown}")

    if not lines:
        return ""
    return "\n".join(
        ["IMPACT CONTEXT (JS/TS import relationships for changed files):"]
        + [escape_braces(line) for line in lines]
    )


def render_changed_dependency_section(
    root: str | Path,
    changed_paths: list[str],
    *,
    repo_name: str = "repo",
    max_files: int = 2000,
    max_related: int = 6,
) -> str:
    """Dispatch dependency rendering to each supported changed language."""
    sections: list[str] = []
    for renderer in (
        render_python_dependency_section,
        render_cpp_dependency_section,
        render_js_dependency_section,
    ):
        section = renderer(
            root,
            changed_paths,
            repo_name=repo_name,
            max_files=max_files,
            max_related=max_related,
        )
        if section:
            sections.append(section)
    return "\n\n".join(sections)
