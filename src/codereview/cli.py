"""Console entry point.

PR mode::

    codereview-review --repo owner/repo --pr 42 --post

Push audit mode (no PR to comment on; exits non-zero on findings)::

    codereview-review --repo owner/repo --push \
      --before <sha> --after <sha> --branch main --fail-on-severity high
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

from codereview.config import Settings
from codereview.errors import ConfigError
from codereview.logging_utils import setup_logging
from codereview.models import severity_at_least
from codereview.review.factory import build_pipeline


def review_exit_code(outcome, *, post: bool) -> int:
    """Exit code for PR mode.

    A green required check must mean the review was posted. A partial LLM
    failure that skipped posting is a silent no-review and must fail the check.
    """
    if outcome.status == "failed":
        return 1
    if post and outcome.status == "completed_with_errors" and not outcome.posted:
        return 2
    return 0


def _write_json(path: str | None, payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if path:
        Path(path).write_text(text, encoding="utf-8")
    print(text)
    return text


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Review a GitHub PR or audit a push")
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr", type=int, default=None, help="PR number (PR mode)")
    parser.add_argument("--installation-id", type=int, default=None)
    parser.add_argument("--head-sha", default=None)
    parser.add_argument("--mode", choices=["general", "security", "pattern", "ensemble"])
    parser.add_argument("--dry-run", action="store_true", help="force dry-run")
    parser.add_argument("--post", action="store_true", help="post the review")
    parser.add_argument("--force", action="store_true", help="ignore the review cache")
    parser.add_argument("--json-out", default=None)

    # Push audit mode: compare before...after and fail the workflow on findings.
    parser.add_argument("--push", action="store_true", help="audit a push instead of a PR")
    parser.add_argument("--before", default=None, help="push before SHA")
    parser.add_argument("--after", default=None, help="push after SHA")
    parser.add_argument("--branch", default=None, help="push branch name")
    parser.add_argument("--push-title", default="", help="commit message for the audit prompt")
    parser.add_argument(
        "--fail-on-severity",
        choices=["high", "medium", "low", "none"],
        default="high",
        help="exit 1 when a finding reaches this severity (push mode)",
    )
    args = parser.parse_args(argv)

    if args.push:
        if not (args.before and args.after and args.branch):
            parser.error("--push requires --before, --after and --branch")
    elif args.pr is None:
        parser.error("--pr is required unless --push is used")

    try:
        settings = Settings.from_env()
        if args.mode:
            settings = replace(settings, mode=args.mode)
        if args.post:
            settings = replace(settings, dry_run=False)
        if args.dry_run:
            settings = replace(settings, dry_run=True)
        # Opt-in: without LOG_LEVEL the CLI stays quiet so stdout remains a
        # single JSON document for scripts. The Actions workflow sets it.
        if os.getenv("LOG_LEVEL"):
            setup_logging(settings.log_level)
        pipeline = build_pipeline(settings)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return 2

    try:
        if args.push:
            if set(args.before) == {"0"}:
                _write_json(
                    args.json_out,
                    {"status": "skipped", "reason": "new branch push (before is all zeros)"},
                )
                return 0
            try:
                diff = pipeline.github.get_compare_diff(
                    args.repo, args.before, args.after, args.installation_id
                )
            except Exception as exc:  # noqa: BLE001
                _write_json(args.json_out, {"status": "failed", "error": f"compare failed: {exc}"})
                return 2
            outcome = pipeline.review_diff(
                repo=args.repo,
                number=0,
                title=f"Push to {args.branch}",
                body=args.push_title,
                diff=diff,
                head_sha=args.after,
                dry_run=True,
            )
            _write_json(args.json_out, outcome.to_dict())
            if outcome.status == "failed":
                return 2
            if outcome.status == "completed_with_errors":
                # An audit that could not complete must not look green.
                return 2
            if args.fail_on_severity == "none":
                return 0
            failing = [
                finding
                for finding in outcome.findings
                if severity_at_least(finding.severity, args.fail_on_severity)
            ]
            return 1 if failing else 0

        outcome = pipeline.review_pr(
            args.repo,
            args.pr,
            args.installation_id or settings.github_installation_id,
            head_sha=args.head_sha,
            force=args.force,
        )
        _write_json(args.json_out, outcome.to_dict())
        return review_exit_code(outcome, post=args.post)
    finally:
        if getattr(pipeline, "github", None) is not None:
            pipeline.github.close()
        pipeline.db.close()


if __name__ == "__main__":
    raise SystemExit(main())
