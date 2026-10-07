"""Requirements-focused simulations; no project worktrees are created."""

from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api_agent.runtime import worktrees as gate


class GitSimulation:
    def __init__(self, repositories, timestamps, candidates=None, ignored=(), missing=(), broken=()):
        self.repositories = repositories
        self.timestamps = timestamps
        self.candidates = candidates or {}
        self.ignored = ignored
        self.missing = missing
        self.broken = broken
        self.calls = []

    def git(self, root, *args, **kwargs):
        root = str(root)
        self.calls.append((root, args, kwargs))
        if root in self.broken:
            raise gate.WorktreeGateError("missing checkout")
        if args == ("rev-parse", "--show-toplevel"):
            owner = next((r for roots in self.repositories.values() for r in roots if root == r or root.startswith(r + "/")), None)
            if owner is None:
                raise gate.WorktreeGateError("not a repository")
            return os.fsencode(owner) + b"\n"
        if args == ("rev-parse", "--path-format=absolute", "--git-common-dir"):
            roots = next(roots for roots in self.repositories.values() if root in roots)
            return os.fsencode(roots[0] + "/.git") + b"\n"
        if args == ("worktree", "list", "--porcelain", "-z"):
            roots = next(roots for roots in self.repositories.values() if root in roots)
            return b"".join(b"worktree " + os.fsencode(r) + b"\0HEAD abc\0" +
                            (b"detached\0" if i else b"branch refs/heads/main\0") + b"\0"
                            for i, r in enumerate(roots))
        if args == ("rev-parse", "--verify", "HEAD"):
            return b"abc123\n"
        if args == ("show", "-s", "--format=%ct", "HEAD"):
            return str(self.timestamps[root]).encode() + b"\n"
        if args[0] in ("diff", "ls-files"):
            kind = "unstaged" if args[0] == "diff" else "untracked"
            return b"".join(os.fsencode(p) + b"\0" for p in self.candidates.get((root, kind), ()))
        if args[0] == "check-ignore":
            raw = kwargs["input"][:-1]
            return raw + b"\0" if (root, os.fsdecode(raw)) in self.ignored else b""
        raise AssertionError(args)

    def stat(self, path):
        if str(path) in self.missing:
            raise FileNotFoundError(str(path))
        return SimpleNamespace(st_mtime=self.timestamps[str(path)])


