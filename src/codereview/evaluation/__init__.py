"""Real-PR evaluation: datasets, metrics and runners."""

from codereview.evaluation.dataset import DatasetPR, ExpectedFinding, load_dataset, save_dataset
from codereview.evaluation.metrics import (
    aggregate,
    bootstrap_ci,
    issue_metrics,
    line_metrics,
    placement_metrics,
)
from codereview.evaluation.runner import evaluate_dataset, evaluate_with_pipeline

__all__ = [
    "DatasetPR",
    "ExpectedFinding",
    "load_dataset",
    "save_dataset",
    "aggregate",
    "bootstrap_ci",
    "issue_metrics",
    "line_metrics",
    "placement_metrics",
    "evaluate_dataset",
    "evaluate_with_pipeline",
]
