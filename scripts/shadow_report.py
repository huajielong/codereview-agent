"""Export shadow runs for labeling and import labels back.

Shadow mode = ``REVIEW_DRY_RUN=true``. Every run is stored in SQLite together
with a snapshot of the PR diff, so export works without another GitHub call.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dotenv import load_dotenv


def _export(settings, out: str, limit: int) -> int:
    from codereview.evaluation.dataset import DatasetPR, ExpectedFinding, save_dataset
    from codereview.storage.db import Database

    db = Database(settings.db_path)
    try:
        runs = db.list_runs(limit=limit)
        records = []
        seen: set[str] = set()
        missing = 0
        for run in runs:
            record_id = f"{run['repo']}#{run['pr_number']}"
            if record_id in seen:
                continue
            snapshot = db.get_snapshot(run["repo"], run["pr_number"], run["head_sha"])
            if not snapshot or not (snapshot.get("diff") or "").strip():
                missing += 1
                continue
            seen.add(record_id)
            records.append(
                DatasetPR(
                    id=f"{run['repo']}#{run['pr_number']}",
                    repo=run["repo"],
                    number=int(run["pr_number"]),
                    title=snapshot["title"],
                    body=snapshot["body"],
                    diff=snapshot["diff"],
                    expected=[],
                    head_sha=run["head_sha"],
                    source="shadow",
                    labeled=False,
                    meta={
                        "run_id": run["run_id"],
                        "status": run["status"],
                        "posted": bool(run.get("posted")),
                        "review_id": run.get("review_id"),
                        "findings": json.loads(run["findings_json"]),
                    },
                )
            )
        save_dataset(out, records)
    finally:
        db.close()
    print(json.dumps({"exported": len(records), "missing_snapshot": missing, "out": out}))
    return 0


def _import(settings, file: str, dataset_out: str | None) -> int:
    from codereview.evaluation.dataset import load_dataset, save_dataset
    from codereview.storage.db import Database

    prs = load_dataset(file)
    labels = []
    for pr in prs:
        if pr.expected:
            pr.labeled = True
        for item in pr.expected:
            labels.append(
                {
                    "repo": pr.repo,
                    "pr_number": pr.number,
                    "head_sha": pr.head_sha,
                    "file": item.file,
                    "line": item.line,
                    "issue": item.issue,
                    "severity": item.severity,
                    "labeler": "shadow_import",
                }
            )
    db = Database(settings.db_path)
    try:
        inserted = db.add_labels(labels)
    finally:
        db.close()
    if dataset_out:
        save_dataset(dataset_out, prs)
    print(json.dumps({"records": len(prs), "labels": len(labels), "inserted": inserted}))
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    from codereview.config import Settings

    parser = argparse.ArgumentParser(description="Shadow-mode export/import")
    sub = parser.add_subparsers(dest="command", required=True)

    export = sub.add_parser("export")
    export.add_argument("--out", default="eval/datasets/shadow.jsonl")
    export.add_argument("--limit", type=int, default=500)

    imp = sub.add_parser("import-labels")
    imp.add_argument("--file", required=True)
    imp.add_argument("--dataset-out", default=None)

    sub.add_parser("stats")

    args = parser.parse_args(argv)
    settings = Settings.from_env()

    if args.command == "export":
        return _export(settings, args.out, args.limit)
    if args.command == "import-labels":
        return _import(settings, args.file, args.dataset_out)

    from codereview.storage.db import Database

    db = Database(settings.db_path)
    try:
        print(json.dumps(db.stats(), ensure_ascii=False, indent=2))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
