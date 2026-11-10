"""Seed a small Python + TypeScript + C/C++ review dataset.

The workspace is written under ``eval/workspaces/multilang`` and the dataset
under ``eval/datasets/multilang.jsonl``. This is a synthetic smoke/regression
set, not a benchmark of real-world accuracy.

    python -I -X utf8 scripts/seed_multilang_eval.py
"""
from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from codereview.diff.parser import normalize_diff, parse_unified_diff
from codereview.evaluation.dataset import DatasetPR, ExpectedFinding, save_dataset

WORKSPACE = ROOT / "eval" / "workspaces" / "multilang"
DATASET = ROOT / "eval" / "datasets" / "multilang.jsonl"

SOURCES = json.loads(
    (ROOT / "eval" / "fixtures" / "multilang_files.json").read_text(
        encoding="utf-8"
    )
)
SAFE_DB = SOURCES["SAFE_DB"]
VULN_DB = SOURCES["VULN_DB"]
SAFE_RENDER = SOURCES["SAFE_RENDER"]
VULN_RENDER = SOURCES["VULN_RENDER"]
SAFE_BUFFER = SOURCES["SAFE_BUFFER"]
VULN_BUFFER = SOURCES["VULN_BUFFER"]
RUNNER = SOURCES["RUNNER"]
AUTH = SOURCES["AUTH"]
LOG_CPP = SOURCES["LOG_CPP"]
FILES = SOURCES["FILES"]


def _diff(path: str, before: str, after: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    ) + "\n"


def _changes(*items):
    return {path: (before, after) for path, before, after in items}


PRS = [
    {
        "id": "multilang#1",
        "number": 1,
        "title": "Use an f-string in the user query",
        "body": "Simplify the lookup.",
        "changes": _changes(("backend/db.py", SAFE_DB, VULN_DB)),
        "expected": [
            {
                "file": "backend/db.py",
                "line": 6,
                "issue": "SQL injection via f-string in query_user",
                "severity": "high",
            }
        ],
    },
    {
        "id": "multilang#2",
        "number": 2,
        "title": "Add a command runner",
        "body": "Run user-provided commands.",
        "changes": _changes(("backend/runner.py", "", RUNNER)),
        "expected": [
            {
                "file": "backend/runner.py",
                "line": 4,
                "issue": "command injection via subprocess shell=True",
                "severity": "high",
            }
        ],
    },
    {
        "id": "multilang#3",
        "number": 3,
        "title": "Render the user name as HTML",
        "body": "Show bold names.",
        "changes": _changes(("web/src/render.ts", SAFE_RENDER, VULN_RENDER)),
        "expected": [
            {
                "file": "web/src/render.ts",
                "line": 5,
                "issue": "XSS via innerHTML in renderUser",
                "severity": "high",
            }
        ],
    },
    {
        "id": "multilang#4",
        "number": 4,
        "title": "Add frontend auth helpers",
        "body": "Parse auth input.",
        "changes": _changes(("web/src/auth.ts", "", AUTH)),
        "expected": [
            {
                "file": "web/src/auth.ts",
                "line": 1,
                "issue": "hardcoded API token in frontend",
                "severity": "high",
            },
            {
                "file": "web/src/auth.ts",
                "line": 4,
                "issue": "eval injection of untrusted input",
                "severity": "high",
            },
        ],
    },
    {
        "id": "multilang#5",
        "number": 5,
        "title": "Simplify the buffer copy helper",
        "body": "Use the standard copy call.",
        "changes": _changes(
            ("native/include/buffer.h", SAFE_BUFFER, VULN_BUFFER)
        ),
        "expected": [
            {
                "file": "native/include/buffer.h",
                "line": 5,
                "issue": "buffer overflow via strcpy in copy_name",
                "severity": "high",
            }
        ],
    },
    {
        "id": "multilang#6",
        "number": 6,
        "title": "Add a logging helper",
        "body": "Log messages to stdout.",
        "changes": _changes(("native/src/log.cpp", "", LOG_CPP)),
        "expected": [
            {
                "file": "native/src/log.cpp",
                "line": 4,
                "issue": "format string vulnerability in printf",
                "severity": "high",
            }
        ],
    },
    {
        "id": "multilang#7",
        "number": 7,
        "title": "Mixed backend, frontend and native change",
        "body": "Touch all three stacks.",
        "changes": _changes(
            ("backend/db.py", SAFE_DB, VULN_DB),
            ("web/src/render.ts", SAFE_RENDER, VULN_RENDER),
            ("native/include/buffer.h", SAFE_BUFFER, VULN_BUFFER),
        ),
        "expected": [
            {
                "file": "backend/db.py",
                "line": 6,
                "issue": "SQL injection via f-string in query_user",
                "severity": "high",
            },
            {
                "file": "web/src/render.ts",
                "line": 5,
                "issue": "XSS via innerHTML in renderUser",
                "severity": "high",
            },
            {
                "file": "native/include/buffer.h",
                "line": 5,
                "issue": "buffer overflow via strcpy in copy_name",
                "severity": "high",
            },
        ],
    },
]


def main() -> int:
    for relative, source in FILES.items():
        path = WORKSPACE / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    DATASET.parent.mkdir(parents=True, exist_ok=True)
    records = []
    for pr in PRS:
        parts = [
            _diff(path, before, after)
            for path, (before, after) in pr["changes"].items()
        ]
        diff = "".join(parts)
        parsed = parse_unified_diff(diff) or parse_unified_diff(normalize_diff(diff))
        if not parsed:
            raise SystemExit(f"{pr['id']}: generated diff did not parse")
        records.append(
            DatasetPR(
                id=pr["id"],
                repo="fixture/multilang",
                number=pr["number"],
                head_sha="fixture",
                title=pr["title"],
                body=pr["body"],
                diff=diff,
                expected=[
                    ExpectedFinding.from_value(item) for item in pr["expected"]
                ],
                source="synthetic-multilang",
                labeled=True,
                meta={"workspace": "eval/workspaces/multilang"},
            )
        )

    save_dataset(DATASET, records)
    print(f"wrote {len(records)} records to {DATASET}")
    print(f"workspace: {WORKSPACE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
