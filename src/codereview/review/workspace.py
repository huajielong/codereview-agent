"""Download and cache repository tarballs for indexing.

Extraction is intentionally strict: only regular files and directories, no
absolute paths, no ``..`` traversal, no symlinks, and a hard byte limit.
"""
from __future__ import annotations

import hashlib
import io
import shutil
import tarfile
import threading
import uuid
from pathlib import Path

from codereview.config import Settings
from codereview.errors import IndexError_
from codereview.logging_utils import get_logger

log = get_logger(__name__)


def _safe_slug(repo: str) -> str:
    return repo.replace("/", "__").replace("\\", "__").replace(":", "_")


def extract_tarball(data: bytes, target: Path, *, max_bytes: int, max_files: int = 50_000) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    written = 0
    file_count = 0

    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > max_files:
            raise IndexError_(f"tarball has too many entries: {len(members)}")

        names = [member.name for member in members if member.name]
        safe_names = [
            name
            for name in names
            if not name.startswith("/") and ".." not in Path(name).parts
        ]
        top_levels = {Path(name).parts[0] for name in safe_names if Path(name).parts}
        strip_root = len(top_levels) == 1

        for member in members:
            if not (member.isfile() or member.isdir()):
                continue
            name = member.name
            parts = Path(name).parts
            if not parts or name.startswith("/") or ".." in parts:
                continue
            if strip_root:
                parts = parts[1:]
            if not parts:
                continue
            destination = target.joinpath(*parts)
            try:
                destination.relative_to(target)
            except ValueError:
                continue

            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue

            file_count += 1
            if file_count > max_files:
                raise IndexError_("tarball has too many files")
            written += member.size
            if written > max_bytes:
                raise IndexError_(f"extracted repository exceeds {max_bytes} bytes")

            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with destination.open("wb") as fh:
                shutil.copyfileobj(source, fh, length=1024 * 1024)

    return target


class RepoWorkspace:
    def __init__(self, settings: Settings, github):
        self.settings = settings
        self.github = github
        self.root = Path(settings.cache_dir) / "repos"
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def ensure(
        self, repo: str, ref: str, installation_id: int | None = None
    ) -> Path:
        if not self.github:
            raise IndexError_("GitHub client is not configured")
        if not ref:
            raise IndexError_("ref is required to download a repository")

        safe_ref = ref if all(ch.isalnum() or ch in "-_." for ch in ref) else hashlib.sha1(ref.encode()).hexdigest()
        target = self.root / _safe_slug(repo) / safe_ref
        marker = target / ".ready"
        if marker.exists():
            return target

        with self._lock_for(f"{repo}@{ref}"):
            if marker.exists():
                return target

            data = self.github.download_tarball(repo, ref, installation_id)
            temp = target.with_name(f"{target.name}.tmp-{uuid.uuid4().hex[:8]}")
            try:
                extract_tarball(
                    data,
                    temp,
                    max_bytes=self.settings.repo_max_mb * 1024 * 1024,
                )
                (temp / ".ready").write_text("ok", encoding="utf-8")
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                temp.replace(target)
            except Exception:
                shutil.rmtree(temp, ignore_errors=True)
                raise

            self._prune(repo, keep=target)
            return target

    def _prune(self, repo: str, keep: Path) -> None:
        parent = self.root / _safe_slug(repo)
        if not parent.exists():
            return
        refs = [path for path in parent.iterdir() if path.is_dir() and path != keep]
        refs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        for old in refs[self.settings.repo_cache_keep - 1:]:
            log.info("prune_repo_cache", extra={"path": str(old)})
            shutil.rmtree(old, ignore_errors=True)
