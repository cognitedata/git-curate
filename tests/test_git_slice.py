"""Tests for git_slice.py."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from git_curate.common import RebaseInProgressError, git
from git_curate.slice import (
    Hunk,
    _dry_run_remaining,
    _is_diff_context,
    _make_hunk_header,
    _rebuild_sub_hunk_headers,
    _split_hunk,
    parse_first_hunk,
    slice_command,
    slice_hunks,
)

# ---------------------------------------------------------------------------
# Sample diff strings reused across tests
# ---------------------------------------------------------------------------

SINGLE_HUNK_DIFF = """\
diff --git a/foo.py b/foo.py
index aaa..bbb 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 context
-old line
+new line
 context
"""

TWO_HUNK_DIFF = """\
diff --git a/foo.py b/foo.py
index aaa..bbb 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 context
-old line 1
+new line 1
 context
@@ -10,3 +10,3 @@
 context
-old line 2
+new line 2
 context
"""

TWO_FILE_DIFF = """\
diff --git a/a.py b/a.py
index aaa..bbb 100644
--- a/a.py
+++ b/a.py
@@ -1 +1 @@
-old
+new
diff --git a/b.py b/b.py
index ccc..ddd 100644
--- a/b.py
+++ b/b.py
@@ -5 +5 @@
-x
+y
"""

BINARY_THEN_TEXT_DIFF = """\
diff --git a/data.bin b/data.bin
index aaa..bbb 100644
Binary files a/data.bin and b/data.bin differ
diff --git a/foo.py b/foo.py
index aaa..bbb 100644
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,3 @@
 context
-old line
+new line
 context
