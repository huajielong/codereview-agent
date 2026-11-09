"""Build and post GitHub review comments with idempotency.

Idempotency has two layers:
1. a hidden marker in the review body, checked against existing GitHub reviews
2. a SQLite unique key ``(repo, pr, head_sha, fingerprint)``

The fingerprint covers the findings and the review configuration, so changing a
prompt or policy produces a new review instead of silently reusing the old one.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from codereview.config import Settings
from codereview.errors import GitHubAPIError
from codereview.logging_utils import get_logger
from codereview.models import Finding, PublishResult
from codereview.review.paths import normalize_path, resolve_path
from codereview.storage.db import Database
from codereview.text_utils import truncate

log = get_logger(__name__)
MARKER_PREFIX = "<!-- codereview:"


@dataclass
class CommentPlan:
    inline: list[dict] = field(default_factory=list)
    summary: str = ""
    dropped: list[Finding] = field(default_factory=list)
    fingerprint: str = ""


def fingerprint_findings(
    repo: str,
    number: int,
    head_sha: str,
    mode: str,
    findings: list[Finding],
    config_hash: str = "",
) -> str:
    payload = {
        "repo": repo,
        "number": number,
        "head_sha": head_sha,
        "mode": mode,
        "config": config_hash,
        "findings": [
            {
                "issue": finding.issue,
                "severity": finding.severity,
                "file": normalize_path(finding.file),
                "line": finding.line,
            }
            for finding in sorted(findings, key=lambda item: item.key())
        ],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _location(finding: Finding) -> str:
    if finding.file and finding.line:
        return f"`{normalize_path(finding.file)}:{finding.line}`"
    if finding.file:
        return f"`{normalize_path(finding.file)}`"
    return "—"


def _inline_body(finding: Finding) -> str:
    body = f"**{finding.severity.upper()}** — {truncate(finding.issue, 2000)}"
    if finding.evidence:
        evidence = finding.evidence.replace("`", "\\`")
        body += f"\n\nEvidence: `{truncate(evidence, 500)}`"
    return body


def _summary_body(
    findings: list[Finding], fingerprint: str, mode: str
) -> str:
    marker = f"{MARKER_PREFIX}{fingerprint} -->"
    if not findings:
        return f"## AI Code Review\n\nNo security issues found in the changed lines.\n\n{marker}"

    counts = {"high": 0, "medium": 0, "low": 0}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
    lines = [
        "## AI Code Review",
        "",
        f"Found **{len(findings)}** issue(s): "
        f"**{counts.get('high', 0)}** high, "
        f"**{counts.get('medium', 0)}** medium, "
        f"**{counts.get('low', 0)}** low. "
        f"(mode: `{mode}`)",
        "",
        "| Severity | Location | Issue |",
        "|---|---|---|",
    ]
    for finding in findings:
        issue = truncate(finding.issue.replace("|", "\\|").replace("\n", " "), 300)
        lines.append(f"| {finding.severity.upper()} | {_location(finding)} | {issue} |")
    # Keep the marker outside the truncation: it is the idempotency key.
    body = truncate("\n".join(lines), 59_900)
    return body + "\n\n" + marker


def build_comment_plan(
    findings: list[Finding],
    valid_lines: dict[str, set[int]],
    *,
    repo: str,
    number: int,
    head_sha: str,
    mode: str,
    config_hash: str = "",
    max_inline: int = 20,
) -> CommentPlan:
    fingerprint = fingerprint_findings(repo, number, head_sha, mode, findings, config_hash)
    ordered = sorted(
        findings,
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}.get(item.severity, 3),
            item.file or "",
            item.line or 0,
        ),
    )

    inline: list[dict] = []
    dropped: list[Finding] = []
    for finding in ordered:
        path = resolve_path(finding.file, valid_lines)
        can_inline = bool(
            path
            and finding.line
            and finding.line in valid_lines.get(path, set())
            and len(inline) < max_inline
        )
        if can_inline:
            inline.append(
                {
                    "path": path,
                    "line": finding.line,
                    "side": finding.side if finding.side in ("LEFT", "RIGHT") else "RIGHT",
                    "body": _inline_body(finding),
                }
            )
        else:
            dropped.append(finding)

    return CommentPlan(
        inline=inline,
        summary=_summary_body(ordered, fingerprint, mode),
        dropped=dropped,
        fingerprint=fingerprint,
    )


class Publisher:
    def __init__(self, settings: Settings, github, db: Database):
        self.settings = settings
        self.github = github
        self.db = db

    def publish(
        self,
        *,
        repo: str,
        number: int,
        head_sha: str,
        findings: list[Finding],
        valid_lines: dict[str, set[int]],
        mode: str,
        config_hash: str = "",
        installation_id: int | None = None,
        dry_run: bool | None = None,
    ) -> PublishResult:
        dry_run = self.settings.dry_run if dry_run is None else dry_run
        plan = build_comment_plan(
            findings,
            valid_lines,
            repo=repo,
            number=number,
            head_sha=head_sha,
            mode=mode,
            config_hash=config_hash,
            max_inline=self.settings.max_inline_comments,
        )

        if not findings and not self.settings.post_empty_summary:
            return PublishResult(
                posted=False,
                inline_comments=plan.inline,
                summary_body=plan.summary,
                skipped_reason="no findings",
            )

        if self.db.is_posted(repo, number, head_sha, plan.fingerprint):
            return PublishResult(
                posted=False,
                inline_comments=plan.inline,
                summary_body=plan.summary,
                skipped_reason="already posted (database)",
            )

        if dry_run:
            return PublishResult(
                posted=False,
                inline_comments=plan.inline,
                summary_body=plan.summary,
                skipped_reason="dry-run",
            )

        marker = f"{MARKER_PREFIX}{plan.fingerprint} -->"
        try:
            reviews = self.github.list_reviews(repo, number, installation_id)
        except Exception as exc:  # noqa: BLE001
            return PublishResult(
                posted=False,
                inline_comments=plan.inline,
                summary_body=plan.summary,
                errors=[f"cannot check existing reviews, refusing to post: {exc}"],
            )

        for review in reviews:
            if marker in (review.get("body") or ""):
                self.db.mark_posted(
                    repo, number, head_sha, plan.fingerprint, review.get("id")
                )
                return PublishResult(
                    posted=False,
                    inline_comments=plan.inline,
                    summary_body=plan.summary,
                    skipped_reason="already posted (GitHub marker)",
                )

        body = plan.summary
        comments = plan.inline
        try:
            payload = self.github.create_review(
                repo,
                number,
                commit_id=head_sha,
                body=body,
                comments=comments,
                event="COMMENT",
                installation_id=installation_id,
            )
        except GitHubAPIError as exc:
            # A line can become invalid if the PR changed between fetch and post.
            if exc.status == 422 and comments:
                log.warning("review_post_422_fallback_to_summary", extra={"repo": repo, "pr": number})
                body = truncate(
                    body.replace(
                        marker,
                        "_Inline comments were dropped because GitHub rejected their positions._\n\n"
                        + marker,
                    ),
                    60_000,
                )
                try:
                    payload = self.github.create_review(
                        repo,
                        number,
                        commit_id=head_sha,
                        body=body,
                        comments=[],
                        event="COMMENT",
                        installation_id=installation_id,
                    )
                    comments = []
                except Exception as retry_exc:  # noqa: BLE001
                    return PublishResult(
                        posted=False,
                        inline_comments=plan.inline,
                        summary_body=body,
                        errors=[f"post failed: {retry_exc}"],
                    )
            else:
                return PublishResult(
                    posted=False,
                    inline_comments=plan.inline,
                    summary_body=body,
                    errors=[f"post failed: {exc}"],
                )
        except Exception as exc:  # noqa: BLE001
            return PublishResult(
                posted=False,
                inline_comments=plan.inline,
                summary_body=body,
                errors=[f"post failed: {exc}"],
            )

        review_id = payload.get("id") if isinstance(payload, dict) else None
        self.db.mark_posted(repo, number, head_sha, plan.fingerprint, review_id)
        return PublishResult(
            posted=True,
            inline_comments=comments,
            summary_body=body,
            review_id=review_id,
        )
