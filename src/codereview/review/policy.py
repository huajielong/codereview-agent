"""JSON policy rules with glob matching, prompt injection and an audit trail."""
from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codereview.text_utils import escape_braces

VALID_SEVERITIES = {"high", "medium", "low"}


@dataclass
class PolicyRule:
    id: str
    description: str
    severity: str = "medium"
    glob: str = "**/*"


@dataclass
class PolicySet:
    rules: list[PolicyRule] = field(default_factory=list)

    def matching(self, path: str) -> list[PolicyRule]:
        return [rule for rule in self.rules if _matches(path, rule.glob)]

    def fingerprint(self) -> str:
        blob = json.dumps(
            [asdict(rule) for rule in self.rules], sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def _matches(path: str, pattern: str) -> bool:
    if fnmatch.fnmatch(path, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(path, pattern[3:])
    return False


def load_policy(path: str | Path) -> PolicySet:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rules = [PolicyRule(**rule) for rule in (data.get("rules") or [])]
    unknown = [rule.severity for rule in rules if rule.severity not in VALID_SEVERITIES]
    if unknown:
        raise ValueError(f"unknown policy severity: {unknown}")
    return PolicySet(rules=rules)


def render_policy_section(policy: PolicySet, paths: list[str]) -> str:
    matched: list[PolicyRule] = []
    seen: set[str] = set()
    for path in paths:
        for rule in policy.matching(path):
            if rule.id in seen:
                continue
            seen.add(rule.id)
            matched.append(rule)
    if not matched:
        return ""
    lines = ["POLICY REQUIREMENTS (organization rules):"]
    for rule in matched:
        lines.append(
            f"- [{escape_braces(rule.id)}] ({escape_braces(rule.severity)}) "
            f"{escape_braces(rule.description)}"
        )
    return "\n".join(lines)


def append_audit(path: str | Path, record: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {**record, "timestamp": datetime.now(timezone.utc).isoformat()}
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
