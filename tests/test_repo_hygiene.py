"""Guard against .gitignore rules that swallow real source packages.

A bare ``build/`` pattern cost this project a deployment: it silently ignored
``app/build/``, so the builder never reached GitHub and every workflow died with
``ModuleNotFoundError: No module named 'app.build'``.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_source_files_are_not_gitignored() -> None:
    """Every .py under app/ must be committable, or it silently never deploys."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    ignored = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if subprocess.run(
            ["git", "check-ignore", "-q", rel],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0:
            ignored.append(rel)

    assert not ignored, (
        "these source files are matched by .gitignore and would never be "
        f"deployed: {ignored} - anchor the offending pattern to the repo root "
        "(e.g. '/build/' instead of 'build/')"
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_workflow_files_are_tracked() -> None:
    """The four Actions pipelines must actually be committed."""
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    expected = {
        ".github/workflows/discover.yml",
        ".github/workflows/health-check.yml",
        ".github/workflows/nightly-build.yml",
        ".github/workflows/pages.yml",
    }
    tracked = set(
        subprocess.run(
            ["git", "ls-files", ".github/workflows"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    )
    assert expected <= tracked, f"workflows missing from git: {sorted(expected - tracked)}"
