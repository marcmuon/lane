"""Held-out acceptance for trajectory prewalk (--prewalk SHA --prewalk-plan FILE).

The orchestrator explores, writes a short todo list with a check per item, and commits the first edit. Lane hands
that over as the Codex worker's own session history: the files read (pre-edit contents), the todo list in the plan
tool, the edit as an apply_patch call and the first check's real output. The worker resumes that session.
Written by the orchestrator; the lane that builds the feature must not edit this file.
"""

import json
import subprocess
import unittest
from pathlib import Path

import test_lane


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class PrewalkTrajectory(test_lane.LaneTest):
    def setup_prewalk(self, steps=2, first_check="true"):
        repo = self.git_repo()
        (repo / "shapes.py").write_text("def area(r):\n    return r * r\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "base")
        (repo / "shapes.py").write_text("def area(r):\n    if r < 0:\n        raise ValueError('negative')\n    return r * r\n")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "prewalk: area rejects negatives")
        sha = git(repo, "rev-parse", "HEAD")
        plan = self.tmp / "plan.json"
        items = [{"step": "area rejects negatives", "check": first_check}]
        items += [{"step": f"item {i}", "check": "true"} for i in range(2, steps + 1)]
        plan.write_text(json.dumps(items))
        return repo, sha, plan

    def run_prewalk(self, repo, sha, plan, *extra, **env):
        return self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--sandbox", "workspace-write",
                         "--worktree", "--prewalk", sha, "--prewalk-plan", str(plan), *extra, **env)

    def test_worker_resumes_a_session_holding_the_prewalk_as_its_own_steps(self):
        repo, sha, plan = self.setup_prewalk()
        rc, r = self.run_prewalk(repo, sha, plan)
        self.assertEqual((rc, r["status"]), (0, "ok"))
        self.assertEqual(r["prewalk"]["mode"], "trajectory")
        run = Path(r["run_dir"])
        argv = json.loads((run / "argv.json").read_text())["argv"]
        self.assertIn("resume", argv)
        session = (run / "prewalk_session.jsonl").read_text()
        for expected in ("Do the bounded thing.",       # the brief
                         "return r * r",                 # pre-edit read
                         "raise ValueError",             # the first edit
                         "apply_patch", "update_plan",   # as the worker's own tool calls
                         "area rejects negatives", "item 2"):
            self.assertIn(expected, session)
        # the worker must not be told someone else planned or edited for it
        self.assertNotIn("settled design decision", session)
        self.assertNotIn("PREWALK:", session)

    def test_task_lanes_get_the_todo_tool_and_an_item_cap(self):
        _, r = self.run_lane()
        argv = json.loads((Path(r["run_dir"]) / "argv.json").read_text())["argv"]
        self.assertIn("tools.update_plan.enabled=true", argv)
        brief = (Path(r["run_dir"]) / "brief.md").read_text()
        self.assertIn("todo", brief.lower())
        self.assertIn("8", brief)

    def test_more_than_eight_items_is_a_usage_error(self):
        repo, sha, plan = self.setup_prewalk(steps=9)
        _, r = self.run_prewalk(repo, sha, plan)
        self.assertEqual(r["status"], "usage_error")

    def test_plan_without_a_prewalk_commit_is_a_usage_error(self):
        repo, _, plan = self.setup_prewalk()
        _, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--sandbox", "workspace-write",
                         "--worktree", "--prewalk-plan", str(plan))
        self.assertEqual(r["status"], "usage_error")

    def test_a_failing_first_check_stops_before_the_worker_starts(self):
        repo, sha, plan = self.setup_prewalk(first_check="false")
        _, r = self.run_prewalk(repo, sha, plan)
        self.assertEqual(r["status"], "usage_error")

    def test_claude_worker_falls_back_to_brief_mode(self):
        repo, sha, plan = self.setup_prewalk()
        rc, r = self.run_prewalk(repo, sha, plan, "--provider", "claude", "--model", "opus")
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["prewalk"]["mode"], "brief")
        brief = (Path(r["run_dir"]) / "brief.md").read_text()
        self.assertIn(sha[:7], brief)
        self.assertIn("area rejects negatives", brief)

    def test_retry_keeps_the_trajectory(self):
        repo, sha, plan = self.setup_prewalk()
        _, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--profile", "implement-light",
                         "--prewalk", sha, "--prewalk-plan", str(plan))
        self.assertEqual(r["prewalk"]["mode"], "trajectory")
        _, r2 = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--retry-of", r["lane_id"])
        self.assertEqual((r2["status"], r2["prewalk"]["mode"]), ("ok", "trajectory"))


def load_tests(loader, tests, pattern):
    """Run only the tests defined here, not the LaneTest suite this class inherits."""
    return unittest.TestSuite(PrewalkTrajectory(n) for n in PrewalkTrajectory.__dict__ if n.startswith("test_"))
