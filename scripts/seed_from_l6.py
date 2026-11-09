"""Seed the evaluation dataset with L6's 15 synthetic fixtures.

These are useful for smoke tests and metric plumbing, not as evidence of real
performance. Records are marked ``source=synthetic-l6``.
"""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _load_sample_prs(path: Path) -> dict:
    spec = importlib.util.spec_from_file_location("l6_sample_prs", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SAMPLE_PRS


def main(argv: list[str] | None = None) -> int:
    from codereview.diff.parser import normalize_diff
    from codereview.evaluation.dataset import DatasetPR, ExpectedFinding, save_dataset

    parser = argparse.ArgumentParser(description="Import L6 fixtures as a dataset")
    parser.add_argument(
        "--l6-path",
        default="../L6/src/data/sample_prs.py",
        help="path to L6 src/data/sample_prs.py",
    )
    parser.add_argument("--out", default="eval/datasets/l6_fixtures.jsonl")
    args = parser.parse_args(argv)

    sample = _load_sample_prs(Path(args.l6_path))
    records = []
    for index, (pr_id, pr) in enumerate(sample.items(), 1):
        diff = normalize_diff(pr.get("diff", ""), "change.py")
        records.append(
            DatasetPR(
                id=f"l6:{pr_id}",
                repo="l6-fixture",
                number=index,
                title=pr.get("title", ""),
                body=pr.get("task_context", ""),
                diff=diff,
                expected=[
                    ExpectedFinding(issue=str(issue))
                    for issue in pr.get("expected_issues", [])
                ],
                source="synthetic-l6",
                labeled=True,
                meta={"task_context": pr.get("task_context", "")},
            )
        )
    save_dataset(args.out, records)
    print(f"wrote {len(records)} records to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
