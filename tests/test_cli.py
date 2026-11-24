"""CLI exit-code semantics (the fail-loud gate)."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from codereview.cli import main, review_exit_code


@dataclass
class Outcome:
    status: str
    posted: bool = False


def test_review_exit_code():
    assert review_exit_code(Outcome("completed", posted=True), post=True) == 0
    assert review_exit_code(Outcome("completed", posted=False), post=True) == 0
    assert review_exit_code(Outcome("skipped", posted=False), post=True) == 0
    assert review_exit_code(Outcome("failed"), post=True) == 1
    assert review_exit_code(Outcome("failed"), post=False) == 1
    assert review_exit_code(Outcome("completed_with_errors", posted=False), post=True) == 2
    assert review_exit_code(Outcome("completed_with_errors", posted=True), post=True) == 0
    assert review_exit_code(Outcome("completed_with_errors", posted=False), post=False) == 0


def test_push_requires_before_after_branch():
    with pytest.raises(SystemExit) as exc:
        main(["--repo", "owner/repo", "--push"])
    assert exc.value.code == 2


def test_pr_required_without_push():
    with pytest.raises(SystemExit) as exc:
        main(["--repo", "owner/repo"])
    assert exc.value.code == 2
