from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.storage.sqlite import Store


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "actions-state.sh"


def test_release_snapshot_dry_run_accepts_runtime_database(tmp_path: Path) -> None:
    db_path = tmp_path / "monitor.db"
    with Store(db_path):
        pass

    env = os.environ.copy()
    env["TVBOX_STATE_DB"] = str(db_path)
    result = subprocess.run(
        ["bash", str(SCRIPT), "save", "--dry-run"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "snapshot validated" in result.stderr
    assert "dry run: skipped release upload" in result.stderr
