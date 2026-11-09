"""Parse model output into Findings with file/line where available."""
from __future__ import annotations

import re

from codereview.models import Finding

_ISSUE_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\*{0,2}\s*ISSUE\s*#?\s*\d*\s*\*{0,2}\s*[:：]\s*\*{0,2}\s*(.+?)\s*\*{0,2}\s*$",
    re.IGNORECASE,
)
_SEVERITY_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\*{0,2}\s*SEVERITY\s*\*{0,2}\s*[:：]\s*\*{0,2}\s*([A-Za-z㐀-鿿]+)",
    re.IGNORECASE,
)
_FILE_RE = re.compile(
    r"^\s*\*{0,2}\s*FILE\s*\*{0,2}\s*[:：]\s*\*{0,2}\s*(.+?)\s*\*{0,2}\s*$",
    re.IGNORECASE,
)
_LINE_RE = re.compile(
    r"^\s*\*{0,2}\s*LINE\s*\*{0,2}\s*[:：]\s*\*{0,2}\s*(\d+)\s*\*{0,2}\s*$",
    re.IGNORECASE,
)
_EVIDENCE_RE = re.compile(
    r"^\s*\*{0,2}\s*EVIDENCE\s*\*{0,2}\s*[:：]\s*\*{0,2}\s*(.+?)\s*\*{0,2}\s*$",
    re.IGNORECASE,
)


def _clean_path(value: str) -> str | None:
    path = value.strip().strip("`\"'")
    for prefix in ("./", "a/", "b/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
    if not path or path.upper() in ("UNKNOWN", "N/A", "NONE"):
        return None
    return path


def parse_structured_findings(
    content: str, default_file: str | None = None, default_severity: str = "medium"
) -> list[Finding]:
    findings: list[Finding] = []
    current: Finding | None = None
    pending_severity: str | None = None
    pending_file: str | None = None
    pending_line: int | None = None

    def flush() -> None:
        nonlocal current
        if current and current.issue:
            findings.append(current)
        current = None

    for line in (content or "").splitlines():
        issue_match = _ISSUE_RE.match(line)
        if issue_match:
            flush()
            current = Finding(
                issue=issue_match.group(1).strip(),
                severity=pending_severity or default_severity,
                file=pending_file or default_file,
                line=pending_line,
            )
            pending_severity = None
            pending_file = None
            pending_line = None
            continue

        severity_match = _SEVERITY_RE.match(line)
        if severity_match:
            value = severity_match.group(1).strip().lower()
            if current:
                current.severity = value
            else:
                pending_severity = value
            continue

        file_match = _FILE_RE.match(line)
        if file_match:
            path = _clean_path(file_match.group(1))
            if current:
                current.file = path
            else:
                pending_file = path
            continue

        line_match = _LINE_RE.match(line)
        if line_match:
            number = int(line_match.group(1)) or None
            if current:
                current.line = number
            else:
                pending_line = number
            continue

        evidence_match = _EVIDENCE_RE.match(line)
        if evidence_match and current:
            current.evidence = evidence_match.group(1).strip()

    flush()
    return findings
