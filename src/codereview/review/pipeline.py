"""End-to-end review orchestration."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from codereview.config import Settings
from codereview.context.languages import language_for, languages_for_paths, render_language_section
from codereview.diff.chunker import chunk_diff, render_chunk
from codereview.diff.parser import (
    changed_paths,
    normalize_diff,
    parse_unified_diff,
    valid_line_map,
)
from codereview.llm.reviewers import Reviewer
from codereview.logging_utils import get_logger
from codereview.models import Finding, PRInfo, ReviewOutcome, severity_at_least
from codereview.review.dependency import (
    DEPENDENCY_LANGUAGES,
    render_changed_dependency_section,
)
from codereview.review.history import HistoryStore, render_history_section
from codereview.review.policy import PolicySet, append_audit, render_policy_section
from codereview.review.publisher import Publisher
from codereview.storage.db import Database
from codereview.text_utils import overlap_ratio, truncate

log = get_logger(__name__)


def _cache_key(
    repo: str,
    number: int,
    head_sha: str,
    mode: str,
    settings: Settings,
    prompt_sha: str,
    policy_sha: str,
) -> str:
    payload = {
        "repo": repo,
        "number": number,
        "head_sha": head_sha,
        "mode": mode,
        "config": settings.fingerprint(),
        "prompt": prompt_sha,
        "policy": policy_sha,
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Keep the highest-severity copy of near-identical findings."""
    ordered = sorted(
        findings,
        key=lambda item: (
            {"high": 0, "medium": 1, "low": 2}.get(item.severity, 3),
            item.file or "",
            item.line or 0,
        ),
    )
    result: list[Finding] = []
    for finding in ordered:
        duplicate = None
        for existing in result:
            if finding.key() == existing.key() or overlap_ratio(finding.issue, existing.issue) > 0.6:
                duplicate = existing
                break
        if duplicate is None:
            result.append(finding)
        elif not (duplicate.file and duplicate.line) and (finding.file and finding.line):
            result[result.index(duplicate)] = finding
    return result


