"""Core data models shared by the pipeline, storage and evaluation layers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3}
VALID_SEVERITIES = ("low", "medium", "high")

_ALIASES = {
    "critical": "high",
    "blocker": "high",
    "error": "high",
    "warning": "medium",
    "warn": "medium",
    "info": "low",
    "informational": "low",
    "note": "low",
    "minor": "low",
    "major": "high",
}


def normalize_severity(value: Any) -> str:
    """Map model output to high/medium/low. Unknown values become medium."""
    text = str(value or "").strip().lower()
    if text in SEVERITY_ORDER:
        return text
    return _ALIASES.get(text, "medium")


def severity_at_least(value: str, minimum: str) -> bool:
    return SEVERITY_ORDER[normalize_severity(value)] >= SEVERITY_ORDER[normalize_severity(minimum)]


@dataclass
class Finding:
    issue: str
    severity: str = "medium"
    file: str | None = None
    line: int | None = None
    side: str = "RIGHT"
    evidence: str = ""
    source_agent: str = ""

    def __post_init__(self) -> None:
        self.issue = " ".join(str(self.issue or "").split())
        self.severity = normalize_severity(self.severity)
        self.side = self.side if self.side in ("LEFT", "RIGHT") else "RIGHT"
        if self.line is not None:
            try:
                self.line = int(self.line)
            except (TypeError, ValueError):
                self.line = None

    def to_dict(self) -> dict:
        return {
            "issue": self.issue,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "side": self.side,
            "evidence": self.evidence,
            "source_agent": self.source_agent,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Finding":
        return cls(
            issue=data.get("issue", ""),
            severity=data.get("severity", "medium"),
            file=data.get("file") or None,
            line=data.get("line") if data.get("line") not in (0, "", None) else None,
            side=data.get("side", "RIGHT"),
            evidence=data.get("evidence", "") or "",
            source_agent=data.get("source_agent", "") or "",
        )

    def key(self) -> tuple:
        return (
            " ".join(self.issue.lower().split()),
            (self.file or "").lower(),
            self.line or 0,
        )


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str
    lines: list[str] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    is_new: bool = False
    is_deleted: bool = False
    hunks: list[Hunk] = field(default_factory=list)


@dataclass(frozen=True)
class PRInfo:
    repo: str
    number: int
    title: str
    body: str
    author: str
    base_sha: str
    head_sha: str
    state: str
    draft: bool
    merged: bool
    html_url: str = ""
    changed_files: int = 0
    additions: int = 0
    deletions: int = 0
    installation_id: int | None = None

    @classmethod
    def from_api(cls, repo: str, payload: dict) -> "PRInfo":
        head = payload.get("head") or {}
        base = payload.get("base") or {}
        return cls(
            repo=repo,
            number=int(payload.get("number") or 0),
            title=payload.get("title") or "",
            body=payload.get("body") or "",
            author=(payload.get("user") or {}).get("login") or "",
            base_sha=base.get("sha") or "",
            head_sha=head.get("sha") or "",
            state=payload.get("state") or "open",
            draft=bool(payload.get("draft")),
            merged=bool(payload.get("merged")),
            html_url=payload.get("html_url") or "",
            changed_files=int(payload.get("changed_files") or 0),
            additions=int(payload.get("additions") or 0),
            deletions=int(payload.get("deletions") or 0),
        )

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "author": self.author,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "state": self.state,
            "draft": self.draft,
            "merged": self.merged,
            "html_url": self.html_url,
            "changed_files": self.changed_files,
            "additions": self.additions,
            "deletions": self.deletions,
        }


@dataclass
class PublishResult:
    posted: bool
    inline_comments: list[dict] = field(default_factory=list)
    summary_body: str = ""
    review_id: int | None = None
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "posted": self.posted,
            "inline_comments": self.inline_comments,
            "summary_body": self.summary_body,
            "review_id": self.review_id,
            "skipped_reason": self.skipped_reason,
            "errors": self.errors,
        }


@dataclass
class ReviewOutcome:
    run_id: str
    repo: str
    number: int
    head_sha: str
    mode: str
    status: str = "completed"
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    summary_body: str = ""
    inline_comments: list[dict] = field(default_factory=list)
    posted: bool = False
    review_id: int | None = None
    cache_hit: bool = False
    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    dry_run: bool = True
    index_stats: dict | None = None
    chunks: int = 0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "repo": self.repo,
            "number": self.number,
            "head_sha": self.head_sha,
            "mode": self.mode,
            "status": self.status,
            "findings": [f.to_dict() for f in self.findings],
            "errors": list(self.errors),
            "summary_body": self.summary_body,
            "inline_comments": list(self.inline_comments),
            "posted": self.posted,
            "review_id": self.review_id,
            "cache_hit": self.cache_hit,
            "llm_calls": self.llm_calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "duration_ms": self.duration_ms,
            "dry_run": self.dry_run,
            "index_stats": self.index_stats,
            "chunks": self.chunks,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewOutcome":
        return cls(
            run_id=data.get("run_id", ""),
            repo=data.get("repo", ""),
            number=int(data.get("number") or 0),
            head_sha=data.get("head_sha", ""),
            mode=data.get("mode", "security"),
            status=data.get("status", "completed"),
            findings=[Finding.from_dict(f) for f in data.get("findings", [])],
            errors=list(data.get("errors", [])),
            summary_body=data.get("summary_body", ""),
            inline_comments=list(data.get("inline_comments", [])),
            posted=bool(data.get("posted")),
            review_id=data.get("review_id"),
            cache_hit=bool(data.get("cache_hit")),
            llm_calls=int(data.get("llm_calls") or 0),
            prompt_tokens=int(data.get("prompt_tokens") or 0),
            completion_tokens=int(data.get("completion_tokens") or 0),
            duration_ms=int(data.get("duration_ms") or 0),
            dry_run=bool(data.get("dry_run", True)),
            index_stats=data.get("index_stats"),
            chunks=int(data.get("chunks") or 0),
        )


@dataclass
class IndexStats:
    repo: str
    ref: str
    files: int
    chunks: int
    reused_files: int = 0
    embedded_chunks: int = 0
    skipped: bool = False
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "repo": self.repo,
            "ref": self.ref,
            "files": self.files,
            "chunks": self.chunks,
            "reused_files": self.reused_files,
            "embedded_chunks": self.embedded_chunks,
            "skipped": self.skipped,
            "duration_ms": self.duration_ms,
        }
