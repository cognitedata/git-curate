"""Tests for diff_command --tmp flag."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from typer.testing import CliRunner

from git_curate.cli import app
from git_curate.common import git

runner = CliRunner()


class TestDiffTmp:
    def test_prints_path_and_line_count(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        commit_test_file("a.py", "x = 1\n")
        base = str(git("rev-parse", "HEAD~1", _cwd=git_repo)).strip()
        result = runner.invoke(app, ["diff", "--tmp", base])

        assert result.exit_code == 0
        parts = result.output.strip().split()
        assert len(parts) == 2
        path, count = parts
        assert Path(path).exists()
        assert int(count) > 0

    def test_line_count_matches_file(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        commit_test_file("b.py", "a = 1\nb = 2\nc = 3\n")
        base = str(git("rev-parse", "HEAD~1", _cwd=git_repo)).strip()

        result = runner.invoke(app, ["diff", "--tmp", base])

        assert result.exit_code == 0
        path, count = result.output.strip().split()
        assert int(count) == len(Path(path).read_text().splitlines())

    def test_file_contains_diff_content(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        commit_test_file("c.py", "z = 9\n")
        base = str(git("rev-parse", "HEAD~1", _cwd=git_repo)).strip()

        result = runner.invoke(app, ["diff", "--tmp", base])

        assert result.exit_code == 0
        path, _ = result.output.strip().split()
        assert "c.py" in Path(path).read_text()

    def test_empty_diff_exits_nonzero(self, git_repo: Path) -> None:
        base = str(git("rev-parse", "HEAD", _cwd=git_repo)).strip()

        result = runner.invoke(app, ["diff", "--tmp", base])

        assert result.exit_code != 0

    def test_without_tmp_prints_diff_to_stdout(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        commit_test_file("d.py", "w = 0\n")
        base = str(git("rev-parse", "HEAD~1", _cwd=git_repo)).strip()

        result = runner.invoke(app, ["diff", base])

        assert result.exit_code == 0
        assert "diff --git" in result.output
        assert "d.py" in result.output
