"""Pre-flight checks before exposing the webhook.

Offline: no GitHub/LLM calls. Verifies config, credentials, writable paths,
policy/prompt files and the SQLite schema. Exit code 1 if a critical check fails.

    python -I -X utf8 scripts/check_production.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv


def check(settings) -> dict:
    from codereview.llm.prompts import PromptRegistry
    from codereview.review.policy import load_policy
    from codereview.storage.db import Database

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, critical: bool = True, warning: bool = False) -> None:
        checks.append(
            {
                "name": name,
                "ok": ok,
                "detail": detail,
                "critical": critical,
                "warning": warning,
            }
        )

    add("llm_model", bool(settings.model), settings.model or "OPENAI_MODEL is empty")
    add("llm_key", bool(settings.openai_api_key), "OPENAI_API_KEY is set" if settings.openai_api_key else "OPENAI_API_KEY is empty")
    add(
        "embedding_channel",
        settings.embedding_available(),
        "Cohere" if settings.cohere_api_key and settings.cohere_embed_model else "OpenAI-compatible",
        critical=False,
        warning=not settings.embedding_available(),
    )
    add(
        "webhook_secret",
        bool(settings.github_webhook_secret) and len(settings.github_webhook_secret or "") >= 16,
        "set and >=16 chars" if settings.github_webhook_secret else "GITHUB_WEBHOOK_SECRET is empty",
    )

    if settings.has_github_app():
        key = Path(settings.github_private_key_path or "")
        add("github_app_key", key.is_file() and os.access(key, os.R_OK), f"private key path exists: {key}")
        add("github_app_id", bool(settings.github_app_id), settings.github_app_id or "GITHUB_APP_ID is empty")
    else:
        add(
            "github_write_auth",
            bool(settings.github_token),
            "GITHUB_TOKEN set (PAT fallback)" if settings.github_token else "no GitHub App and no GITHUB_TOKEN",
        )

    if settings.policy_enabled:
        policy_path = Path(settings.policy_path)
        if policy_path.is_file():
            try:
                policy = load_policy(policy_path)
                add("policy", True, f"{len(policy.rules)} rules loaded")
            except Exception as exc:  # noqa: BLE001
                add("policy", False, f"cannot load policy: {exc}")
        else:
            add("policy", False, f"missing policy file: {policy_path}", critical=False, warning=True)

    prompt_dir = Path(settings.prompt_dir)
    if prompt_dir.is_dir():
        try:
            loaded = PromptRegistry().load_directory(prompt_dir, strict=True)
            add("prompts", True, f"{loaded} override(s) loaded", critical=False)
        except Exception as exc:  # noqa: BLE001
            add("prompts", False, f"invalid prompt template: {exc}", critical=False, warning=True)
    else:
        add("prompts", True, "no prompt directory; built-in prompts used", critical=False, warning=True)

    for name, path in (
        ("database_dir", Path(settings.db_path).parent),
        ("chroma_dir", Path(settings.chroma_path)),
        ("cache_dir", Path(settings.cache_dir)),
        ("audit_dir", Path(settings.audit_path).parent),
    ):
        path.mkdir(parents=True, exist_ok=True)
        add(name, os.access(path, os.W_OK), f"writable: {path}")

    try:
        db = Database(settings.db_path)
        stats = db.stats()
        db.close()
        add("sqlite", True, f"schema ok, runs={stats['runs']}")
    except Exception as exc:  # noqa: BLE001
        add("sqlite", False, f"cannot open database: {exc}")

    if not settings.dry_run:
        add(
            "posting_enabled",
            True,
            "REVIEW_DRY_RUN=false: reviews will be posted",
            critical=False,
            warning=True,
        )
    if not settings.allowed_repos:
        add(
            "repository_allowlist",
            True,
            "REVIEW_ALLOWED_REPOS is empty: every installation repo is allowed",
            critical=False,
            warning=True,
        )
    if settings.review_drafts:
        add(
            "draft_prs",
            True,
            "REVIEW_DRAFTS=true: draft PRs will be reviewed",
            critical=False,
            warning=True,
        )
    if settings.post_on_partial:
        add(
            "partial_reviews",
            True,
            "REVIEW_POST_ON_PARTIAL=true: partial reviews may be posted",
            critical=False,
            warning=True,
        )
    if settings.concurrency > 1:
        add(
            "concurrency",
            True,
            "REVIEW_CONCURRENCY>1: multiple in-process workers share one Chroma index; keep a single replica",
            critical=False,
            warning=True,
        )

    failed = [item for item in checks if item["critical"] and not item["ok"]]
    return {
        "ready": not failed,
        "failed": [item["name"] for item in failed],
        "warnings": [item["name"] for item in checks if item["warning"]],
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    from codereview.config import Settings

    parser = argparse.ArgumentParser(description="Production readiness checks (offline)")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    report = check(settings)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
