"""Deterministic edge-level evaluation of the multi-language dependency graph.

Builds a small Python + C/C++ + JS/TS fixture, runs
``render_changed_dependency_section`` for several changed-file sets, and
compares the (changed file, related file) edges with the expected graph.

This does not call an LLM; it measures the dependency layer itself.

    python -I -X utf8 scripts/evaluate_multilang.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codereview.context.index import RepoIndexer
from codereview.context.languages import languages_for_paths
from codereview.context.retriever import ContextRetriever
from codereview.context.store import MemoryVectorStore
from codereview.review.dependency import render_changed_dependency_section
from codereview.storage.db import Database

FILES = {
    "pkg/__init__.py": "",
    "pkg/base.py": "def helper():\n    return 1\n",
    "pkg/api.py": (
        "from pkg.base import helper\n\n"
        "def endpoint():\n"
        "    return helper()\n"
    ),
    "pkg/views.py": (
        "from pkg.api import endpoint\n\n"
        "def view():\n"
        "    return endpoint()\n"
    ),
    "native/include/foo.h": "#pragma once\ninline int foo() { return 1; }\n",
    "native/src/main.cpp": (
        '#include "../include/foo.h"\n'
        "int main() { return foo(); }\n"
    ),
    "native/src/other.cpp": (
        '#include "../include/foo.h"\n'
        "int other() { return foo(); }\n"
    ),
    "web/src/b.ts": "export const b = 1;\n",
    "web/src/a.ts": "import { b } from './b';\nexport const a = b;\n",
    "web/src/c.js": "const { b } = require('./b');\nmodule.exports = b;\n",
}

# Each case: changed paths -> expected (changed, related) edges.
CASES = [
    {
        "id": "python-dependent",
        "changed": ["pkg/base.py"],
        "expected": [("pkg/base.py", "pkg/api.py")],
    },
    {
        "id": "python-both-directions",
        "changed": ["pkg/api.py"],
        "expected": [
            ("pkg/api.py", "pkg/__init__.py"),
            ("pkg/api.py", "pkg/base.py"),
            ("pkg/api.py", "pkg/views.py"),
        ],
    },
    {
        "id": "cpp-header-dependents",
        "changed": ["native/include/foo.h"],
        "expected": [
            ("native/include/foo.h", "native/src/main.cpp"),
            ("native/include/foo.h", "native/src/other.cpp"),
        ],
    },
    {
        "id": "cpp-source-includes",
        "changed": ["native/src/main.cpp"],
        "expected": [("native/src/main.cpp", "native/include/foo.h")],
    },
    {
        "id": "js-dependent",
        "changed": ["web/src/b.ts"],
        "expected": [
            ("web/src/b.ts", "web/src/a.ts"),
            ("web/src/b.ts", "web/src/c.js"),
        ],
    },
    {
        "id": "js-source-import",
        "changed": ["web/src/a.ts"],
        "expected": [("web/src/a.ts", "web/src/b.ts")],
    },
    {
        "id": "mixed",
        "changed": ["pkg/base.py", "native/include/foo.h", "web/src/b.ts"],
        "expected": [
            ("pkg/base.py", "pkg/api.py"),
            ("native/include/foo.h", "native/src/main.cpp"),
            ("native/include/foo.h", "native/src/other.cpp"),
            ("web/src/b.ts", "web/src/a.ts"),
            ("web/src/b.ts", "web/src/c.js"),
        ],
    },
]

_LINE = re.compile(
    r"^- (.+?) (?:imports|includes|is imported by|is included by): (.+)$"
)


def _pairs(section: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for line in section.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        source = match.group(1).strip()
        related = match.group(2).split(", ... and ")[0]
        for target in related.split(","):
            target = target.strip()
            if target:
                found.add((source, target))
    return found


def _metrics(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def evaluate(root: Path) -> dict:
    results = []
    total_tp = total_fp = total_fn = 0
    for case in CASES:
        section = render_changed_dependency_section(root, case["changed"])
        predicted = _pairs(section)
        expected = set(case["expected"])
        tp = len(predicted & expected)
        fp = len(predicted - expected)
        fn = len(expected - predicted)
        total_tp += tp
        total_fp += fp
        total_fn += fn
        results.append(
            {
                "id": case["id"],
                "changed": case["changed"],
                "expected": sorted(expected),
                "predicted": sorted(predicted),
                "metrics": _metrics(tp, fp, fn),
            }
        )
    return {
        "fixture": "multilang-dependency",
        "method": "edge-level (changed file -> related file)",
        "cases": results,
        "summary": _metrics(total_tp, total_fp, total_fn),
    }


class LexicalEmbedding:
    """Deterministic bag-of-tokens embedding for a repeatable retrieval eval."""

    name = "lexical-hash"
    last_misses = 0
    DIM = 128

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.DIM
        for token in re.findall(r"[A-Za-z0-9_]+", text.lower()):
            index = int(hashlib.sha1(token.encode("utf-8")).hexdigest(), 16) % self.DIM
            vector[index] += 1.0
        norm = sum(value * value for value in vector) ** 0.5 or 1.0
        return [value / norm for value in vector]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.last_misses = len(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def _retrieval_metrics(expected: set[str], retrieved: set[str]) -> dict:
    tp = len(expected & retrieved)
    fp = len(retrieved - expected)
    fn = len(expected - retrieved)
    return _metrics(tp, fp, fn)


def evaluate_retrieval(root: Path) -> dict:
    """File-level retrieval P/R/F1 with and without language filtering."""
    emb = LexicalEmbedding()
    store = MemoryVectorStore()
    with tempfile.TemporaryDirectory(prefix="multilang-db-") as temp:
        db = Database(Path(temp) / "codereview.db")
        indexer = RepoIndexer(db, emb, store)
        stats = indexer.ensure_index("fixture/multilang", "fixture", root)
        retriever = ContextRetriever(emb, store)
        cases = []
        totals = {
            "filtered": [0, 0, 0],
            "unfiltered": [0, 0, 0],
        }
        for case in CASES:
            changed = set(case["changed"])
            expected = {related for _, related in case["expected"]}
            query_parts = list(case["changed"])
            for relative in case["changed"]:
                path = root / relative
                if path.is_file():
                    query_parts.append(path.read_text(encoding="utf-8", errors="replace"))
            query = "\n".join(query_parts)
            languages = set(languages_for_paths(case["changed"]))

            results = {}
            for mode, selected_languages in (
                ("filtered", languages),
                ("unfiltered", None),
            ):
                hits = retriever.retrieve(
                    "fixture/multilang",
                    "fixture",
                    query,
                    k=6,
                    exclude_paths=changed,
                    languages=selected_languages,
                )
                retrieved = {
                    hit.metadata.get("file_path")
                    for hit in hits
                    if hit.metadata.get("file_path")
                }
                metrics = _retrieval_metrics(expected, retrieved)
                results[mode] = {
                    "retrieved": sorted(retrieved),
                    "metrics": metrics,
                }
                totals[mode][0] += metrics["tp"]
                totals[mode][1] += metrics["fp"]
                totals[mode][2] += metrics["fn"]
            cases.append(
                {
                    "id": case["id"],
                    "changed": case["changed"],
                    "expected_files": sorted(expected),
                    "languages": sorted(languages),
                    "filtered": results["filtered"],
                    "unfiltered": results["unfiltered"],
                }
            )
        db.close()

    return {
        "fixture": "multilang-retrieval",
        "method": "file-level top-6 retrieval",
        "embedding": "lexical-hash (deterministic proxy, not Cohere)",
        "index_stats": {
            "files": stats.files,
            "chunks": stats.chunks,
        },
        "cases": cases,
        "summary": {
            "filtered": _metrics(*totals["filtered"]),
            "unfiltered": _metrics(*totals["unfiltered"]),
        },
    }


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Multi-language dependency evaluation",
        "",
        f"- Method: {report['method']}",
        f"- Cases: {len(report['cases'])}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Precision | {summary['precision']:.4f} |",
        f"| Recall | {summary['recall']:.4f} |",
        f"| F1 | {summary['f1']:.4f} |",
        f"| TP / FP / FN | {summary['tp']} / {summary['fp']} / {summary['fn']} |",
        "",
        "| Case | P | R | F1 |",
        "|---|---|---|---|",
    ]
    for case in report["cases"]:
        metrics = case["metrics"]
        lines.append(
            f"| {case['id']} | {metrics['precision']:.4f} | "
            f"{metrics['recall']:.4f} | {metrics['f1']:.4f} |"
        )

    retrieval = report.get("retrieval")
    if retrieval:
        lines.extend(
            [
                "",
                "## Retrieval",
                "",
                f"- Method: {retrieval['method']}",
                f"- Embedding: {retrieval['embedding']}",
                "",
                "| Mode | P | R | F1 | TP / FP / FN |",
                "|---|---|---|---|---|",
            ]
        )
        for mode in ("filtered", "unfiltered"):
            metrics = retrieval["summary"][mode]
            lines.append(
                f"| {mode} | {metrics['precision']:.4f} | "
                f"{metrics['recall']:.4f} | {metrics['f1']:.4f} | "
                f"{metrics['tp']} / {metrics['fp']} / {metrics['fn']} |"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate multi-language dependency edges")
    parser.add_argument("--out", default="eval/reports/multilang_dependency.json")
    parser.add_argument("--markdown", default="eval/reports/multilang_dependency.md")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="multilang-") as temp:
        root = Path(temp)
        for relative, source in FILES.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(source, encoding="utf-8")
        report = evaluate(root)
        report["retrieval"] = evaluate_retrieval(root)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = render_markdown(report)
    Path(args.markdown).write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
