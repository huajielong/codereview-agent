"""Review one PR from the command line.

Default is dry-run: the review is computed and stored, but nothing is posted.
Use ``--post`` only after shadow validation.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    from codereview.config import Settings
    from codereview.review.factory import build_pipeline

    parser = argparse.ArgumentParser(description="Review a GitHub PR once")
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--installation-id", type=int, default=None)
    parser.add_argument("--head-sha", default=None)
    parser.add_argument("--mode", choices=["general", "security", "pattern", "ensemble"])
    parser.add_argument("--dry-run", action="store_true", help="force dry-run")
    parser.add_argument("--post", action="store_true", help="post the review")
    parser.add_argument("--force", action="store_true", help="ignore the review cache")
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.mode:
        settings = replace(settings, mode=args.mode)
    if args.post:
        settings = replace(settings, dry_run=False)
    if args.dry_run:
        settings = replace(settings, dry_run=True)

    pipeline = build_pipeline(settings)
    installation_id = args.installation_id or settings.github_installation_id
    try:
        outcome = pipeline.review_pr(
            args.repo,
            args.pr,
            installation_id,
            head_sha=args.head_sha,
            force=args.force,
        )
    finally:
        if getattr(pipeline, "github", None) is not None:
            pipeline.github.close()
        pipeline.db.close()

    payload = outcome.to_dict()
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if outcome.status != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
