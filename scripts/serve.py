"""Run the webhook server without installing the package.

    python -I -X utf8 scripts/serve.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codereview.server.__main__ import main


if __name__ == "__main__":
    main()
