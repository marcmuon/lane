"""Held-out acceptance for --context, --holdout/--protect and --prewalk.

Written by the orchestrator, not by the lane that builds these features; that lane must not edit this file.
Run: python3 -m unittest discover -s tests -p 'test_lane_heldout.py'
"""

import json
import subprocess
import unittest
from pathlib import Path

import test_lane


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class HeldoutFeatures(test_lane.LaneTest):
    def commit_all(self, repo, msg):
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", msg)
        return git(repo, "rev-parse", "HEAD")

    def wt_lane(self, repo, *extra, **env):
        return self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--sandbox", "workspace-write",
                         "--worktree", *extra, **env)

    def brief_of(self, r):
        return (Path(r["run_dir"]) / "brief.md").read_text()

    # --- context ---
    def test_context_is_named_by_its_working_copy_path(self):
        repo = self.git_repo()
        (repo / "CONTEXT.md").write_text("goals: compound wealth\n")
        self.commit_all(repo, "context")
        rc, r = self.wt_lane(repo, "--context", str(repo / "CONTEXT.md"))
        self.assertEqual((rc, r["status"]), (0, "ok"))
        brief = self.brief_of(r)
        wt = Path(r["worktree"])
        self.assertTrue(str(wt / "CONTEXT.md") in brief or str(wt.resolve() / "CONTEXT.md") in brief, brief)

    def test_context_is_inlined_for_answer_only(self):
        ctx = self.tmp / "ctx.md"
        ctx.write_text("UNIQUE-CONTEXT-7731\n")
        dump = self.tmp / "dump.json"
        rc, r = self.lane("run", "--brief", str(self.brief), "--answer-only", "--model", "gpt-6-astra",
                          "--context", str(ctx), FAKE_CODEX_DUMP=dump)
        self.assertEqual((rc, r["status"]), (0, "ok"))
        self.assertIn("UNIQUE-CONTEXT-7731", json.loads(dump.read_text())["prompt"])

    def test_missing_context_is_a_usage_error(self):
        rc, r = self.run_lane("--context", str(self.tmp / "nope.md"))
        self.assertEqual(r["status"], "usage_error")

    # --- holdout and protect ---
    def test_holdout_runs_in_the_working_copy_after_the_worker(self):
        repo = self.git_repo()
        rc, r = self.wt_lane(repo, "--holdout", "test -f marker.txt", FAKE_CODEX_CLOBBER="marker.txt")
        self.assertEqual((rc, r["status"]), (0, "ok"))
        self.assertEqual((r["holdout"]["passed"], r["holdout"]["exit_code"]), (True, 0))
        rc, r = self.wt_lane(repo, "--holdout", "test -f marker.txt")
        self.assertEqual(r["status"], "ok")
        self.assertFalse(r["holdout"]["passed"])
        self.assertNotEqual(r["holdout"]["exit_code"], 0)

    def test_editing_a_protected_path_fails_the_holdout(self):
        repo = self.git_repo()
        (repo / "tests").mkdir()
        (repo / "tests" / "heldout.txt").write_text("original\n")
        self.commit_all(repo, "heldout")
        _, r = self.wt_lane(repo, "--holdout", "true", "--protect", "tests/heldout.txt",
                            FAKE_CODEX_CLOBBER="tests/heldout.txt")
        self.assertFalse(r["holdout"]["passed"])
        self.assertEqual(r["holdout"]["tampered"], ["tests/heldout.txt"])
        _, r = self.wt_lane(repo, "--holdout", "true", "--protect", "tests/heldout.txt")
        self.assertEqual((r["holdout"]["passed"], r["holdout"]["tampered"]), (True, []))

    def test_worker_is_told_the_holdout_and_protected_paths(self):
        repo = self.git_repo()
        _, r = self.wt_lane(repo, "--holdout", "uv run pytest tests/heldout -q", "--protect", "tests/heldout")
        brief = self.brief_of(r)
        self.assertIn("uv run pytest tests/heldout -q", brief)
        self.assertIn("tests/heldout", brief)

    def test_no_holdout_means_no_holdout_field_value(self):
        _, r = self.run_lane()
        self.assertIsNone(r.get("holdout"))

    # --- prewalk ---
    def test_prewalk_names_the_commit_its_files_and_the_rule(self):
        repo = self.git_repo()
        (repo / "exemplar.py").write_text("x = 1\n")
        sha = self.commit_all(repo, "exemplar: first case done")
        _, r = self.wt_lane(repo, "--prewalk", sha)
        brief = self.brief_of(r)
        self.assertIn(sha[:7], brief)
        self.assertIn("exemplar.py", brief)
        self.assertIn("settled design decision", brief)

    def test_unknown_prewalk_is_a_usage_error(self):
        repo = self.git_repo()
        rc, r = self.wt_lane(repo, "--prewalk", "deadbeefdeadbeef")
        self.assertEqual(r["status"], "usage_error")

    # --- retries keep the checks ---
    def test_retry_of_carries_holdout_protect_context_and_prewalk(self):
        repo = self.git_repo()
        (repo / "CONTEXT.md").write_text("ctx\n")
        (repo / "exemplar.py").write_text("x = 1\n")
        sha = self.commit_all(repo, "exemplar")
        _, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--profile", "implement-light",
                         "--context", str(repo / "CONTEXT.md"), "--holdout", "true", "--protect", "exemplar.py",
                         "--prewalk", sha)
        self.assertEqual(r["status"], "ok")
        _, r2 = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--retry-of", r["lane_id"])
        self.assertEqual(r2["status"], "ok")
        self.assertEqual(r2["holdout"]["passed"], True)
        brief = self.brief_of(r2)
        self.assertIn("CONTEXT.md", brief)
        self.assertIn(sha[:7], brief)
        self.assertIn("exemplar.py", brief)


def load_tests(loader, tests, pattern):
    """Run only the tests defined here, not the LaneTest suite this class inherits."""
    return unittest.TestSuite(HeldoutFeatures(n) for n in HeldoutFeatures.__dict__ if n.startswith("test_"))
