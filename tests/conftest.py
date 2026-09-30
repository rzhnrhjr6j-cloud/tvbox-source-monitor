"""Shared fixtures.

Every test runs against real sockets and real SQLite files - spec §35-10
forbids substituting mocks for the final link of the chain.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture()
def cfg():
    from app.config import load_config

    return load_config(explicit_root=ROOT)


@pytest.fixture()
def scratch_cfg(tmp_path):
    """A config whose DB and dist directories live in tmp_path."""
    from app.config import load_config

    return load_config(
        explicit_root=ROOT,
        overrides={
            "app": {
                "db_path": str(tmp_path / "monitor.db"),
                "dist_dir": str(tmp_path / "dist"),
            }
        },
    )


@pytest.fixture()
def store(scratch_cfg):
    from app.storage.sqlite import Store

    handle = Store(scratch_cfg.path("app.db_path", ensure_parent=True))
    yield handle
    handle.close()


@pytest.fixture(autouse=True, scope="session")
def _production_dedup_policy():
    """Install the real §7 dedup policy so URL tests reflect production wiring."""
    from app.config import load_config

    load_config(explicit_root=ROOT)
    yield
