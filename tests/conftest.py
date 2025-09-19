from __future__ import annotations

import sys
from pathlib import Path


pytest_plugins = ["pytest_asyncio"]


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    src_str = str(src)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


_ensure_src_on_path()
