"""Record a real PR into the evaluation dataset.

The PR is fetched with the GitHub App or PAT from the environment. Labels are
optional: without them the record is a shadow sample that can be labeled later.

Examples::

    python scripts/record_pr.py --repo pallets/flask --pr 5000 \
        --labels labels/flask-5000.json --out eval/datasets/real.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv


def _load_labels(path: str | None) -> list[dict]:
    if not path:
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("expected") or []
    if not isinstance(data, list):
        raise ValueError("labels file must be a list or {expected: [...]}")
    return data


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    from codereview.config import Settings
    from codereview.evaluation.dataset import DatasetPR, ExpectedFinding, upsert_dataset
    from codereview.github.auth import build_token_provider
    from codereview.github.client import GitHubClient

    parser = argparse.ArgumentParser(description="Record a real PR for evaluation")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", required=True, type=int)
    parser.add_argument("--out", default="eval/datasets/real.jsonl")
    parser.add_argument("--labels", default=None)
    parser.add_argument("--source", default="real")
    parser.add_argument("--installation-id", type=int, default=None)
    parser.add_argument("--label-template", default=None, help="write a labeling template here")
    parser.add_argument(
        "--include-review-comments",
        action="store_true",
        help="store human review comments in meta for labeling context",
    )
    parser.add_argument(
        "--labels-from-comments",
        action="store_true",
        help="treat human review comments as weak expected findings",
    )
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    installation_id = args.installation_id or settings.github_installation_id
    provider = build_token_provider(settings)
    client = GitHubClient(settings, provider)
    try:
        pr = client.get_pull(args.repo, args.pr, installation_id)
        diff = client.get_pull_diff(args.repo, args.pr, installation_id)
        files = client.list_pull_files(args.repo, args.pr, installation_id)
        comments = []
        if args.include_review_comments or args.labels_from_comments:
            comments = client.list_review_comments(args.repo, args.pr, installation_id)
    finally:
        client.close()

    labels = _load_labels(args.labels)
    expected = [ExpectedFinding.from_value(item) for item in labels]
    meta = {}
    if comments:
        meta["review_comments"] = [
            {
                "path": item.get("path"),
                "line": item.get("line") or item.get("original_line"),
                "body": item.get("body") or "",
                "user": (item.get("user") or {}).get("login", ""),
            }
            for item in comments
            if (item.get("body") or "").strip()
        ]
        if args.labels_from_comments and not expected:
            # Weak labels: human comments are not a security ground truth, but
            # they are a real signal and a useful starting point for review.
            expected = [
                ExpectedFinding(
                    issue=item["body"][:500],
                    file=item.get("path"),
                    line=item.get("line"),
                    severity="medium",
                )
                for item in meta["review_comments"]
            ]

    record = DatasetPR(
        id=f"{args.repo}#{args.pr}",
        repo=args.repo,
        number=args.pr,
        title=pr.title,
        body=pr.body,
        diff=diff,
        expected=expected,
        head_sha=pr.head_sha,
        source=args.source,
        labeled=bool(expected),
        meta=meta,
    )
    upsert_dataset(args.out, record)

    if args.label_template:
        template = {
            "repo": args.repo,
            "number": args.pr,
            "head_sha": pr.head_sha,
            "title": pr.title,
            "body": pr.body,
            "changed_files": [
                {
                    "filename": item.get("filename"),
                    "status": item.get("status"),
                    "patch": item.get("patch"),
                }
                for item in files
            ],
            "expected": [],
        }
        Path(args.label_template).parent.mkdir(parents=True, exist_ok=True)
        Path(args.label_template).write_text(
            json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(
        json.dumps(
            {
                "saved": args.out,
                "id": record.id,
                "head_sha": record.head_sha,
                "diff_chars": len(diff),
                "expected": len(expected),
                "label_template": args.label_template,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
