"""Tests for git-curate abort command."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from git_curate.cli import app
from git_curate.common import curate_git, git

_runner = CliRunner()
_git_commit = git.commit.bake("--no-verify")
_curate_commit = curate_git.commit.bake("--no-verify")


def _make_temp_commits(git_repo: Path, n: int = 3) -> str:
    """Create n curate-authored temp commits on top of current HEAD, return base SHA."""
    base = str(git("rev-parse", "HEAD")).strip()
    for i in range(n):
        (git_repo / f"temp{i}.py").write_text(f"x = {i}\n")
        curate_git.add(".", _cwd=git_repo)
        _curate_commit("-m", f"temp: temp{i}.py:L1-1", _cwd=git_repo)
    return base


class TestAbortCommand:
    def test_resets_head_to_base(self, git_repo: Path) -> None:
        base = _make_temp_commits(git_repo, n=3)

        result = _runner.invoke(app, ["abort"])

        assert result.exit_code == 0, result.output
        head = str(git("rev-parse", "HEAD")).strip()
        assert head == base

    def test_changes_remain_as_unstaged(self, git_repo: Path) -> None:
        # Stage and slice a change so we have a real curate session in progress.
        (git_repo / "main.py").write_text("original\n")
        git.add("main.py", _cwd=git_repo)
        _git_commit("-m", "base: add main.py", _cwd=git_repo)

        (git_repo / "main.py").write_text("modified\n")
        git.add("main.py", _cwd=git_repo)
        _runner.invoke(app, ["slice"])

        _runner.invoke(app, ["abort"])

        # --mixed: index reset to base, working tree unchanged → change is unstaged
        staged = str(git("diff", "--cached", "--name-only")).strip()
        assert staged == ""
        unstaged = str(git("diff", "--name-only")).strip()
        assert "main.py" in unstaged

    def test_removes_temp_commits(self, git_repo: Path) -> None:
        base = _make_temp_commits(git_repo, n=2)

        _runner.invoke(app, ["abort"])

        log = str(git.log("--oneline", f"{base}..HEAD")).strip()
        assert log == ""

    def test_no_curate_commits_after_abort(self, git_repo: Path) -> None:
        _make_temp_commits(git_repo, n=1)

        _runner.invoke(app, ["abort"])

        head_author = str(git.log("-1", "--format=%ae")).strip()
        assert head_author != "git-curate@local"

    def test_reports_dropped_commit_count(self, git_repo: Path) -> None:
        _make_temp_commits(git_repo, n=3)

        result = _runner.invoke(app, ["abort"])

        assert "3 temp commits" in result.output

    def test_singular_commit_count(self, git_repo: Path) -> None:
        _make_temp_commits(git_repo, n=1)

        result = _runner.invoke(app, ["abort"])

        assert "1 temp commit" in result.output
        assert "1 temp commits" not in result.output

    def test_slice_then_abort_loses_no_changes(self, git_repo: Path) -> None:
        """Changes staged before slice must survive abort as unstaged changes.

        Untracked files must also survive. The full diff must be identical
        before and after the slice+abort round-trip.
        """
        # Lay down a tracked file at the base commit
        (git_repo / "main.py").write_text("line1\nline2\nline3\n")
        git.add("main.py", _cwd=git_repo)
        _git_commit("-m", "base: add main.py", _cwd=git_repo)

        # Modify the tracked file and stage it
        (git_repo / "main.py").write_text("line1\nLINE2_CHANGED\nline3\n")
        git.add("main.py", _cwd=git_repo)

        # Leave an untracked file untouched
        (git_repo / "untracked.py").write_text("new untracked\n")

        # Capture the full diff from HEAD before slice (staged changes relative to HEAD)
        diff_before = str(git("diff", "--cached")).strip()

        # Slice — creates temp commits from the staged changes
        result = _runner.invoke(app, ["slice"])
        assert result.exit_code == 0, result.output

        # Abort — should undo temp commits and restore changes as unstaged
        result = _runner.invoke(app, ["abort"])
        assert result.exit_code == 0, result.output

        # Nothing staged
        assert str(git("diff", "--cached", "--name-only")).strip() == ""

        # Unstaged diff must match the original staged diff exactly
        diff_after = str(git("diff")).strip()
        assert diff_after == diff_before

        # Untracked file still present
        untracked = str(git("ls-files", "--others", "--exclude-standard")).strip()
        assert "untracked.py" in untracked

    def test_no_session_reports_nothing_to_do(self, git_repo: Path) -> None:
        result = _runner.invoke(app, ["abort"])

        assert result.exit_code == 0
        assert "no git-curate session" in result.output.lower()
