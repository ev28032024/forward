from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


try:  # pragma: no cover - optional dependency
    import pytest_asyncio  # type: ignore  # noqa: F401
except ImportError:  # pragma: no cover - fallback path
    pytest_plugins: list[str] = []

    def pytest_configure(config: pytest.Config) -> None:
        config.addinivalue_line("markers", "asyncio: run test in an event loop")

    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
        if pyfuncitem.get_closest_marker("asyncio") is None:
            return None
        loop = asyncio.new_event_loop()
        try:
            kwargs = {
                name: pyfuncitem.funcargs[name]
                for name in pyfuncitem._fixtureinfo.argnames  # type: ignore[attr-defined]
            }
            loop.run_until_complete(pyfuncitem.obj(**kwargs))
        finally:
            loop.close()
        return True
else:  # pragma: no cover - exercised in environments with pytest-asyncio
    pytest_plugins = ["pytest_asyncio"]


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    src_str = str(src)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


_ensure_src_on_path()
