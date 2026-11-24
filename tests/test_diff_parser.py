"""Unified-diff parsing used by GitHub and GitLab adapters."""
from __future__ import annotations

from codereview.diff.parser import (
    changed_paths,
    normalize_diff,
    parse_unified_diff,
    valid_line_map,
)

from tests.conftest import SAMPLE_DIFF


def test_valid_line_map_only_added_new_lines():
    files = parse_unified_diff(SAMPLE_DIFF)
    assert [f.path for f in files] == ["app.py"]
    assert valid_line_map(files) == {"app.py": {3, 4}}


def test_parse_new_deleted_renamed_flags():
    diff = """\
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,1 @@
+print("new")
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,1 +0,0 @@
-print("gone")
diff --git a/old.py b/renamed.py
similarity index 100%
rename from old.py
rename to renamed.py
"""
    files = parse_unified_diff(diff)
    assert [f.path for f in files] == ["new.py", "gone.py", "renamed.py"]
    assert files[0].is_new is True
    assert files[1].is_deleted is True
    assert files[2].old_path == "old.py"
    assert changed_paths(files) == ["new.py", "renamed.py"]


def test_quoted_paths_and_binary_files():
    diff = """\
diff --git "a/file with space.py" "b/file with space.py"
--- "a/file with space.py"
+++ "b/file with space.py"
@@ -1,1 +1,1 @@
-old
+new
diff --git a/logo.png b/logo.png
Binary files a/logo.png and b/logo.png differ
"""
    files = parse_unified_diff(diff)
    lines = valid_line_map(files)
    assert files[0].path == "file with space.py"
    assert lines["file with space.py"] == {1}
    assert files[1].path == "logo.png"
    assert files[1].hunks == []
    assert lines["logo.png"] == set()


def test_normalize_bare_hunk_is_idempotent():
    bare = "@@ -1,1 +1,2 @@\n-x\n+y\n+z\n"
    normalized = normalize_diff(bare, "app.py")
    assert normalized.startswith("diff --git a/app.py b/app.py")
    assert normalize_diff(normalized, "app.py") == normalized
    options = valid_line_map(parse_unified_diff(normalized))
    assert options == {"app.py": {1, 2}}
