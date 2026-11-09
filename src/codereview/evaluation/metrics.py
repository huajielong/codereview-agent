"""Issue-level, line-level and placement metrics.

Issue matching is greedy one-to-one so a single finding cannot satisfy two
expected issues. Keyword matching is the default; callers can pass an embedding
function to reproduce L6's hybrid matching for comparison.
"""
from __future__ import annotations

import math
import random
from typing import Callable

from codereview.models import Finding
from codereview.review.paths import normalize_path, resolve_path
from codereview.text_utils import token_set

EmbedFn = Callable[[str], list[float]]


def _issue_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Finding):
        return value.issue
    if isinstance(value, dict):
        return str(value.get("issue", ""))
    return str(getattr(value, "issue", ""))


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if not norm_left or not norm_right:
        return 0.0
    return dot / (norm_left * norm_right)


def issue_similarity(
    expected: str,
    found: str,
    *,
    embed_fn: EmbedFn | None = None,
    cache: dict[str, list[float]] | None = None,
) -> float:
    expected_tokens = token_set(expected)
    found_tokens = token_set(found)
    if expected_tokens:
        keyword = len(expected_tokens & found_tokens) / len(expected_tokens)
    else:
        keyword = 0.0

    expected_lower = expected.lower().strip()
    found_lower = found.lower().strip()
    if expected_lower and found_lower and (
        expected_lower in found_lower or found_lower in expected_lower
    ):
        keyword = max(keyword, 0.7)

    if embed_fn is None:
        return keyword

    cache = cache if cache is not None else {}
    try:
        if expected not in cache:
            cache[expected] = embed_fn(expected)
        if found not in cache:
            cache[found] = embed_fn(found)
        semantic = _cosine(cache[expected], cache[found])
    except Exception:  # noqa: BLE001 - embeddings are optional
        semantic = 0.0
    return max(keyword, semantic)


def match_issues(
    expected: list[str],
    found: list[str],
    *,
    threshold: float = 0.3,
    embed_fn: EmbedFn | None = None,
) -> list[tuple[int, int, float]]:
    """Greedy one-to-one matching. Returns (expected_index, found_index, score)."""
    cache: dict[str, list[float]] = {}
    used: set[int] = set()
    pairs: list[tuple[int, int, float]] = []
    for expected_index, expected_issue in enumerate(expected):
        best_index = None
        best_score = 0.0
        for found_index, found_issue in enumerate(found):
            if found_index in used:
                continue
            score = issue_similarity(
                expected_issue, found_issue, embed_fn=embed_fn, cache=cache
            )
            if score > best_score:
                best_score = score
                best_index = found_index
        if best_index is not None and best_score >= threshold:
            used.add(best_index)
            pairs.append((expected_index, best_index, best_score))
    return pairs


def _prf(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {"precision": precision, "recall": recall, "f1": f1, "tp": tp, "fp": fp, "fn": fn}


def issue_metrics(
    expected,
    findings: list[Finding],
    *,
    threshold: float = 0.3,
    embed_fn: EmbedFn | None = None,
    labeled: bool = True,
) -> dict:
    expected_issues = [_issue_text(item) for item in expected]
    expected_issues = [issue for issue in expected_issues if issue.strip()]
    found_issues = [finding.issue for finding in findings if finding.issue.strip()]

    if not labeled:
        result = _prf(0, 0, 0)
        result.update({"n_expected": 0, "n_found": len(found_issues), "labeled": False})
        return result

    if not expected_issues:
        # A labeled PR with zero expected issues: every finding is a false
        # positive, not "not evaluated".
        result = _prf(0, len(found_issues), 0)
        result.update({"n_expected": 0, "n_found": len(found_issues), "labeled": True})
        return result

    pairs = match_issues(
        expected_issues, found_issues, threshold=threshold, embed_fn=embed_fn
    )
    tp = len(pairs)
    fp = max(0, len(found_issues) - tp)
    fn = len(expected_issues) - tp
    result = _prf(tp, fp, fn)
    result.update({"n_expected": len(expected_issues), "n_found": len(found_issues), "labeled": True})
    return result


def line_metrics(expected, findings: list[Finding], *, labeled: bool = True) -> dict:
    expected_locations = set()
    for item in expected:
        if isinstance(item, dict):
            file, line = item.get("file"), item.get("line")
        else:
            file, line = getattr(item, "file", None), getattr(item, "line", None)
        if file and line:
            expected_locations.add((normalize_path(str(file)), int(line)))

    actual_locations = {
        (normalize_path(finding.file), finding.line)
        for finding in findings
        if finding.file and finding.line
    }
    if not labeled:
        result = _prf(0, 0, 0)
        result.update({"n_expected": 0, "n_found": len(actual_locations), "labeled": False})
        return result
    if not expected_locations:
        result = _prf(0, len(actual_locations), 0)
        result.update({"n_expected": 0, "n_found": len(actual_locations), "labeled": True})
        return result

    tp = len(expected_locations & actual_locations)
    result = _prf(tp, len(actual_locations) - tp, len(expected_locations) - tp)
    result.update(
        {
            "n_expected": len(expected_locations),
            "n_found": len(actual_locations),
            "labeled": True,
        }
    )
    return result


def placement_metrics(findings: list[Finding], valid_lines: dict[str, set[int]]) -> dict:
    total = len(findings)
    located = 0
    mapped = 0
    for finding in findings:
        if finding.file and finding.line:
            located += 1
            path = resolve_path(finding.file, valid_lines)
            if path and finding.line in valid_lines.get(path, set()):
                mapped += 1
    return {
        "total": total,
        "located": located,
        "mapped": mapped,
        "located_rate": located / total if total else 0.0,
        "actionable_rate": mapped / total if total else 0.0,
    }


def bootstrap_ci(
    values: list[float], *, n_boot: int = 1000, seed: int = 0
) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    if len(values) == 1:
        return values[0], values[0]
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(sum(sample) / len(sample))
    means.sort()
    lower = means[int(0.025 * (len(means) - 1))]
    upper = means[int(0.975 * (len(means) - 1))]
    return lower, upper


def aggregate(per_pr: list[dict]) -> dict:
    labeled = [item for item in per_pr if item["issue"].get("labeled")]
    line_labeled = [item for item in per_pr if item["line"].get("labeled")]

    def micro(items: list[dict], key: str) -> dict:
        tp = sum(item[key]["tp"] for item in items)
        fp = sum(item[key]["fp"] for item in items)
        fn = sum(item[key]["fn"] for item in items)
        return _prf(tp, fp, fn)

    issue_macro = [item["issue"]["f1"] for item in labeled]
    line_macro = [item["line"]["f1"] for item in line_labeled]
    placements = [item["placement"]["actionable_rate"] for item in per_pr]

    return {
        "issue": {
            **micro(labeled, "issue"),
            "macro_f1": sum(issue_macro) / len(issue_macro) if issue_macro else 0.0,
            "f1_ci95": bootstrap_ci(issue_macro),
        },
        "line": {
            **micro(line_labeled, "line"),
            "macro_f1": sum(line_macro) / len(line_macro) if line_macro else 0.0,
            "f1_ci95": bootstrap_ci(line_macro),
        },
        "placement": {
            "actionable_rate": sum(placements) / len(placements) if placements else 0.0,
            "total_findings": sum(item["placement"]["total"] for item in per_pr),
            "mapped_findings": sum(item["placement"]["mapped"] for item in per_pr),
        },
        "n_prs": len(per_pr),
        "n_labeled": len(labeled),
        "errors": sum(1 for item in per_pr if item.get("error")),
        "per_pr": per_pr,
    }
