"""Regression checks for the GitLab CI hardenings (no YAML dependency)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ("ci/gitlab-codereview.yml", "docs/gitlab-codereview.example.yml")


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def ref_guard_script(path: str) -> str:
    lines = []
    for line in read(path).splitlines():
        stripped = line.strip()
        if stripped.startswith("- if ") and "CODEREVIEW_AGENT_REF" in stripped:
            lines.append(stripped[2:])
    return "\n".join(lines)


def test_agent_ci_does_not_use_gitlab_cache():
    text = read(".gitlab-ci.yml")
    assert 'PIP_NO_CACHE_DIR: "1"' in text
    assert "\n  cache:" not in text


def test_templates_disable_cache_for_protected_push_audit():
    for path in TEMPLATES:
        text = read(path)
        assert "cache: []" in text
        assert 'PIP_NO_CACHE_DIR: "1"' in text
        assert 'case "$CODEREVIEW_AGENT_REF" in main|master' not in text
        assert "grep -Eq" in text
        assert "git check-ref-format --branch" in text
        assert "CODEREVIEW_ALLOW_MUTABLE_AGENT_REF" in text


def test_ref_guard_accepts_single_line_tag():
    for path in TEMPLATES:
        env = {
            **os.environ,
            "CODEREVIEW_AGENT_REF": "v0.1.0",
            "CODEREVIEW_ALLOW_MUTABLE_AGENT_REF": "false",
        }
        result = subprocess.run(
            ["sh", "-c", ref_guard_script(path)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_ref_guard_rejects_multiline_ref():
    for path in TEMPLATES:
        env = {
            **os.environ,
            "CODEREVIEW_AGENT_REF": "v0.1.0\nmain",
            "CODEREVIEW_ALLOW_MUTABLE_AGENT_REF": "false",
        }
        result = subprocess.run(
            ["sh", "-c", ref_guard_script(path)],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