class ReviewPipeline:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        github,
        reviewer: Reviewer,
        publisher: Publisher,
        *,
        indexer=None,
        retriever=None,
        workspace=None,
        policy: PolicySet | None = None,
        history: HistoryStore | None = None,
    ):
        self.settings = settings
        self.db = db
        self.github = github
        self.reviewer = reviewer
        self.publisher = publisher
        self.indexer = indexer
        self.retriever = retriever
        self.workspace = workspace
        self.policy = policy
        self.history = history
        self.config_hash = settings.fingerprint()

    # --- public entry points ----------------------------------------------

    def review_pr(
        self,
        repo: str,
        number: int,
        installation_id: int | None = None,
        *,
        head_sha: str | None = None,
        trigger: str = "manual",
        dry_run: bool | None = None,
        force: bool = False,
    ) -> ReviewOutcome:
        run_id = uuid.uuid4().hex
        started = time.monotonic()
        dry = self.settings.dry_run if dry_run is None else dry_run
        outcome = ReviewOutcome(
            run_id=run_id,
            repo=repo,
            number=number,
            head_sha=head_sha or "",
            mode=self.settings.mode,
            dry_run=dry,
        )

        try:
            pr = self.github.get_pull(repo, number, installation_id)
        except Exception as exc:  # noqa: BLE001
            outcome.status = "failed"
            outcome.errors.append(f"fetch PR failed: {exc}")
            return self._finish(outcome, started)

        if head_sha and pr.head_sha and head_sha != pr.head_sha:
            outcome.status = "skipped"
            outcome.errors.append(
                f"stale event: webhook head {head_sha} != current head {pr.head_sha}"
            )
            return self._finish(outcome, started)

        outcome.head_sha = pr.head_sha
        if pr.state != "open" or pr.merged:
            outcome.status = "skipped"
            outcome.errors.append("PR is not open")
            return self._finish(outcome, started)
        if pr.draft and not self.settings.review_drafts:
            outcome.status = "skipped"
            outcome.errors.append("draft PR")
            return self._finish(outcome, started)

        prompt_sha = self.reviewer.prompt_fingerprint(repo)
        policy_sha = self.policy.fingerprint() if self.policy else ""
        cache_key = _cache_key(
            repo, number, pr.head_sha, self.settings.mode,
            self.settings, prompt_sha, policy_sha,
        )

        if self.settings.cache_enabled and not force and pr.head_sha:
            cached = self.db.get_review_cache(cache_key)
            if cached is not None:
                cached.run_id = run_id
                cached.cache_hit = True
                cached.dry_run = dry
                log.info("review_cache_hit", extra={"repo": repo, "pr": number})
                return self._finish(cached, started)

        try:
            diff = self.github.get_pull_diff(repo, number, installation_id)
        except Exception as exc:  # noqa: BLE001
            outcome.status = "failed"
            outcome.errors.append(f"fetch diff failed: {exc}")
            return self._finish(outcome, started)

        return self._review(
            pr,
            diff,
            run_id=run_id,
            started=started,
            installation_id=installation_id,
            dry_run=dry,
            cache_key=cache_key if not dry else None,
        )

    def review_diff(
        self,
        *,
        repo: str,
        number: int,
        title: str,
        body: str,
        diff: str,
        head_sha: str = "",
        installation_id: int | None = None,
        dry_run: bool = True,
        workspace_root: str | Path | None = None,
    ) -> ReviewOutcome:
        """Review a diff without calling GitHub (used by evaluation)."""
        pr = PRInfo(
            repo=repo,
            number=number,
            title=title,
            body=body,
            author="",
            base_sha="",
            head_sha=head_sha,
            state="open",
            draft=False,
            merged=False,
        )
        return self._review(
            pr,
            diff,
            run_id=uuid.uuid4().hex,
            started=time.monotonic(),
            installation_id=installation_id,
            dry_run=dry_run,
            cache_key=None,
            workspace_root=workspace_root,
        )

    def review_event(self, event, *, dry_run: bool | None = None) -> ReviewOutcome:
        return self.review_pr(
            event.repo,
            event.number,
            event.installation_id,
            head_sha=event.head_sha,
            trigger="webhook",
            dry_run=dry_run,
        )

    # --- core --------------------------------------------------------------

    def _review(
        self,
        pr: PRInfo,
        diff: str,
        *,
        run_id: str,
        started: float,
        installation_id: int | None,
        dry_run: bool,
        cache_key: str | None,
        workspace_root: str | Path | None = None,
    ) -> ReviewOutcome:
        outcome = ReviewOutcome(
            run_id=run_id,
            repo=pr.repo,
            number=pr.number,
            head_sha=pr.head_sha,
            mode=self.settings.mode,
            dry_run=dry_run,
        )
        try:
            if not diff.strip():
                outcome.status = "skipped"
                outcome.errors.append("empty diff")
                return outcome

            files = parse_unified_diff(diff)
            if not files:
                normalized = normalize_diff(diff)
                files = parse_unified_diff(normalized)
                diff = normalized
            if not files:
                outcome.status = "failed"
                outcome.errors.append("unparseable diff")
                return outcome

            valid_lines = valid_line_map(files)
            paths = changed_paths(files)
            try:
                self.db.save_snapshot(
                    pr.repo, pr.number, pr.head_sha, pr.title, pr.body, diff
                )
            except Exception:  # noqa: BLE001 - snapshot is best-effort
                log.warning(
                    "save_snapshot_failed", extra={"repo": pr.repo, "pr": pr.number}
                )

            context, context_errors, index_stats = self._build_context(
                pr, diff, paths, installation_id, workspace_root
            )
            outcome.errors.extend(context_errors)
            outcome.index_stats = index_stats.to_dict() if index_stats else None

            chunks = chunk_diff(files, self.settings.max_diff_chars)
            outcome.chunks = len(chunks)
            findings: list[Finding] = []
            llm_errors: list[str] = []
            for index, chunk in enumerate(chunks, 1):
                call = self.reviewer.review(
                    {
                        "id": f"{pr.repo}#{pr.number}:{index}",
                        "title": pr.title,
                        "diff": render_chunk(chunk),
                    },
                    context=context,
                    task_context=pr.body,
                    mode=self.settings.mode,
                    repo=pr.repo,
                )
                findings.extend(call.findings)
                llm_errors.extend(call.errors)
                outcome.errors.extend(call.errors)
                outcome.llm_calls += call.llm_calls
                outcome.prompt_tokens += call.prompt_tokens
                outcome.completion_tokens += call.completion_tokens

            findings = deduplicate_findings(findings)
            findings = [
                finding
                for finding in findings
                if severity_at_least(finding.severity, self.settings.min_severity)
            ]
            findings.sort(
                key=lambda item: (
                    {"high": 0, "medium": 1, "low": 2}.get(item.severity, 3),
                    item.file or "",
                    item.line or 0,
                )
            )
            findings = findings[: self.settings.max_findings]
            outcome.findings = findings

            # Fail closed on partial LLM failures: posting half a review is
            # worse than posting nothing, because the missing chunk looks clean.
            if llm_errors and not self.settings.post_on_partial:
                outcome.status = "completed_with_errors"
                outcome.errors.append("review not posted: partial LLM failure")
                return outcome

            publish = self.publisher.publish(
                repo=pr.repo,
                number=pr.number,
                head_sha=pr.head_sha,
                findings=findings,
                valid_lines=valid_lines,
                mode=self.settings.mode,
                config_hash=self.config_hash,
                installation_id=installation_id,
                dry_run=dry_run,
            )
            outcome.summary_body = publish.summary_body
            outcome.inline_comments = publish.inline_comments
            outcome.posted = publish.posted
            outcome.review_id = publish.review_id
            outcome.errors.extend(publish.errors)

            outcome.status = "completed" if not outcome.errors else "completed_with_errors"
            if (
                self.settings.history_enabled
                and not dry_run
                and pr.number > 0
                and outcome.status == "completed"
            ):
                self._record_history(pr, findings)
            if cache_key and not dry_run and not outcome.errors and pr.head_sha:
                self.db.set_review_cache(cache_key, outcome)
            return outcome
        except Exception as exc:  # noqa: BLE001 - never crash a worker
            log.exception("review_failed", extra={"repo": pr.repo, "pr": pr.number})
            outcome.status = "failed"
            outcome.errors.append(f"review failed: {exc}")
            return outcome
        finally:
            self._finish(outcome, started)

    def _build_context(
        self,
        pr: PRInfo,
        diff: str,
        paths: list[str],
        installation_id: int | None,
        workspace_root: str | Path | None,
    ):
        sections: list[str] = []
        errors: list[str] = []
        stats = None
        dependency_chars = 0
        history_chars = 0
        languages = languages_for_paths(paths)

        language_section = render_language_section(paths)
        if language_section:
            sections.append(language_section)

        embeddings_ready = bool(
            self.settings.context_enabled
            and self.indexer is not None
            and self.retriever is not None
            and getattr(self.indexer, "embeddings", None) is not None
        )
        dependency_ready = bool(
            self.settings.dependency_enabled
            and any(language_for(path) in DEPENDENCY_LANGUAGES for path in paths)
        )

        root = workspace_root
        if (
            (embeddings_ready or dependency_ready)
            and root is None
            and pr.head_sha
            and self.workspace is not None
        ):
            try:
                root = self.workspace.ensure(pr.repo, pr.head_sha, installation_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"workspace: {exc}")

        if embeddings_ready and root is not None:
            ref = pr.head_sha or "local"
            try:
                stats = self.indexer.ensure_index(pr.repo, ref, root)
                context = self.retriever.context_for_diff(
                    pr.repo,
                    ref,
                    pr.title,
                    diff,
                    k=self.settings.context_top_k,
                    exclude_paths=set(paths),
                    languages=set(languages),
                )
                if context:
                    sections.append(context)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"index: {exc}")

        if dependency_ready and root is not None:
            try:
                section = render_changed_dependency_section(
                    root, paths, repo_name=pr.repo
                )
                if section:
                    sections.append(section)
                    dependency_chars = len(section)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"dependency: {exc}")

        if self.settings.policy_enabled and self.policy is not None:
            try:
                section = render_policy_section(self.policy, paths)
                if section:
                    sections.append(section)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"policy: {exc}")

        if self.settings.history_enabled and self.history is not None:
            try:
                records = self.history.search(f"{pr.title}\n{diff[:2000]}", limit=5)
                section = render_history_section(records)
                if section:
                    sections.append(section)
                    history_chars = len(section)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"history: {exc}")

        log.info(
            "review_context_built",
            extra={
                "repo": pr.repo,
                "pr": pr.number,
                "languages": languages,
                "sections": len(sections),
                "context_chars": sum(len(section) for section in sections),
                "dependency_chars": dependency_chars,
                "history_chars": history_chars,
                "workspace_ready": root is not None,
            },
        )
        return "\n\n".join(sections), errors, stats

    def _record_history(self, pr: PRInfo, findings: list[Finding]) -> None:
        """Persist posted findings so later reviews can retrieve past context."""
        if self.history is None or not findings:
            return
        created_at = datetime.now(timezone.utc).isoformat()
        records = [
            {
                "repo": pr.repo,
                "pr": pr.number,
                "head_sha": pr.head_sha,
                "mode": self.settings.mode,
                "issue": finding.issue,
                "file": finding.file or "",
                "line": finding.line or 0,
                "severity": finding.severity,
                "source_agent": finding.source_agent or "",
                "created_at": created_at,
            }
            for finding in findings
        ]
        try:
            added = self.history.add_many(records)
            log.info(
                "history_recorded",
                extra={"repo": pr.repo, "pr": pr.number, "added": added},
            )
        except Exception:  # noqa: BLE001 - history is best-effort
            log.warning(
                "history_write_failed", extra={"repo": pr.repo, "pr": pr.number}
            )

    def _finish(self, outcome: ReviewOutcome, started: float) -> ReviewOutcome:
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        outcome.errors = [truncate(error, 2000) for error in outcome.errors]
        try:
            self.db.save_run(outcome, self.config_hash)
        except Exception:  # noqa: BLE001 - storage failure must not hide the result
            log.exception("save_run_failed", extra={"run_id": outcome.run_id})
        if self.settings.audit_path:
            try:
                append_audit(
                    self.settings.audit_path,
                    {
                        "run_id": outcome.run_id,
                        "repo": outcome.repo,
                        "pr": outcome.number,
                        "head_sha": outcome.head_sha,
                        "mode": outcome.mode,
                        "status": outcome.status,
                        "dry_run": outcome.dry_run,
                        "posted": outcome.posted,
                        "findings": len(outcome.findings),
                        "llm_calls": outcome.llm_calls,
                        "errors": outcome.errors,
                    },
                )
            except Exception:  # noqa: BLE001 - audit is best-effort
                log.warning("audit_write_failed", extra={"run_id": outcome.run_id})
        return outcome
