"""Shared fixtures for _slice and _group tests."""

from __future__ import annotations

import os
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
import sh

from git_curate.common import git

_REAL_HARNESS_MARKERS = {"claude", "pi"}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip real-harness tests unless their marker is explicitly selected."""
    selected = getattr(config.option, "markexpr", "") or ""
    if any(m in selected for m in _REAL_HARNESS_MARKERS):
        return
    skip = pytest.mark.skip(reason="real-harness test — select explicitly with -m claude or -m pi")
    for item in items:
        if any(item.get_closest_marker(m) for m in _REAL_HARNESS_MARKERS):
            item.add_marker(skip)


@pytest.fixture()
def commit_test_file(git_repo: Path) -> Callable[[str, str], None]:
    def _commit(name: str, content: str) -> None:
        (git_repo / name).write_text(content)
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", f"add {name}", _cwd=git_repo)

    return _commit


@pytest.fixture()
def git_repo(tmp_path: Path) -> Generator[Path, None, None]:
    """Create a temporary git repo, chdir into it, restore CWD on teardown."""
    original_cwd = os.getcwd()
    try:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git = sh.git.bake("--no-pager", "-c", "color.ui=false", _tty_out=False, _cwd=str(repo))

        _git.init("-b", "main")
        _git.config("user.email", "test@example.com")
        _git.config("user.name", "Test User")
        (repo / "README.md").write_text("# repo\n")
        _git.add(".")
        _git.commit("--no-verify", "-m", "init: initial commit")

        os.chdir(repo)
        yield repo
    finally:
        os.chdir(original_cwd)
