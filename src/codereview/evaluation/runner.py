"""Run a review function over a dataset and aggregate metrics across runs."""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Callable

from codereview.diff.parser import normalize_diff, parse_unified_diff, valid_line_map
from codereview.evaluation.dataset import DatasetPR
from codereview.evaluation.metrics import (
    aggregate,
    issue_metrics,
    line_metrics,
    placement_metrics,
)
from codereview.models import Finding, ReviewOutcome

ReviewFn = Callable[[DatasetPR], object]


def _normalize_result(result) -> tuple[list[Finding], list[str]]:
    if isinstance(result, ReviewOutcome):
        return list(result.findings), list(result.errors)
    if isinstance(result, dict):
        raw = result.get("findings") or []
        errors = result.get("errors") or ([result["error"]] if result.get("error") else [])
        return [
            item if isinstance(item, Finding) else Finding.from_dict(item)
            for item in raw
        ], [str(error) for error in errors]
    if isinstance(result, list):
        return [
            item if isinstance(item, Finding) else Finding.from_dict(item)
            for item in result
        ], []
    raise TypeError(f"unsupported review result: {type(result)!r}")


def evaluate_dataset(
    prs: list[DatasetPR],
    review_fn: ReviewFn,
    *,
    mode: str = "keyword",
    runs: int = 1,
    threshold: float = 0.3,
    embed_fn=None,
    progress: Callable[[int, int, DatasetPR], None] | None = None,
) -> dict:
    if mode not in ("keyword", "hybrid"):
        raise ValueError("mode must be 'keyword' or 'hybrid'")
    if mode == "hybrid" and embed_fn is None:
        raise ValueError("hybrid matching requires embed_fn")
    runs = max(1, runs)

    run_aggregates = []
    last_per_pr = []
    for _ in range(runs):
        per_pr = []
        for index, pr in enumerate(prs, 1):
            if progress:
                progress(index, len(prs), pr)
            error = ""
            try:
                findings, errors = _normalize_result(review_fn(pr))
                error = "; ".join(errors)
            except Exception as exc:  # noqa: BLE001 - evaluation keeps going
                findings, error = [], f"{type(exc).__name__}: {exc}"

            files = parse_unified_diff(pr.diff)
            if not files:
                files = parse_unified_diff(normalize_diff(pr.diff))
            valid_lines = valid_line_map(files)

            per_pr.append(
                {
                    "pr_id": pr.id,
                    "repo": pr.repo,
                    "number": pr.number,
                    "error": error,
                    "issue": issue_metrics(
                        pr.expected,
                        findings,
                        threshold=threshold,
                        embed_fn=embed_fn if mode == "hybrid" else None,
                        labeled=pr.labeled,
                    ),
                    "line": line_metrics(pr.expected, findings, labeled=pr.labeled),
                    "placement": placement_metrics(findings, valid_lines),
                    "n_findings": len(findings),
                }
            )
        run_aggregates.append(aggregate(per_pr))
        last_per_pr = per_pr

    def mean_std(values: list[float]) -> dict:
        if not values:
            return {"mean": 0.0, "std": 0.0}
        return {
            "mean": statistics.fmean(values),
            "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        }

    summary = {
        "issue_f1": mean_std([run["issue"]["f1"] for run in run_aggregates]),
        "issue_precision": mean_std([run["issue"]["precision"] for run in run_aggregates]),
        "issue_recall": mean_std([run["issue"]["recall"] for run in run_aggregates]),
        "line_f1": mean_std([run["line"]["f1"] for run in run_aggregates]),
        "actionable_rate": mean_std(
            [run["placement"]["actionable_rate"] for run in run_aggregates]
        ),
        "errors": sum(run["errors"] for run in run_aggregates),
    }

    return {
        "mode": mode,
        "runs": runs,
        "dataset_size": len(prs),
        "summary": summary,
        "run_metrics": run_aggregates,
        "per_pr": last_per_pr,
    }


def evaluate_with_pipeline(
    pipeline,
    prs: list[DatasetPR],
    *,
    runs: int = 1,
    mode: str = "keyword",
    threshold: float = 0.3,
    embed_fn=None,
    progress: Callable[[int, int, DatasetPR], None] | None = None,
) -> dict:
    if embed_fn is None and mode == "hybrid":
        embeddings = getattr(pipeline.retriever, "embeddings", None)
        if embeddings is None:
            # Hybrid *matching* does not require context retrieval. Build a
            # standalone embedding service when --no-context disabled the
            # retriever.
            from codereview.context.embedding_cache import build_embedding_service

            embeddings = build_embedding_service(pipeline.settings, pipeline.db)
        if embeddings is None:
            raise ValueError(
                "hybrid matching requires an embedding channel; configure Cohere or OPENAI_API_KEY"
            )
        embed_fn = embeddings.embed_query

    def review_fn(pr: DatasetPR):
        return pipeline.review_diff(
            repo=pr.repo,
            number=pr.number,
            title=pr.title,
            body=pr.body,
            diff=pr.diff,
            head_sha=pr.head_sha,
            dry_run=True,
            workspace_root=pr.meta.get("workspace"),
        )

    report = evaluate_dataset(
        prs,
        review_fn,
        mode=mode,
        runs=runs,
        threshold=threshold,
        embed_fn=embed_fn,
        progress=progress,
    )
    report["config"] = {
        "model": pipeline.settings.model,
        "review_mode": pipeline.settings.mode,
        "prompt_sha": pipeline.reviewer.prompt_fingerprint(),
        "policy_sha": pipeline.policy.fingerprint() if pipeline.policy else "",
        "max_tokens": pipeline.settings.max_tokens,
    }
    return report


def save_report(path: str | Path, report: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=float),
        encoding="utf-8",
    )


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Code Review Evaluation",
        "",
        f"- Dataset: {report['dataset_size']} PRs",
        f"- Runs: {report['runs']}",
        f"- Matching: {report['mode']}",
        "",
        "| Metric | Mean | Std |",
        "|---|---|---|",
        f"| Issue F1 | {summary['issue_f1']['mean']:.4f} | {summary['issue_f1']['std']:.4f} |",
        f"| Issue Precision | {summary['issue_precision']['mean']:.4f} | {summary['issue_precision']['std']:.4f} |",
        f"| Issue Recall | {summary['issue_recall']['mean']:.4f} | {summary['issue_recall']['std']:.4f} |",
        f"| Line F1 | {summary['line_f1']['mean']:.4f} | {summary['line_f1']['std']:.4f} |",
        f"| Actionable rate | {summary['actionable_rate']['mean']:.4f} | {summary['actionable_rate']['std']:.4f} |",
        "",
        f"Errors: {summary['errors']}",
    ]
    if report.get("config"):
        lines.extend(["", "## Config", "", "```json", json.dumps(report["config"], ensure_ascii=False, indent=2), "```"])
    return "\n".join(lines) + "\n"
