"""Evaluate the pipeline on a recorded dataset (always dry-run).

Examples::

    python scripts/evaluate.py --dataset eval/datasets/real.jsonl \
        --runs 3 --match keyword --out eval/reports/real.json
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    from codereview.config import Settings
    from codereview.evaluation.dataset import dataset_stats, load_dataset
    from codereview.evaluation.runner import evaluate_with_pipeline, render_markdown, save_report
    from codereview.review.factory import build_pipeline

    parser = argparse.ArgumentParser(description="Evaluate the review pipeline")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--out", default="eval/reports/report.json")
    parser.add_argument("--markdown", default="eval/reports/report.md")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--match", choices=["keyword", "hybrid"], default="keyword")
    parser.add_argument("--threshold", type=float, default=0.3)
    parser.add_argument("--review-mode", choices=["general", "security", "pattern", "ensemble"])
    parser.add_argument("--no-context", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.review_mode:
        settings = replace(settings, mode=args.review_mode)
    if args.no_context:
        settings = replace(settings, context_enabled=False)

    prs = load_dataset(args.dataset)
    pipeline = build_pipeline(settings)
    try:
        report = evaluate_with_pipeline(
            pipeline,
            prs,
            runs=args.runs,
            mode=args.match,
            threshold=args.threshold,
            progress=lambda index, total, pr: print(
                f"[{index}/{total}] {pr.id}", file=sys.stderr
            ),
        )
    finally:
        if getattr(pipeline, "github", None) is not None:
            pipeline.github.close()
        pipeline.db.close()

    report["dataset"] = dataset_stats(prs)
    save_report(args.out, report)
    markdown = render_markdown(report)
    Path(args.markdown).parent.mkdir(parents=True, exist_ok=True)
    Path(args.markdown).write_text(markdown, encoding="utf-8")
    print(markdown)
    return 1 if report["summary"]["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
