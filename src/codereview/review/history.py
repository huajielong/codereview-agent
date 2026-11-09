"""JSONL history of past review findings with keyword retrieval."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from codereview.text_utils import escape_braces, token_set


def _record_key(record: dict) -> tuple:
    return (
        str(record.get("repo", "")).lower(),
        str(record.get("pr", "")),
        str(record.get("file", "")),
        str(record.get("line", "")),
        str(record.get("issue", "")).strip().lower(),
    )


class HistoryStore:
    def __init__(self, path: str | Path, *, max_records: int = 2000):
        self.path = Path(path)
        self.max_records = max(1, max_records)
        self._lock = threading.Lock()

    def add(self, record: dict) -> bool:
        """Append one record unless the same finding was already stored."""
        return self.add_many([record]) == 1

    def add_many(self, records: list[dict]) -> int:
        """Append new records, dedupe by (repo, pr, file, line, issue), and trim."""
        if not records:
            return 0
        with self._lock:
            existing = self.all()
            seen = {_record_key(item) for item in existing}
            added = 0
            for record in records:
                key = _record_key(record)
                if key in seen:
                    continue
                seen.add(key)
                existing.append(record)
                added += 1
            if not added:
                return 0
            if len(existing) > self.max_records:
                existing = existing[-self.max_records :]
            self._write(existing)
            return added

    def _write(self, records: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        text = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records)
        temp.write_text(text, encoding="utf-8")
        temp.replace(self.path)

    def all(self) -> list[dict]:
        if not self.path.exists():
            return []
        records = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def search(self, query: str, limit: int = 5) -> list[dict]:
        query_tokens = token_set(query)
        if not query_tokens:
            return []
        scored = []
        for record in self.all():
            text = " ".join(
                str(record.get(key, ""))
                for key in ("issue", "comment", "file", "severity")
            )
            overlap = len(query_tokens & token_set(text))
            if overlap:
                scored.append((overlap, record))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [record for _, record in scored[:limit]]


def render_history_section(records: list[dict]) -> str:
    if not records:
        return ""
    lines = [
        "HISTORICAL REVIEW NOTES (similar findings in this repository; verify each "
        "against the current diff and do not repeat issues that no longer apply):"
    ]
    for record in records:
        issue = escape_braces(record.get("issue", ""))
        location = ""
        if record.get("file") and record.get("line"):
            location = f" ({escape_braces(record['file'])}:{record['line']})"
        lines.append(f"- {issue}{location} [{escape_braces(record.get('severity', ''))}]")
    return "\n".join(lines)
