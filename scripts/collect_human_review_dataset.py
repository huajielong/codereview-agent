"""Collect real PRs whose human review comments can serve as expected findings.

This is deliberately conservative:
- only merged PRs with a security word in the title
- only inline review comments on non-test source files
- bots and the PR author are excluded
- comments must mention a security-relevant concept
- diffs above ``--max-diff-chars`` are skipped

The result is a JSONL dataset in the same format as ``eval/datasets/*.jsonl``.
It is **not** a blinded benchmark and the keyword heuristic still catches some
style/docs comments; review the output before using it for metrics. Prefer the
hand-curated ``real_human_labeled.jsonl`` for reported numbers.

    python -I -X utf8 scripts/collect_human_review_dataset.py \
      --repos apache/superset,frappe/frappe,wagtail/wagtail \
      --out eval/datasets/real_human_batch.jsonl
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv

SECURITY_WORDS = (
    "inject", "bypass", "auth", "authoriz", "sql", "xss", "secret", "validate",
    "sanitiz", "unsafe", "security", "escape", "permission", "traversal",
    "redirect", "cve", "leak", "exposure", "csrf", "ssrf", "deserializ",
    "hardcod", "credential", "token", "privilege", "allowlist", "whitelist",
)
CODE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".rb",
    ".php", ".cs", ".rs", ".swift", ".scala", ".sql", ".sh",
}
TEST_DIRS = {"tests", "test", "testing", "spec", "__tests__"}


def _is_code_path(path: str) -> bool:
    from pathlib import Path

    candidate = Path(path)
    if candidate.suffix.lower() not in CODE_EXTENSIONS:
        return False
    if TEST_DIRS.intersection(candidate.parts):
        return False
    name = candidate.name.lower()
    if name.startswith("test_") or name.endswith("_test.py") or name.startswith("test."):
        return False
    return True


def _security_comment(body: str) -> bool:
    lowered = body.lower()
    return any(word in lowered for word in SECURITY_WORDS)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    from codereview.config import Settings
    from codereview.diff.parser import parse_unified_diff
    from codereview.evaluation.dataset import DatasetPR, ExpectedFinding, save_dataset
    from codereview.github.client import GitHubClient

    parser = argparse.ArgumentParser(description="Collect human-review-labeled PRs")
    parser.add_argument(
        "--repos",
        default="apache/superset,frappe/frappe,wagtail/wagtail",
        help="comma-separated owner/repo list",
    )
    parser.add_argument("--limit-per-repo", type=int, default=3)
    parser.add_argument("--max-diff-chars", type=int, default=15000)
    parser.add_argument("--out", default="eval/datasets/real_human_batch.jsonl")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    client = GitHubClient(settings)
    records: list[DatasetPR] = []
    try:
        for repo in [item.strip() for item in args.repos.split(",") if item.strip()]:
            query = f"repo:{repo} is:pr is:merged security in:title comments:>0"
            found = 0
            try:
                items = client.search_issues(query, per_page=max(30, args.limit_per_repo * 10))
            except Exception as exc:  # noqa: BLE001
                print(f"{repo}: search failed: {exc}")
                continue
            for item in items:
                if found >= args.limit_per_repo:
                    break
                number = item.get("number")
                if not number:
                    continue
                pr = client.get_pull(repo, number)
                diff = client.get_pull_diff(repo, number)
                if not diff.strip() or len(diff) > args.max_diff_chars:
                    continue
                code_paths = {
                    file.path for file in parse_unified_diff(diff) if _is_code_path(file.path)
                }
                if not code_paths:
                    continue
                comments = client.list_review_comments(repo, number)
                expected: list[ExpectedFinding] = []
                seen: set[tuple] = set()
                for comment in comments:
                    user = (comment.get("user") or {}).get("login") or ""
                    body = (comment.get("body") or "").strip()
                    path = comment.get("path") or ""
                    if not body or user.endswith("[bot]") or user == pr.author:
                        continue
                    if path not in code_paths:
                        continue
                    if not _security_comment(body):
                        continue
                    key = (path, comment.get("line") or comment.get("original_line"), body[:120])
                    if key in seen:
                        continue
                    seen.add(key)
                    expected.append(
                        ExpectedFinding(
                            issue=body,
                            file=path or None,
                            line=comment.get("line") or comment.get("original_line"),
                            severity="high",
                        )
                    )
                if not expected:
                    continue
                records.append(
                    DatasetPR(
                        id=f"{repo}#{number}",
                        repo=repo,
                        number=number,
                        title=pr.title,
                        body=pr.body,
                        diff=diff,
                        expected=expected,
                        head_sha=pr.head_sha,
                        source="human-review-batch",
                        labeled=True,
                        meta={
                            "label_note": "Human inline review comments, bots/author/tests excluded.",
                            "reviewers": sorted(
                                {
                                    (c.get("user") or {}).get("login", "")
                                    for c in comments
                                }
                            ),
                        },
                    )
                )
                found += 1
                print(f"{repo}#{number}: {len(expected)} expected, diff={len(diff)} chars")
    finally:
        client.close()

    save_dataset(args.out, records)
    print(f"wrote {len(records)} records to {args.out}")
    return 0 if records else 1


if __name__ == "__main__":
    raise SystemExit(main())