"""


# ---------------------------------------------------------------------------
# _is_diff_context
# ---------------------------------------------------------------------------


class TestIsCtx:
    def test_space_prefix(self) -> None:
        assert _is_diff_context(" context line\n") is True

    def test_backslash_prefix(self) -> None:
        assert _is_diff_context("\\ No newline at end of file\n") is True

    def test_bare_newline(self) -> None:
        assert _is_diff_context("\n") is True

    def test_crlf_bare(self) -> None:
        assert _is_diff_context("\r\n") is True

    def test_added_line(self) -> None:
        assert _is_diff_context("+new line\n") is False

    def test_removed_line(self) -> None:
        assert _is_diff_context("-old line\n") is False

    def test_hunk_header(self) -> None:
        assert _is_diff_context("@@ -1,3 +1,3 @@\n") is False

    def test_empty_string(self) -> None:
        assert _is_diff_context("") is False


# ---------------------------------------------------------------------------
# _make_hunk_header
# ---------------------------------------------------------------------------


class TestMakeHunkHeader:
    def test_basic(self) -> None:
        assert _make_hunk_header(1, 3, 1, 3) == "@@ -1,3 +1,3 @@\n"

    def test_with_label(self) -> None:
        result = _make_hunk_header(10, 5, 10, 6, " def foo")
        assert result == "@@ -10,5 +10,6 @@ def foo\n"

    def test_zero_count(self) -> None:
        assert _make_hunk_header(5, 0, 5, 3) == "@@ -5,0 +5,3 @@\n"


# ---------------------------------------------------------------------------
# _split_hunk
# ---------------------------------------------------------------------------


class TestSplitHunk:
    def _make_hunk(self, lines: list[str]) -> list[str]:
        return ["@@ -1,20 +1,20 @@\n"] + lines

    def test_no_split_single_change(self) -> None:
        lines = self._make_hunk([" ctx\n", "+add\n", " ctx\n"])
        assert _split_hunk(lines, min_context=1) is None

    def test_no_split_context_run_too_short(self) -> None:
        lines = self._make_hunk(["+change1\n", " ctx1\n", " ctx2\n", "+change2\n"])
        assert _split_hunk(lines, min_context=3) is None

    def test_splits_at_sufficient_context_run(self) -> None:
        lines = self._make_hunk(
            [
                "+change1\n",
                " ctx1\n",
                " ctx2\n",
                " ctx3\n",
                "+change2\n",
            ]
        )
        result = _split_hunk(lines, min_context=3)
        assert result is not None
        assert len(result) == 2
        assert result[0][-1] == " ctx3\n"
        assert result[1][0] == " ctx1\n"

    def test_splits_into_three_sub_hunks(self) -> None:
        ctx = [" c\n"] * 3
        lines = self._make_hunk(["+a\n"] + ctx + ["+b\n"] + ctx + ["+c\n"])
        result = _split_hunk(lines, min_context=3)
        assert result is not None
        assert len(result) == 3

    def test_empty_body_returns_none(self) -> None:
        lines = ["@@ -1,0 +1,0 @@\n"]
        assert _split_hunk(lines, min_context=1) is None

    def test_only_context_no_split(self) -> None:
        lines = self._make_hunk([" ctx\n"] * 10)
        assert _split_hunk(lines, min_context=1) is None

    def test_removal_line_triggers_split(self) -> None:
        ctx = [" c\n"] * 4
        lines = self._make_hunk(["-removed\n"] + ctx + ["+added\n"])
        result = _split_hunk(lines, min_context=4)
        assert result is not None
        assert len(result) == 2

    def test_exact_min_context_boundary(self) -> None:
        # Exactly min_context=2 context lines → should split
        lines = self._make_hunk(["+a\n", " c1\n", " c2\n", "+b\n"])
        assert _split_hunk(lines, min_context=2) is not None
        # One fewer → should not split
        assert _split_hunk(lines, min_context=3) is None


# ---------------------------------------------------------------------------
# _rebuild_sub_hunk_headers
# ---------------------------------------------------------------------------


class TestRebuildSubHunkHeaders:
    HUNK_RE = re.compile(r"^@@ -(\d+),(\d+) \+(\d+),(\d+) @@")

    def test_two_sub_hunks_have_correct_structure(self) -> None:
        original_header = "@@ -1,8 +1,8 @@\n"
        sub_hunk_bodies = [
            ["+change1\n", " ctx1\n", " ctx2\n", " ctx3\n"],
            [" ctx1\n", " ctx2\n", " ctx3\n", "+change2\n"],
        ]
        result = _rebuild_sub_hunk_headers(original_header, sub_hunk_bodies)
        assert len(result) == 2
        for sub_hunk in result:
            assert self.HUNK_RE.match(sub_hunk[0]) is not None

    def test_first_sub_hunk_starts_at_original_position(self) -> None:
        original_header = "@@ -5,6 +10,6 @@\n"
        bodies = [["+x\n", " c\n"], [" c\n", "-y\n"]]
        result = _rebuild_sub_hunk_headers(original_header, bodies)
        m = self.HUNK_RE.match(result[0][0])
        assert m is not None
        assert m.group(1) == "5"
        assert m.group(3) == "10"

    def test_second_sub_hunk_position_advances(self) -> None:
        original_header = "@@ -1,6 +1,6 @@\n"
        # First body: 1 add + 3 ctx = old_count=3, new_count=4
        bodies = [
            ["+a\n", " c\n", " c\n", " c\n"],
            [" c\n", " c\n", " c\n", "-b\n"],
        ]
        result = _rebuild_sub_hunk_headers(original_header, bodies)
        m2 = self.HUNK_RE.match(result[1][0])
        assert m2 is not None
        # old_pos advanced by 3 context lines (the 3 ctx in first body)
        assert int(m2.group(1)) > 1
        # new_pos advanced by 4 (1 add + 3 ctx in first body)
        assert int(m2.group(3)) > 1

    def test_label_preserved_on_all_sub_hunks(self) -> None:
        original_header = "@@ -10,5 +10,5 @@ def my_func\n"
        bodies = [["+x\n"], ["-y\n"]]
        result = _rebuild_sub_hunk_headers(original_header, bodies)
        for sub_hunk in result:
            assert " def my_func" in sub_hunk[0]


# ---------------------------------------------------------------------------
# parse_first_hunk
# ---------------------------------------------------------------------------


class TestParseFirstHunk:
    def test_returns_none_on_empty(self) -> None:
        assert parse_first_hunk("") is None
        assert parse_first_hunk("   \n") is None

    def test_returns_none_on_no_file_header(self) -> None:
        assert parse_first_hunk("not a diff\n") is None

    def test_single_hunk_single_file(self) -> None:
        result = parse_first_hunk(SINGLE_HUNK_DIFF)
        assert result is not None
        assert isinstance(result, Hunk)
        assert result.file_path == "foo.py"
        assert "diff --git" in result.mini_patch
        assert "--- a/foo.py" in result.mini_patch
        assert "+++ b/foo.py" in result.mini_patch
        assert result.mini_patch.endswith("\n")

    def test_two_hunk_diff_returns_only_first_hunk(self) -> None:
        result = parse_first_hunk(TWO_HUNK_DIFF)
        assert result is not None
        hunk_headers = [ln for ln in result.mini_patch.splitlines() if ln.startswith("@@")]
        assert len(hunk_headers) == 1

    def test_two_file_diff_returns_first_file_only(self) -> None:
        result = parse_first_hunk(TWO_FILE_DIFF)
        assert result is not None
        assert result.file_path == "a.py"
        assert "b.py" not in result.mini_patch

    def test_line_desc_format(self) -> None:
        result = parse_first_hunk(SINGLE_HUNK_DIFF)
        assert result is not None
        assert re.match(r"L\d+-\d+$", result.line_desc)

    def test_min_context_zero_disables_split(self) -> None:
        ctx = " c\n" * 5
        diff = (
            "diff --git a/f.py b/f.py\n"
            "index aaa..bbb 100644\n"
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,12 +1,12 @@\n"
            "+change1\n" + ctx + "+change2\n"
        )
        r_split = parse_first_hunk(diff, min_context=3)
        r_no_split = parse_first_hunk(diff, min_context=0)
        assert r_no_split is not None
        assert r_split is not None
        assert len(r_no_split.mini_patch.splitlines()) > len(r_split.mini_patch.splitlines())

    def test_mini_patch_always_ends_with_newline(self) -> None:
        diff = SINGLE_HUNK_DIFF.rstrip("\n")
        result = parse_first_hunk(diff)
        assert result is not None
        assert result.mini_patch.endswith("\n")

    def test_skips_binary_file_and_returns_next_text_hunk(self) -> None:
        result = parse_first_hunk(BINARY_THEN_TEXT_DIFF)
        assert result is not None
        assert result.file_path == "foo.py"
        assert "Binary" not in result.mini_patch
        assert "+++ b/foo.py" in result.mini_patch

    def test_all_binary_returns_none(self) -> None:
        diff = """\
