"""JSONL dataset for real-PR evaluation.

One line per PR::

    {"id": "owner/repo#42", "repo": "owner/repo", "number": 42,
     "head_sha": "...", "title": "...", "body": "...", "diff": "...",
     "expected": [{"file": "app.py", "line": 12, "issue": "SQL injection", "severity": "high"}],
     "source": "real", "meta": {"workspace": "path/to/checkout"}}
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codereview.models import normalize_severity


@dataclass
class ExpectedFinding:
    issue: str
    file: str | None = None
    line: int | None = None
    severity: str = "medium"

    def to_dict(self) -> dict:
        return {
            "issue": self.issue,
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
        }

    @classmethod
    def from_value(cls, value: Any) -> "ExpectedFinding":
        if isinstance(value, str):
            return cls(issue=value)
        if isinstance(value, dict):
            line = value.get("line")
            return cls(
                issue=str(value.get("issue") or value.get("description") or ""),
                file=value.get("file") or None,
                line=int(line) if line not in (None, "", 0) else None,
                severity=normalize_severity(value.get("severity", "medium")),
            )
        raise ValueError(f"invalid expected finding: {value!r}")


@dataclass
class DatasetPR:
    id: str
    repo: str
    number: int
    title: str
    body: str
    diff: str
    expected: list[ExpectedFinding] = field(default_factory=list)
    head_sha: str = ""
    source: str = "real"
    labeled: bool = True
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo": self.repo,
            "number": self.number,
            "head_sha": self.head_sha,
            "title": self.title,
            "body": self.body,
            "diff": self.diff,
            "expected": [item.to_dict() for item in self.expected],
            "source": self.source,
            "labeled": self.labeled,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DatasetPR":
        repo = str(data.get("repo") or "")
        number = int(data.get("number") or 0)
        if not repo or not number:
            raise ValueError("dataset record requires repo and number")
        diff = data.get("diff")
        if not isinstance(diff, str) or not diff.strip():
            raise ValueError(f"{repo}#{number}: diff is empty")
        expected = [
            ExpectedFinding.from_value(item) for item in (data.get("expected") or [])
        ]
        expected = [item for item in expected if item.issue.strip()]
        return cls(
            id=str(data.get("id") or f"{repo}#{number}"),
            repo=repo,
            number=number,
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            diff=diff,
            expected=expected,
            head_sha=str(data.get("head_sha") or ""),
            source=str(data.get("source") or "real"),
            # An explicit ``labeled: true`` with expected=[] means "we checked
            # this PR and there should be no findings". That must count false
            # positives, unlike an unlabeled shadow record.
            labeled=bool(data["labeled"]) if "labeled" in data else bool(expected),
            meta=dict(data.get("meta") or {}),
        )


def load_dataset(path: str | Path) -> list[DatasetPR]:
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"dataset not found: {target}")
    records: list[DatasetPR] = []
    for number, line in enumerate(target.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            records.append(DatasetPR.from_dict(json.loads(line)))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"{target}:{number}: {exc}") from exc
    return records


def save_dataset(path: str | Path, prs: list[DatasetPR]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for pr in prs:
            fh.write(json.dumps(pr.to_dict(), ensure_ascii=False) + "\n")


def upsert_dataset(path: str | Path, pr: DatasetPR) -> None:
    target = Path(path)
    existing = load_dataset(target) if target.exists() else []
    kept = [item for item in existing if item.id != pr.id]
    kept.append(pr)
    save_dataset(target, kept)


def dataset_stats(prs: list[DatasetPR]) -> dict:
    labeled = [pr for pr in prs if pr.labeled]
    line_labeled = [
        pr
        for pr in prs
        if pr.labeled and any(item.file and item.line for item in pr.expected)
    ]
    return {
        "total": len(prs),
        "labeled": len(labeled),
        "line_labeled": len(line_labeled),
        "expected_findings": sum(len(pr.expected) for pr in prs),
        "sources": sorted({pr.source for pr in prs}),
    }