class WorktreeTests(unittest.TestCase):
    def inspect(self, sim, roots):
        with patch.object(gate.shutil, "which", return_value="/usr/bin/git"), patch.object(gate, "_git", side_effect=sim.git), patch.object(Path, "lstat", lambda path: sim.stat(path)):
            return gate.inspect_worktrees([Path(r) for r in roots])

    def test_separate_repositories_and_nested_deduplication(self):
        sim = GitSimulation({"base": ["/base", "/base-new"], "pack": ["/pack", "/pack-other"]},
                            {"/base": 10, "/base-new": 20, "/pack": 30, "/pack-other": 20})
        reports = self.inspect(sim, ["/base", "/base/demo", "/pack"])
        self.assertEqual(len(reports), 2)
        self.assertTrue(reports[0].stale)
        self.assertFalse(reports[1].stale)
        rendered = gate.format_report(reports)
        self.assertIn("SELECTED", rendered)
        self.assertIn("NEWEST", rendered)
        self.assertIn("detached HEAD", rendered)
        self.assertIn("abc123", rendered)
        self.assertIn("UTC", rendered)

    def test_dirty_candidates_ignore_staged_only_and_missing_sources(self):
        strange = 'quote " and newline\n\\file'
        sim = GitSimulation({"base": ["/base", "/other"]},
                            {"/base": 10, "/other": 20, "/base/ignored": 1000, "/base/renamed": 25,
                             "/base/" + strange: 30, "/base/staged-only": 5000},
                            {("/base", "unstaged"): ["ignored", "deleted", "source", "renamed", strange],
                             ("/base", "untracked"): [strange]},
                            ignored=[("/base", "ignored")], missing=["/base/deleted", "/base/source"])
        report = self.inspect(sim, ["/base"])[0]
        selected = report.checkouts[0]
        self.assertEqual(selected.dirty_timestamp, 30)
        self.assertEqual(selected.dirty_path, strange)
        self.assertTrue(selected.newest)
        self.assertEqual(selected.missing_paths, ("deleted", "source"))
        self.assertIn("no recoverable deletion timestamp", gate.format_report([report]))
        self.assertTrue(all(c[2].get("input", b"").endswith(b"\0") for c in sim.calls if c[1][0] == "check-ignore"))

    def test_ties_and_commit_newer_than_file(self):
        sim = GitSimulation({"base": ["/base", "/other"]},
                            {"/base": 40, "/other": 40, "/base/mixed": 30},
                            {("/base", "unstaged"): ["mixed"]})
        report = self.inspect(sim, ["/base"])[0]
        self.assertFalse(report.stale)
        self.assertTrue(all(c.newest for c in report.checkouts))
        self.assertEqual(report.checkouts[0].reason, "HEAD committer timestamp")

    def test_missing_git_invalid_root_and_missing_registered_checkout(self):
        with patch.object(gate.shutil, "which", return_value=None):
            with self.assertRaisesRegex(gate.WorktreeGateError, "Git CLI"):
                gate.inspect_worktrees([Path("/base")])
        sim = GitSimulation({"base": ["/base", "/missing"]}, {"/base": 10}, broken=["/missing"])
        with self.assertRaisesRegex(gate.WorktreeGateError, "Registered runnable checkout /missing"):
            self.inspect(sim, ["/base"])
        with self.assertRaisesRegex(gate.WorktreeGateError, "not a repository"):
            self.inspect(sim, ["/unrelated"])

    def test_stale_explicit_y_only_and_fresh_never_reads(self):
        checkout = gate.CheckoutReport(Path("/base"), "main", "abc", 10, None, None, (), True)
        stale = (gate.RepositoryReport(Path("/base"), (checkout,)),)
        with patch.object(gate, "inspect_worktrees", return_value=stale):
            for stdin in [StringIO("n\n"), StringIO(""), StringIO("yes\n"), None, False]:
                with self.subTest(stdin=stdin), self.assertRaises(gate.WorktreeGateError):
                    gate.verify_worktrees([Path("/base")], stdin=stdin, stdout=StringIO())
            gate.verify_worktrees([Path("/base")], stdin=StringIO("y\n"), stdout=StringIO())
        current = (gate.RepositoryReport(Path("/base"), (gate.replace(checkout, newest=True),)),)
        with patch.object(gate, "inspect_worktrees", return_value=current):
            gate.verify_worktrees([Path("/base")], stdin=False, stdout=StringIO())

    def test_git_uses_argument_arrays_and_byte_capture(self):
        with patch.dict(os.environ, {"GIT_DIR": "/foreign/.git", "GIT_CONFIG_GLOBAL": "/host/gitconfig"}), patch.object(gate.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"x\0", stderr=b"")) as run:
            self.assertEqual(gate._git(Path('/space " path'), "status", "-z"), b"x\0")
            self.assertEqual(run.call_args.args[0], ["git", "-C", '/space " path', "status", "-z"])
            self.assertTrue(run.call_args.kwargs["capture_output"])
            self.assertNotIn("shell", run.call_args.kwargs)
            self.assertNotIn("GIT_DIR", run.call_args.kwargs["env"])
            self.assertEqual(run.call_args.kwargs["env"]["GIT_CONFIG_GLOBAL"], "/host/gitconfig")

    def test_discovered_root_must_contain_supplied_path(self):
        with patch.object(gate.shutil, "which", return_value="git"), patch.object(gate, "_git", return_value=b"/foreign\n"):
            with self.assertRaisesRegex(gate.WorktreeGateError, "does not belong"):
                gate.inspect_worktrees([Path("/expected")])

    def test_nul_records_bare_locked_and_prunable(self):
        raw_path = b'/space "\\\xff\npath'
        records = gate._records(b"worktree /admin\0bare\0\0worktree " + raw_path +
                                b"\0HEAD abc\0detached\0locked reason\0prunable missing\0\0")
        self.assertIn(b"bare", records[0])
        self.assertEqual(records[1][b"worktree"], raw_path)
        self.assertEqual(records[1][b"locked"], b"reason")
        self.assertEqual(records[1][b"prunable"], b"missing")
        sim = GitSimulation({"base": ["/base", "/missing"]}, {"/base": 10}, broken=["/missing"])
        original = sim.git
        def git(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args == ("worktree", "list", "--porcelain", "-z"):
                result = result.replace(b"worktree /missing\0", b"worktree /missing\0locked in use\0prunable missing\0")
            return result
        sim.git = git
        with self.assertRaisesRegex(gate.WorktreeGateError, "locked='in use'; prunable='missing'"):
            self.inspect(sim, ["/base"])

    def test_bare_entry_excluded_without_inspection(self):
        sim = GitSimulation({"base": ["/base"]}, {"/base": 10})
        original = sim.git
        def git(root, *args, **kwargs):
            result = original(root, *args, **kwargs)
            if args == ("worktree", "list", "--porcelain", "-z"):
                return b"worktree /administrative\0bare\0\0" + result
            return result
        sim.git = git
        reports = self.inspect(sim, ["/base"])
        self.assertTrue(reports[0].checkouts[0].bare)
        self.assertIsNone(reports[0].checkouts[0].timestamp)
        self.assertFalse(reports[0].stale)
        self.assertIn("administrative bare entry; excluded", gate.format_report(reports))
        self.assertFalse(any(root == "/administrative" for root, _, _ in sim.calls))

    @unittest.skipUnless(shutil.which("git"), "requires Git CLI")
    def test_inherited_repository_environment_cannot_redirect_launcher(self):
        with tempfile.TemporaryDirectory(prefix="gate environment ") as directory:
            expected = Path(directory) / "expected"
            foreign = Path(directory) / "foreign"
            for root in (expected, foreign):
                root.mkdir()
                subprocess.run(["git", "-C", str(root), "init"], capture_output=True, check=True)
                subprocess.run(["git", "-C", str(root), "-c", "user.name=Gate Test",
                                "-c", "user.email=gate@example.invalid", "commit", "--allow-empty",
                                "-m", "initial"], capture_output=True, check=True)
            nested = expected / "demo"
            nested.mkdir()
            overrides = {
                "GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign),
                "GIT_COMMON_DIR": str(foreign / ".git"),
                "GIT_INDEX_FILE": str(foreign / ".git" / "index"),
                "GIT_OBJECT_DIRECTORY": str(foreign / ".git" / "objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/nonexistent/objects",
                "GIT_NAMESPACE": "foreign", "GIT_PREFIX": "foreign/",
                "GIT_CEILING_DIRECTORIES": directory, "GIT_DISCOVERY_ACROSS_FILESYSTEM": "0",
            }
            with patch.dict(os.environ, overrides):
                reports = gate.inspect_worktrees([expected, nested])
            self.assertEqual(len(reports), 1)
            self.assertEqual(reports[0].selected_root, expected.resolve())
            self.assertEqual(reports[0].checkouts[0].root, expected.resolve())

    @unittest.skipUnless(shutil.which("git"), "requires Git CLI")
    def test_stale_registration_reused_by_unrelated_repository_fails(self):
        with tempfile.TemporaryDirectory(prefix="gate repositories ") as directory:
            selected = Path(directory) / "selected"
            unrelated = Path(directory) / "reused"
            for root in (selected, unrelated):
                root.mkdir()
                subprocess.run(["git", "-C", str(root), "init"], capture_output=True, check=True)
                subprocess.run(["git", "-C", str(root), "-c", "user.name=Gate Test",
                                "-c", "user.email=gate@example.invalid", "commit", "--allow-empty",
                                "-m", "initial"], capture_output=True, check=True)
            registration = selected / ".git" / "worktrees" / "stale"
            registration.mkdir(parents=True)
            (registration / "gitdir").write_text(str(unrelated / ".git") + "\n")
            (registration / "commondir").write_text("../..\n")
            (registration / "HEAD").write_bytes(subprocess.run(
                ["git", "-C", str(selected), "rev-parse", "HEAD"],
                capture_output=True, check=True).stdout)
            with self.assertRaisesRegex(gate.WorktreeGateError, "unrelated repository"):
                gate.inspect_worktrees([selected])

    @unittest.skipUnless(shutil.which("git"), "requires Git CLI")
    def test_real_temporary_repository_paths_and_ignores(self):
        with tempfile.TemporaryDirectory(prefix="gate space ") as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", "-C", directory, *args], capture_output=True, check=True)
            git("init")
            git("config", "user.name", "Gate Test")
            git("config", "user.email", "gate@example.invalid")
            (root / ".gitignore").write_text("ignored\n")
            (root / "ignored").write_text("tracked but ignored")
            (root / "staged").write_text("initial")
            git("add", ".gitignore", "staged")
            git("add", "-f", "ignored")
            git("commit", "-m", "initial")
            (root / "ignored").write_text("new ignored")
            (root / "staged").write_text("staged only")
            git("add", "staged")
            odd = root / 'quote " slash\\ newline\n'
            odd.write_text("untracked")
            commit = int(git("show", "-s", "--format=%ct").stdout)
            os.utime(odd, (commit + 10, commit + 10))
            os.utime(root / "ignored", (commit + 100, commit + 100))
            report = gate.inspect_worktrees([root, root / "."])[0]
            self.assertEqual(report.checkouts[0].dirty_path, odd.name)
            self.assertEqual(report.checkouts[0].dirty_timestamp, commit + 10)
            gate.verify_worktrees([root], stdin=False, stdout=StringIO())


if __name__ == "__main__":
    unittest.main()
