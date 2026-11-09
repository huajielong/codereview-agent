"""JSONL history of past review findings with keyword retrieval."""
from __future__ import annotations

import json
from pathlib import Path

from codereview.text_utils import escape_braces, token_set


class HistoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def add(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

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
    lines = ["HISTORICAL REVIEW NOTES (similar findings in this repository):"]
    for record in records:
        issue = escape_braces(record.get("issue", ""))
        location = ""
        if record.get("file") and record.get("line"):
            location = f" ({escape_braces(record['file'])}:{record['line']})"
        lines.append(f"- {issue}{location} [{escape_braces(record.get('severity', ''))}]")
    return "\n".join(lines)