diff --git a/a.bin b/a.bin
index aaa..bbb 100644
Binary files a/a.bin and b/a.bin differ
diff --git a/b.bin b/b.bin
index ccc..ddd 100644
Binary files a/b.bin and b/b.bin differ
"""
        assert parse_first_hunk(diff) is None


# ---------------------------------------------------------------------------
# _dry_run_remaining
# ---------------------------------------------------------------------------


class TestDryRunRemaining:
    def test_counts_and_labels_all_hunks(self) -> None:
        output = _dry_run_remaining(TWO_HUNK_DIFF)
        lines = [ln for ln in output.splitlines() if ln.strip()]
        assert len(lines) == 2
        assert "[1]" in lines[0]
        assert "[2]" in lines[1]
        assert "foo.py" in lines[0]
        assert "foo.py" in lines[1]

    def test_empty_diff(self) -> None:
        assert _dry_run_remaining("") == ""

    def test_two_file_diff_all_hunks_labeled(self) -> None:
        output = _dry_run_remaining(TWO_FILE_DIFF)
        lines = output.strip().splitlines()
        assert len(lines) == 2
        assert "a.py" in lines[0]
        assert "b.py" in lines[1]


# ---------------------------------------------------------------------------
# slice_hunks (integration — requires git_repo fixture)
# ---------------------------------------------------------------------------


class TestSliceHunks:
    def test_empty_staged_returns_zero(self, git_repo: Path) -> None:
        result = slice_hunks([])
        assert result == 0

    def _stage(self, git_repo: Path, *paths: str) -> None:
        """Stage one or more files."""
        git.add("--", *paths, _cwd=git_repo)

    def test_single_file_creates_one_commit(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        commit_test_file("example.py", "x = 1\n")
        (git_repo / "example.py").write_text("x = 2\n")
        self._stage(git_repo, "example.py")
        result = slice_hunks([])
        assert result == 1
        log = str(git.log("--oneline"))
        assert "temp: example.py:" in log

    def test_temp_message_includes_diff_hash(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        commit_test_file("example.py", "x = 1\n")
        (git_repo / "example.py").write_text("x = 2\n")
        self._stage(git_repo, "example.py")
        slice_hunks([])
        log = str(git.log("--oneline"))
        assert re.search(r"temp: example\.py:L\d+-\d+ #[0-9a-f]{8}-\d+", log)

    def test_commit_count_suffix_makes_messages_unique(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # Two hunks far apart in one file, sliced in a single call.
        # Even if hashes collided, the -N suffix would keep messages distinct.
        content = "\n".join(f"line{i} = {i}" for i in range(1, 31)) + "\n"
        f = git_repo / "multi.py"
        commit_test_file("multi.py", content)
        lines = content.splitlines()
        lines[0] = "line1 = 999"
        lines[27] = "line28 = 999"
        f.write_text("\n".join(lines) + "\n")
        self._stage(git_repo, "multi.py")
        assert slice_hunks([]) == 2

        subjects = [line for line in str(git.log("--format=%s")).splitlines() if line.startswith("temp:")]
        assert len(subjects) == 2
        for s in subjects:
            assert re.search(r"temp: multi\.py:L\d+-\d+ #[0-9a-f]{8}-\d+", s)
        # suffixes must be unique (1 and 2)
        suffixes = [m.group(1) for s in subjects if (m := re.search(r"-(\d+)$", s))]
        assert len(set(suffixes)) == 2

    def test_two_separate_hunks_create_two_commits(self, git_repo: Path) -> None:
        # Commit a file with 30 lines, then modify lines 1 and 28 (far apart)
        content = "\n".join(f"line{i} = {i}" for i in range(1, 31)) + "\n"
        f = git_repo / "multi.py"
        f.write_text(content)
        git.add(".", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add multi", _cwd=git_repo)
        lines = content.splitlines()
        lines[0] = "line1 = 999"
        lines[27] = "line28 = 999"
        f.write_text("\n".join(lines) + "\n")
        self._stage(git_repo, "multi.py")
        result = slice_hunks([])
        assert result == 2

    def test_binary_file_does_not_block_slicing(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # Commit a binary file alongside a text file
        binary_path = git_repo / "data.bin"
        binary_path.write_bytes(bytes(range(256)))
        git.add("data.bin", _cwd=git_repo)
        git.commit("--no-verify", "-m", "add binary", _cwd=git_repo)
        commit_test_file("code.py", "x = 1\n")

        # Modify both — binary change sorts first alphabetically
        binary_path.write_bytes(bytes(reversed(range(256))))
        (git_repo / "code.py").write_text("x = 2\n")
        # Stage both; binary will be skipped, text hunk committed
        git.add(".", _cwd=git_repo)

        result = slice_hunks([])
        # The text hunk should be committed; binary stays staged
        assert result == 1
        log = str(git.log("--oneline"))
        assert "temp: code.py:" in log

    def test_path_filter_limits_slicing(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        commit_test_file("a.py", "a = 1\n")
        commit_test_file("b.py", "b = 1\n")
        (git_repo / "a.py").write_text("a = 2\n")
        (git_repo / "b.py").write_text("b = 2\n")
        # Stage both files
        git.add(".", _cwd=git_repo)
        result = slice_hunks(["a.py"])
        assert result == 1
        log = str(git.log("--oneline"))
        assert "temp: a.py:" in log
        # b.py should still be staged (real index untouched)
        staged = str(git.diff("--cached", "--name-only"))
        assert "b.py" in staged

    def test_staged_new_file_creates_commit(self, git_repo: Path) -> None:
        # Stage a brand-new file (never committed before)
        (git_repo / "new.py").write_text("x = 1\ny = 2\n")
        self._stage(git_repo, "new.py")
        result = slice_hunks([])
        assert result == 1
        log = str(git.log("--oneline"))
        assert "temp: new.py:" in log
        # New file should now be committed into HEAD
        show = str(git.show("HEAD:new.py"))
        assert "x = 1" in show

    def test_staged_deleted_file_creates_commit(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        commit_test_file("gone.py", "x = 1\n")
        git.rm("gone.py", _cwd=git_repo)
        result = slice_hunks([])
        assert result == 1
        log = str(git.log("--oneline"))
        assert "temp: gone.py:" in log
        # File should be absent from HEAD
        ls = str(git("ls-files", "gone.py"))
        assert ls.strip() == ""


# ---------------------------------------------------------------------------
# slice_command (integration — requires git_repo fixture)
# ---------------------------------------------------------------------------


class TestSliceCommand:
    def test_dry_run_does_not_commit(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        commit_test_file("example.py", "x = 1\n")
        (git_repo / "example.py").write_text("x = 2\n")
        git.add("example.py", _cwd=git_repo)
        slice_command(
            paths=[],
            dry_run=True,
            all_changes=False,
            split_context=4,
            from_commit=None,
        )
        log = str(git.log("--oneline", _cwd=git_repo))
        assert "temp:" not in log

    def test_partial_staging_allowed(self, git_repo: Path, commit_test_file: Callable[[str, str], None]) -> None:
        # File with both staged and unstaged changes — the slicer should commit
        # only the staged portion and leave the working tree untouched.
        commit_test_file("f.py", "a = 1\nb = 2\n")
        # Stage one change …
        (git_repo / "f.py").write_text("a = 99\nb = 2\n")
        git.add("f.py", _cwd=git_repo)
        # … then make an additional unstaged change on top
        (git_repo / "f.py").write_text("a = 99\nb = 99\n")

        slice_command(
            paths=[],
            dry_run=False,
            all_changes=False,
            split_context=4,
            from_commit=None,
        )

        # The staged hunk (a = 99) should be in HEAD
        show = str(git("show", "HEAD:f.py", _cwd=git_repo))
        assert "a = 99" in show
        # The unstaged hunk (b = 99) must still only be in the working tree
        assert "b = 2" in show  # HEAD still has b = 2
        working = (git_repo / "f.py").read_text()
        assert "b = 99" in working

    def test_no_staged_no_all_flag_exits_nonzero(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # Nothing staged, no --all — should exit with an error.
        commit_test_file("f.py", "a = 1\n")
        (git_repo / "f.py").write_text("a = 99\n")  # unstaged only

        with pytest.raises(SystemExit) as exc_info:
            slice_command(
                paths=[],
                dry_run=False,
                all_changes=False,
                split_context=4,
                from_commit=None,
            )
        assert exc_info.value.code == 1

    def test_all_flag_stages_and_slices_unstaged_changes(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # --all with only unstaged changes should stage everything and create temp commits.
        commit_test_file("f.py", "a = 1\n")
        (git_repo / "f.py").write_text("a = 99\n")  # unstaged

        slice_command(
            paths=[],
            dry_run=False,
            all_changes=True,
            split_context=4,
            from_commit=None,
        )

        log = str(git.log("--oneline", _cwd=git_repo))
        assert "temp: f.py:" in log

    def test_all_flag_does_not_stage_untracked_files(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # --all must not stage untracked files; only tracked modifications should be sliced.
        commit_test_file("tracked.py", "a = 1\n")
        (git_repo / "tracked.py").write_text("a = 99\n")  # unstaged modification
        (git_repo / "untracked.py").write_text("new file\n")  # never committed

        slice_command(
            paths=[],
            dry_run=False,
            all_changes=True,
            split_context=4,
            from_commit=None,
        )

        # tracked.py change must be committed
        log = str(git.log("--oneline", _cwd=git_repo))
        assert "temp: tracked.py:" in log
        # untracked.py must remain untracked
        untracked = str(git("ls-files", "--others", "--exclude-standard", _cwd=git_repo)).strip()
        assert "untracked.py" in untracked

    def test_all_flag_with_paths_stages_only_those_paths(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # --all with explicit paths should only stage (and slice) those paths.
        commit_test_file("a.py", "a = 1\n")
        commit_test_file("b.py", "b = 1\n")
        (git_repo / "a.py").write_text("a = 99\n")
        (git_repo / "b.py").write_text("b = 99\n")

        slice_command(
            paths=["a.py"],
            dry_run=False,
            all_changes=True,
            split_context=4,
            from_commit=None,
        )

        log = str(git.log("--oneline", _cwd=git_repo))
        assert "temp: a.py:" in log
        # b.py must remain unstaged in the working tree
        unstaged = str(git.diff("--name-only", _cwd=git_repo))
        assert "b.py" in unstaged

    def test_rebase_in_progress_exits_nonzero(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        commit_test_file("f.py", "a = 1\n")
        (git_repo / "f.py").write_text("a = 99\n")
        git.add("f.py", _cwd=git_repo)

        # Simulate a rebase in progress by creating the state directory git checks for.
        (git_repo / ".git" / "rebase-merge").mkdir()

        with pytest.raises(RebaseInProgressError):
            slice_command(
                paths=[],
                dry_run=False,
                all_changes=False,
                split_context=4,
                from_commit=None,
            )

    def test_from_option_resets_to_parent_and_slices(
        self, git_repo: Path, commit_test_file: Callable[[str, str], None]
    ) -> None:
        # Create two commits on top of init; --from will squash them back into staging.
        commit_test_file("a.py", "a = 1\n")
        commit_test_file("b.py", "b = 1\n")

        log = str(git.log("--reverse", "--format=%H", "HEAD~2..HEAD", _cwd=git_repo)).strip().splitlines()
        first_sha = log[0]
        parent_sha = str(git("rev-parse", f"{first_sha}^", _cwd=git_repo)).strip()

        # No explicit staging needed: --from squashes first_sha..HEAD into the index.
        slice_command(
            paths=[],
            dry_run=False,
            all_changes=False,
            split_context=4,
            from_commit=first_sha,
        )

        from git_curate.common import find_slice_base

        # find_slice_base() must return the parent of the from commit
        assert find_slice_base() == parent_sha
        # Every commit above parent_sha must be curate-authored
        author_emails = str(git.log("--format=%ae", f"{parent_sha}..HEAD", _cwd=git_repo)).strip()
        for email in author_emails.splitlines():
            assert email.strip() == "git-curate@local"
