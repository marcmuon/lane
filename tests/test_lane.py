"""lane tests. Run: python3 -m unittest discover -s tests -v   (no Codex quota: codex is faked)."""

import json
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
LANE = ROOT / "lane"
FAKE = ROOT / "tests" / "fake_codex.py"
FAKE_CLAUDE = ROOT / "tests" / "fake_claude.py"
LANE_LOADER = importlib.machinery.SourceFileLoader("lane_module", str(LANE))
LANE_SPEC = importlib.util.spec_from_loader(LANE_LOADER.name, LANE_LOADER)
LANE_MODULE = importlib.util.module_from_spec(LANE_SPEC)
LANE_LOADER.exec_module(LANE_MODULE)


class LaneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="lane-test-"))
        self.home = self.tmp / "codex-home"
        self.home.mkdir()
        (self.home / "auth.json").write_text("{}")
        self.brief = self.tmp / "brief.md"
        self.brief.write_text("Do the bounded thing.\n")
        self.work = self.tmp / "work"
        self.work.mkdir()
        base = {k: v for k, v in os.environ.items() if not k.startswith("LANE_")}   # never touch real lane homes
        self.env = dict(base, LANE_STATE=str(self.tmp / "state"), LANE_CODEX_BIN=str(FAKE),
                        LANE_CODEX_HOME=str(self.home), LANE_WORKTREE_ROOT=str(self.tmp / "worktrees"),
                        LANE_CLAUDE_BIN=str(FAKE_CLAUDE))

    def lane(self, *args, **env):
        e = dict(self.env, **{k: str(v) for k, v in env.items()})
        p = subprocess.run([sys.executable, str(LANE), *args], capture_output=True, text=True, env=e, timeout=120)
        lines = [l for l in p.stdout.splitlines() if l.strip()]
        self.assertEqual(len(lines), 1, f"expected one JSON line, got {p.stdout!r} {p.stderr!r}")
        return p.returncode, json.loads(lines[0])

    def run_lane(self, *extra, **env):
        return self.lane("run", "--brief", str(self.brief), "--cwd", str(self.work), *extra, **env)

    def seed_quota(self, used, resets=None):
        d = self.home / "sessions" / "2026" / "09" / "01"
        d.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 60))
        (d / "rollout-seed-0000.jsonl").write_text(json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {"limit_id": "codex", "plan_type": "pro", "primary": {
                "used_percent": used, "resets_at": resets or int(time.time()) + 3 * 86400}}}}) + "\n")

    # --- outcomes ---
    def test_ok_task(self):
        rc, r = self.run_lane("--model", "gpt-6-luna", "--effort", "max")
        self.assertEqual((rc, r["status"]), (0, "ok"))
        self.assertEqual(r["marker"], "STATUS: DONE")
        self.assertEqual((r["model_verified"], r["effort_recorded"]), ("gpt-6-luna", "max"))
        self.assertTrue(Path(r["report_path"]).exists())
        self.assertIn("STATUS: DONE", r["summary"])
        brief = (Path(r["run_dir"]) / "brief.md").read_text()
        self.assertIn("OUTPUT CONTRACT", brief)

    def test_review_verdict(self):
        rc, r = self.run_lane("--kind", "review", FAKE_CODEX_MODE="ok_review")
        self.assertEqual((rc, r["status"], r["marker"]), (0, "ok", "VERDICT: REVISE"))

    def test_long_reports_reach_the_caller_without_a_lane_length_cap(self):
        self.brief.write_text("Goal: investigate the issue thoroughly and report all findings and evidence.\n")
        report = self.tmp / "worker-report.md"
        for provider, model in (("openai", "gpt-6-astra"), ("claude", "opus")):
            for kind, marker in (("task", "STATUS: DONE"), ("review", "VERDICT: REVISE")):
                with self.subTest(provider=provider, kind=kind):
                    text = marker + "\n" + "Detailed findings and acceptance evidence.\n" * 250 + "COMPLETE REPORT END"
                    report.write_text(text)
                    rc, r = self.run_lane("--provider", provider, "--model", model, "--kind", kind,
                                          FAKE_REPORT_FILE=report)
                    self.assertEqual((rc, r["status"], r["marker"]), (0, "ok", marker))
                    self.assertEqual(Path(r["report_path"]).read_text(), text)
                    self.assertEqual(r["summary"], text)
                    self.assertEqual(r["report_words"], len(text.split()))
                    brief = (Path(r["run_dir"]) / "brief.md").read_text()
                    self.assertNotIn("at most 150 words", brief)
                    rc, saved = self.lane("result", r["lane_id"])
                    self.assertEqual((rc, saved["summary"]), (0, text))

    def test_capped_then_preflight_refuses(self):
        rc, r = self.run_lane(FAKE_CODEX_MODE="capped")
        self.assertEqual((rc, r["status"]), (1, "capped"))
        self.assertTrue((self.home / "CAPPED").exists())
        rc, r = self.run_lane()
        self.assertEqual((rc, r["status"]), (5, "capped_preflight"))

    def test_auth(self):
        self.assertEqual(self.run_lane(FAKE_CODEX_MODE="auth")[1]["status"], "auth_401")

    def test_contract_and_report(self):
        self.assertEqual(self.run_lane(FAKE_CODEX_MODE="nomarker")[1]["status"], "contract_violation")
        self.assertEqual(self.run_lane(FAKE_CODEX_MODE="noreport")[1]["status"], "no_report")

    def test_o_missing_falls_back_to_event_stream(self):
        rc, r = self.run_lane(FAKE_CODEX_MODE="o_missing")
        self.assertEqual((rc, r["status"]), (0, "ok"))
        self.assertIn("from the event stream", r["summary"])

    def test_reconnect_error_is_not_fatal(self):
        self.assertEqual(self.run_lane(FAKE_CODEX_MODE="reconnect_then_ok")[1]["status"], "ok")

    def test_timeout(self):
        rc, r = self.run_lane("--max-time", "1s", FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=20)
        self.assertEqual((rc, r["status"]), (1, "timeout"))

    # --- detach / wait ---
    def test_start_is_detached_and_wait_polls(self):
        rc, s = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work),
                          FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=4)
        self.assertEqual((rc, s["status"]), (0, "started"))
        rc, w = self.lane("wait", s["lane_id"], "--max", "0.5")
        self.assertEqual((rc, w["status"]), (3, "running"))
        rc, w = self.lane("wait", s["lane_id"], "--max", "60")
        self.assertEqual((rc, w["status"]), (0, "ok"))

    def test_slot_limit(self):
        _, s = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work),
                         FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=4, LANE_MAX_CONCURRENT=1)
        rc, b = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work), LANE_MAX_CONCURRENT=1)
        self.assertEqual((rc, b["status"]), (4, "busy"))
        self.lane("wait", s["lane_id"], "--max", "60")

    # --- quota / budget ---
    def test_account_homes_with_symlinked_auth(self):
        account_home = self.tmp / "account-home"
        account_home.mkdir()
        (account_home / "auth.json").write_text("{}")
        (self.home / "auth.json").unlink()
        (self.home / "auth.json").symlink_to(account_home / "auth.json")

        self.assertEqual(LANE_MODULE.account_homes(str(self.home)), [str(self.home), str(account_home.resolve())])

    def test_account_homes_with_plain_auth(self):
        self.assertEqual(LANE_MODULE.account_homes(str(self.home)), [str(self.home)])

    def test_latest_rate_limit_uses_newest_codex_sample_across_homes(self):
        account_home = self.tmp / "account-home"
        account_home.mkdir()
        (account_home / "auth.json").write_text("{}")
        (self.home / "auth.json").unlink()
        (self.home / "auth.json").symlink_to(account_home / "auth.json")

        def event(timestamp, limit_id, used_percent):
            return {"timestamp": timestamp, "type": "event_msg", "payload": {
                "type": "token_count", "rate_limits": {"limit_id": limit_id, "plan_type": "pro",
                    "primary": {"used_percent": used_percent, "resets_at": 123}}}}

        def write_rollout(home, name, *events):
            folder = home / "sessions" / "2026" / "09" / "27"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text("".join(json.dumps(e) + "\n" for e in events))

        write_rollout(self.home, "rollout-lane.jsonl",
                      event("2026-09-25T10:00:00.000Z", "codex", 40),
                      event("2026-09-26T10:00:00.000Z", "other", 99))
        write_rollout(account_home, "rollout-account.jsonl",
                      event("2026-09-26T12:00:00.000Z", "codex", 55),
                      event("2026-09-27T10:00:00.000Z", "other", 88))

        self.assertEqual(LANE_MODULE.latest_rate_limit(str(self.home)), {
            "used_percent": 55, "resets_at": 123, "plan_type": "pro", "ts": "2026-09-26T12:00:00.000Z"})

    def test_quota_stop_task_but_not_review(self):
        self.seed_quota(90)
        self.assertEqual(self.run_lane()[1]["status"], "capped_preflight")
        rc, r = self.run_lane("--kind", "review", FAKE_CODEX_MODE="ok_review", FAKE_USED=90)
        self.assertEqual(r["status"], "ok")

    def test_quota_delta_and_budget(self):
        resets = int(time.time()) + 3 * 86400
        self.seed_quota(40, resets=resets)
        rc, r = self.run_lane(FAKE_USED=40, FAKE_DELTA=2, FAKE_RESETS=resets)
        self.assertEqual((r["quota_before"], r["quota_after"], r["quota_delta"]), (40, 42, 2))
        rc, q = self.lane("quota")
        self.assertEqual(q["lane_points_this_window"], 2)
        rc, b = self.run_lane(LANE_WEEKLY_POINTS=2)
        self.assertEqual((rc, b["status"]), (5, "budget_spent"))

    # --- worktrees / one writer ---
    def git_repo(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        for cmd in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *cmd], check=True)
        return repo

    def test_worktree(self):
        repo = self.git_repo()
        rc, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--sandbox", "workspace-write",
                          "--worktree")
        self.assertEqual(r["status"], "ok")
        self.assertTrue(Path(r["worktree"]).is_dir())
        self.assertTrue(r["branch"].startswith("lane/"))
        self.assertEqual(Path(r["cwd"]).resolve(), Path(r["worktree"]).resolve())
        brief = (Path(r["run_dir"]) / "brief.md").read_text()
        self.assertIn(f"WORKING COPY: you are in {r['cwd']}", brief)   # not the Repo: path the brief names
        self.assertLess(brief.index("WORKING COPY"), brief.index("OUTPUT CONTRACT"))

    def test_one_writer_per_checkout(self):
        repo = self.git_repo()
        _, s = self.lane("start", "--brief", str(self.brief), "--cwd", str(repo), "--sandbox", "workspace-write",
                         FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=4)
        rc, b = self.lane("start", "--brief", str(self.brief), "--cwd", str(repo), "--sandbox", "workspace-write")
        self.assertEqual((rc, b["status"]), (4, "busy_worktree"))
        self.lane("wait", s["lane_id"], "--max", "60")

    # --- context, holdout and prewalk edge cases ---
    def test_repeated_context_is_recorded_and_not_inlined_for_coding(self):
        first, second = self.tmp / "goals.md", self.tmp / "rules.md"
        first.write_text("CONTEXT GOALS CONTENT\n")
        second.write_text("CONTEXT RULES CONTENT\n")
        rc, r = self.run_lane("--context", str(first), "--context", str(second))
        self.assertEqual((rc, r["status"]), (0, "ok"))
        run = Path(r["run_dir"])
        params = json.loads((run / "params.json").read_text())
        self.assertEqual(params["context"], [str(first.resolve()), str(second.resolve())])
        brief = (run / "brief.md").read_text()
        self.assertLess(brief.index(str(first)), brief.index(str(second)))
        self.assertNotIn("CONTEXT GOALS CONTENT", brief)
        rc, r = self.run_lane("--context", str(self.work))
        self.assertEqual((rc, r["status"]), (2, "usage_error"))
        loop = self.tmp / "context-loop"
        loop.symlink_to(loop)
        rc, r = self.run_lane("--context", str(loop))
        self.assertEqual((rc, r["status"]), (2, "usage_error"))

    def test_answer_context_cap_counts_all_files_in_bytes(self):
        first, second = self.tmp / "first.md", self.tmp / "second.md"
        first.write_bytes(b"a" * (200 * 1024 - 8))
        second.write_text("\U0001f600\U0001f600", encoding="utf-8")
        for provider in ("openai", "claude"):
            dump = self.tmp / f"{provider}-context.json"
            rc, r = self.lane("run", "--brief", str(self.brief), "--answer-only", "--web-research",
                              "--provider", provider, "--context", str(first), "--context", str(second),
                              FAKE_CODEX_DUMP=dump, FAKE_CLAUDE_DUMP=dump)
            self.assertEqual((rc, r["status"]), (0, "ok"))
            prompt = json.loads(dump.read_text())["prompt"]
            self.assertIn(first.read_text(), prompt)
            self.assertIn(second.read_text(), prompt)
            self.assertLess(prompt.index(str(first)), prompt.index(str(second)))
            self.assertNotIn("OUTPUT CONTRACT", prompt)
        second.write_text("\U0001f600\U0001f600!", encoding="utf-8")
        rc, r = self.run_lane("--answer-only", "--context", str(first), "--context", str(second))
        self.assertEqual((rc, r["status"]), (2, "usage_error"))

    def test_worktree_context_includes_current_and_untracked_notes(self):
        repo = self.git_repo()
        goals = repo / "GOALS.md"
        goals.write_text("old goals\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "goals"], check=True)
        goals.write_text("current goals\n")
        decisions = repo / "notes" / "DECISIONS.md"
        decisions.parent.mkdir()
        decisions.write_text("current decisions\n")
        rc, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--worktree",
                          "--context", str(goals), "--context", str(decisions))
        self.assertEqual((rc, r["status"]), (0, "ok"))
        worktree = Path(r["worktree"])
        self.assertEqual((worktree / "GOALS.md").read_text(), goals.read_text())
        self.assertEqual((worktree / "notes" / "DECISIONS.md").read_text(), decisions.read_text())
        brief = (Path(r["run_dir"]) / "brief.md").read_text()
        self.assertIn(str(worktree / "notes" / "DECISIONS.md"), brief)
        self.assertEqual(goals.read_text(), "current goals\n")
        self.assertEqual(decisions.read_text(), "current decisions\n")

    def test_protected_directory_detects_worker_deletions_and_additions(self):
        folder = self.work / "checks"
        folder.mkdir()
        original = folder / "acceptance.txt"
        for action, name in (("FAKE_CODEX_REMOVE", "checks/acceptance.txt"),
                             ("FAKE_CODEX_CLOBBER", "checks/added.txt")):
            with self.subTest(action=action):
                original.write_text("original\n")
                rc, r = self.run_lane("--holdout", "true", "--protect", "checks", **{action: name})
                self.assertEqual((rc, r["status"]), (0, "ok"))
                self.assertEqual((r["holdout"]["exit_code"], r["holdout"]["passed"], r["holdout"]["tampered"]),
                                 (0, False, ["checks"]))

    def test_holdout_checks_deletion_and_new_protected_paths(self):
        original = self.work / "acceptance.txt"
        original.write_text("original\n")
        _, r = self.run_lane("--holdout", "rm acceptance.txt; touch missing.txt",
                             "--protect", "acceptance.txt", "--protect", "missing.txt")
        self.assertEqual((r["holdout"]["exit_code"], r["holdout"]["passed"], r["holdout"]["tampered"]),
                         (0, False, ["acceptance.txt", "missing.txt"]))

    def test_holdout_cannot_hide_worker_tampering_by_restoring_the_file(self):
        original = self.work / "acceptance.txt"
        original.write_text("original\n")
        _, r = self.run_lane("--holdout", "printf 'original\\n' > acceptance.txt", "--protect", "acceptance.txt",
                             FAKE_CODEX_CLOBBER="acceptance.txt")
        self.assertEqual(original.read_text(), "original\n")
        self.assertEqual((r["holdout"]["passed"], r["holdout"]["tampered"]), (False, ["acceptance.txt"]))

    def test_holdout_preserves_worker_failure_and_saves_full_output(self):
        command = "printf 'stdout\\n'; printf 'stderr\\n' >&2; exit 7"
        rc, r = self.run_lane("--holdout", command, FAKE_CODEX_MODE="nomarker")
        self.assertEqual((rc, r["status"]), (1, "contract_violation"))
        holdout = r["holdout"]
        self.assertEqual((holdout["cmd"], holdout["exit_code"], holdout["passed"], holdout["tampered"]),
                         (command, 7, False, []))
        self.assertEqual(Path(holdout["log"]).read_text(), "stdout\nstderr\n")
        rc, saved = self.lane("result", r["lane_id"])
        self.assertEqual((rc, saved["holdout"]), (1, holdout))

    def test_holdout_from_repo_subdirectory_uses_cwd_and_repo_relative_protection(self):
        repo = self.git_repo()
        (repo / "subdir").mkdir()
        (repo / "subdir" / "input.txt").write_text("input\n")
        (repo / "protected.txt").write_text("original\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "inputs"], check=True)
        _, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo / "subdir"), "--worktree",
                         "--holdout", "test -f input.txt && printf changed > ../protected.txt",
                         "--protect", "protected.txt")
        self.assertEqual(Path(r["cwd"]).resolve(), (Path(r["worktree"]) / "subdir").resolve())
        self.assertEqual((r["holdout"]["exit_code"], r["holdout"]["passed"], r["holdout"]["tampered"]),
                         (0, False, ["protected.txt"]))
        self.assertEqual((repo / "protected.txt").read_text(), "original\n")

    def test_holdout_timeout_stops_its_process_group(self):
        script = self.tmp / "slow_check.py"
        child_pid = self.tmp / "holdout-child.pid"
        script.write_text("import subprocess, sys, time\n"
                          "from pathlib import Path\n"
                          "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                          "Path(sys.argv[1]).write_text(str(child.pid))\n"
                          "print('partial output', flush=True)\n"
                          "time.sleep(60)\n")
        command = f"'{sys.executable}' '{script}' '{child_pid}'"
        with mock.patch.object(LANE_MODULE, "HOLDOUT_TIMEOUT_S", 0.5):
            result = LANE_MODULE.run_holdout(self.tmp, {"holdout": command, "cwd": str(self.work)}, True)
        self.assertEqual((result["exit_code"], result["passed"], result["tampered"]), (124, False, []))
        self.assertIn("partial output", Path(result["log"]).read_text())
        self.assertIn("timed out", Path(result["log"]).read_text())
        child = int(child_pid.read_text())
        deadline = time.time() + 3
        while LANE_MODULE.alive(child) and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(LANE_MODULE.alive(child))

    def test_invalid_protection_and_empty_holdout_are_usage_errors(self):
        (self.work / "outside").symlink_to(self.tmp, target_is_directory=True)
        for flags in (("--holdout", " "), ("--protect", "checks"),
                      ("--holdout", "true", "--protect", str(self.brief)),
                      ("--holdout", "true", "--protect", "../brief.md"),
                      ("--holdout", "true", "--protect", "outside/brief.md")):
            with self.subTest(flags=flags):
                rc, r = self.run_lane(*flags)
                self.assertEqual((rc, r["status"]), (2, "usage_error"))
        self.assertFalse(any((self.tmp / "state" / "runs").glob("*/params.json")))

    def test_prewalk_rejects_nonrepo_noncommit_and_nonancestor(self):
        rc, r = self.run_lane("--prewalk", "HEAD")
        self.assertEqual((rc, r["status"]), (2, "usage_error"))
        repo = self.git_repo()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        base = git("rev-parse", "HEAD")
        git("commit", "-q", "--allow-empty", "-m", "other branch")
        other = git("rev-parse", "HEAD")
        git("checkout", "-q", "--detach", base)
        tree = git("rev-parse", "HEAD^{tree}")
        for sha in (other, tree):
            rc, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--prewalk", sha)
            self.assertEqual((rc, r["status"]), (2, "usage_error"))
        rc, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--answer-only", "--prewalk", base)
        self.assertEqual((rc, r["status"]), (2, "usage_error"))

    def prewalk_fixture(self, first_check="printf 'checked\\n'"):
        repo = self.git_repo()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        (repo / "shape.py").write_text("\nvalue = 1\n\n")
        (repo / "legacy.py").write_text("legacy = True\n")
        (repo / "guide.md").write_text("Read this guidance.\n")
        git("add", "-A")
        git("commit", "-qm", "base")
        (repo / "shape.py").write_text("\nvalue = 2\n\n")
        (repo / "legacy.py").unlink()
        (repo / "new 'file'.py").write_text("added = True\n")
        (repo / "empty.txt").touch()
        git("add", "-A")
        git("commit", "-qm", "first edit")
        plan = self.tmp / "todo.json"
        items = [{"step": "Apply the first edit", "check": first_check},
                 {"step": "Finish the remaining cases", "check": "true"}]
        plan.write_text(json.dumps(items))
        return repo, git("rev-parse", "HEAD"), plan, items

    def prewalk_lane(self, repo, sha, plan, *extra, **env):
        return self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--worktree",
                         "--sandbox", "workspace-write", "--prewalk", sha, "--prewalk-plan", str(plan),
                         *extra, **env)

    def test_prewalk_patch_handles_add_update_delete_and_empty_files(self):
        repo, sha, plan, items = self.prewalk_fixture("printf 'checked\\n'; printf 'check stderr\\n' >&2")
        rc, r = self.prewalk_lane(repo, sha, plan)
        self.assertEqual((rc, r["status"], r["prewalk"]["mode"]), (0, "ok", "trajectory"))
        self.assertEqual((r["prewalk"]["commit"], r["prewalk"]["items"]), (sha, items))
        run = Path(r["run_dir"])
        records = [json.loads(line) for line in (run / "prewalk_session.jsonl").read_text().splitlines()]
        calls = [record["payload"]["input"] for record in records
                 if record["type"] == "response_item" and record["payload"]["type"] == "custom_tool_call"]
        patch_call = next(call for call in calls if "tools.apply_patch(" in call)
        patch = json.loads(patch_call.split("tools.apply_patch(", 1)[1].removesuffix("));\n"))
        working = Path(r["worktree"])
        added = working / "new 'file'.py"
        for expected in (f"*** Add File: {added}\n+added = True",
                         f"*** Add File: {working / 'empty.txt'}\n",
                         f"*** Delete File: {working / 'legacy.py'}",
                         f"*** Update File: {working / 'shape.py'}\n@@",
                         "-value = 1\n+value = 2"):
            self.assertIn(expected, patch)
        plans = [json.loads(call.split("tools.update_plan(", 1)[1].removesuffix("));\n"))["plan"]
                 for call in calls if "tools.update_plan(" in call]
        self.assertEqual([[item["status"] for item in entries] for entries in plans],
                         [["in_progress", "pending"], ["completed", "in_progress"]])
        self.assertEqual(plans[-1][0]["step"], f"{items[0]['step']} Check: {items[0]['check']}")
        outputs = [record["payload"]["output"][1]["text"] for record in records
                   if record["type"] == "response_item" and record["payload"]["type"] == "custom_tool_call_output"]
        chunks = [json.loads(output) for output in outputs if output.startswith("{")]
        self.assertIn("\nvalue = 1\n\n", [chunk["output"] for chunk in chunks])
        self.assertIn("legacy = True\n", [chunk["output"] for chunk in chunks])
        self.assertEqual(chunks[-1]["output"], "checked\ncheck stderr\n")
        self.assertEqual(chunks[-1]["exit_code"], 0)
        session = (run / "prewalk_session.jsonl").read_text()
        self.assertNotIn("READY", session)
        self.assertNotIn("PREWALK:", session)
        self.assertFalse(any(record["type"] == "event_msg" for record in records))

    def test_object_prewalk_plan_reads_only_requested_parent_files(self):
        repo, sha, plan, items = self.prewalk_fixture()
        plan.write_text(json.dumps({"todo": items, "reads": ["guide.md", "legacy.py"]}))
        _, r = self.prewalk_lane(repo, sha, plan)
        self.assertEqual(r["prewalk"]["mode"], "trajectory")
        session = (Path(r["run_dir"]) / "prewalk_session.jsonl").read_text()
        self.assertIn("Read this guidance.", session)
        self.assertIn("legacy = True", session)
        self.assertNotIn("No such file", session)
        self.assertEqual((repo / "shape.py").read_text(), "\nvalue = 2\n\n")

    def test_bootstrap_and_splice_failures_fall_back_to_the_complete_brief(self):
        repo, sha, plan, items = self.prewalk_fixture()
        for mode in ("fail", "missing", "unknown_shape", "malformed"):
            with self.subTest(mode=mode):
                rc, r = self.prewalk_lane(repo, sha, plan, FAKE_CODEX_BOOTSTRAP_MODE=mode)
                self.assertEqual((rc, r["status"], r["prewalk"]["mode"]), (0, "ok", "brief"))
                self.assertIn("bootstrap" if mode == "fail" else "splice", r["prewalk"]["reason"])
                self.assertEqual(r["prewalk"]["bootstrap_usage"], {} if mode == "fail" else
                                 {"input_tokens": 12, "output_tokens": 1})
                run = Path(r["run_dir"])
                self.assertFalse((run / "prewalk_session.jsonl").exists())
                self.assertNotIn("resume", json.loads((run / "argv.json").read_text())["argv"])
                brief = (run / "brief.md").read_text()
                self.assertIn("PREWALK:", brief)
                self.assertIn(sha[:7], brief)
                for item in items:
                    self.assertIn(item["step"], brief)
                    self.assertIn(item["check"], brief)

    def test_prewalk_symlink_commit_falls_back_instead_of_forging_a_regular_file(self):
        repo, _, plan, _ = self.prewalk_fixture()
        (repo / "link.md").symlink_to("guide.md")
        subprocess.run(["git", "-C", str(repo), "add", "link.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "add symlink"], check=True)
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
        rc, r = self.prewalk_lane(repo, sha, plan)
        self.assertEqual((rc, r["status"], r["prewalk"]["mode"]), (0, "ok", "brief"))
        self.assertIn("file type or mode", r["prewalk"]["reason"])
        self.assertTrue((Path(r["worktree"]) / "link.md").is_symlink())
        self.assertFalse((Path(r["run_dir"]) / "prewalk_session.jsonl").exists())

    def test_root_prewalk_commit_falls_back_when_it_has_no_parent(self):
        repo = self.git_repo()
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True).stdout.strip()
        plan = self.tmp / "root-plan.json"
        plan.write_text(json.dumps([{"step": "Initialize the repository", "check": "true"}]))
        rc, r = self.prewalk_lane(repo, sha, plan)
        self.assertEqual((rc, r["status"], r["prewalk"]["mode"]), (0, "ok", "brief"))
        self.assertIn("splice failed", r["prewalk"]["reason"])

    def test_resume_keeps_worker_configuration_and_verifies_its_latest_effort(self):
        repo, sha, plan, _ = self.prewalk_fixture()
        dump = self.tmp / "resumed.json"
        bootstrap_dump = self.tmp / "bootstrap.json"
        rc, r = self.prewalk_lane(repo, sha, plan, "--model", "gpt-6-luna", "--effort", "max", "--network",
                                  "--add-dir", str(self.work), FAKE_CODEX_DUMP=dump,
                                  FAKE_CODEX_BOOTSTRAP_DUMP=bootstrap_dump)
        self.assertEqual((rc, r["model_verified"], r["effort_recorded"]), (0, "gpt-6-luna", "max"))
        resumed, boot = json.loads(dump.read_text()), json.loads(bootstrap_dump.read_text())
        argv = resumed["argv"]
        self.assertEqual(argv[:3], ["exec", "resume", r["session_id"]])
        self.assertEqual((resumed["cwd"], resumed["prompt"]), (r["cwd"], "Continue."))
        self.assertIn('model_reasoning_effort="max"', argv)
        self.assertIn('sandbox_mode="workspace-write"', argv)
        self.assertIn("sandbox_workspace_write.network_access=true", argv)
        self.assertIn('approval_policy="never"', argv)
        roots = json.loads(next(arg.split("=", 1)[1] for arg in argv
                                if arg.startswith("sandbox_workspace_write.writable_roots=")))
        params = json.loads((Path(r["run_dir"]) / "params.json").read_text())
        self.assertEqual(roots, [str(self.work.resolve())] + params["git_dir"])
        self.assertIn("tools.update_plan.enabled=true", argv)
        self.assertIn('model_reasoning_effort="low"', boot["argv"])
        self.assertEqual(boot["argv"][boot["argv"].index("-m") + 1], "gpt-6-luna")
        self.assertEqual(r["prewalk"]["bootstrap_usage"], {"input_tokens": 12, "output_tokens": 1})
        self.assertIn("Do the bounded thing.", json.dumps(resumed["history"]))

    def test_deepseek_prewalk_resumes_with_its_provider_configuration(self):
        repo, sha, plan, _ = self.prewalk_fixture()
        rc, r = self.prewalk_lane(repo, sha, plan, "--provider", "deepseek", "--model", "deepseek-test",
                                  DEEPSEEK_API_KEY="synthetic-deepseek-key")
        self.assertEqual((rc, r["status"], r["prewalk"]["mode"]), (0, "ok", "trajectory"))
        run = Path(r["run_dir"])
        for filename in ("argv.json", "bootstrap_argv.json"):
            argv = json.loads((run / filename).read_text())["argv"]
            self.assertIn('model_provider="deepseek"', argv)
            self.assertIn('model_providers.deepseek.env_key="DEEPSEEK_API_KEY"', argv)
            self.assertEqual(argv[argv.index("-m") + 1], "deepseek-test")
        self.assertNotIn("synthetic-deepseek-key", (run / "prewalk_session.jsonl").read_text())

    def test_prewalk_bootstrap_obeys_the_worker_deadline(self):
        repo, sha, plan, _ = self.prewalk_fixture()
        rc, r = self.prewalk_lane(repo, sha, plan, "--max-time", "2s",
                                  FAKE_CODEX_BOOTSTRAP_MODE="slow", FAKE_CODEX_SLEEP=30)
        self.assertEqual((rc, r["status"]), (1, "timeout"))
        run = Path(r["run_dir"])
        self.assertFalse((run / "argv.json").exists())
        self.assertFalse((run / "prewalk_session.jsonl").exists())
        status = json.loads((run / "status.json").read_text())
        self.assertEqual(status["state"], "done")
        self.assertFalse(LANE_MODULE.group_alive(status["codex_pgid"]))

    def test_invalid_prewalk_plans_are_usage_errors_before_launch(self):
        repo, sha, plan, items = self.prewalk_fixture()
        invalid = [None, {}, [], items * 5, [{"step": "s"}], [{"step": "s", "check": " "}],
                   [{"step": 1, "check": "true"}], [{"step": "s", "check": "true", "extra": "bad"}],
                   {"todo": items, "extra": []}, {"todo": items, "reads": None},
                   {"todo": items, "reads": "guide.md"}, {"todo": items, "reads": [1]},
                   {"todo": items, "reads": ["../outside"]}, {"todo": items, "reads": [str(repo / "guide.md")]},
                   {"todo": items, "reads": ["null\0path"]}, [{"step": "s", "check": "true\0"}],
                   {"todo": items, "reads": [""]}, {"todo": items, "reads": ["."]},
                   {"todo": items, "reads": ["missing.md"]}, {"todo": items, "reads": ["new 'file'.py"]}]
        for data in invalid:
            with self.subTest(data=data):
                plan.write_text(json.dumps(data))
                rc, r = self.prewalk_lane(repo, sha, plan)
                self.assertEqual((rc, r["status"]), (2, "usage_error"))
        plan.write_text("{invalid")
        self.assertEqual(self.prewalk_lane(repo, sha, plan)[1]["status"], "usage_error")
        plan.unlink()
        self.assertEqual(self.prewalk_lane(repo, sha, plan)[1]["status"], "usage_error")
        self.assertFalse(any(self.home.glob("sessions/**/*.jsonl")))

    def test_prewalk_check_runs_once_in_working_cwd_before_bootstrap(self):
        check = "test ! -f first-check.txt && printf 'once' > first-check.txt"
        repo, sha, plan, _ = self.prewalk_fixture(check)
        rc, r = self.prewalk_lane(repo, sha, plan, "--holdout", "test \"$(cat first-check.txt)\" = once")
        self.assertEqual((rc, r["status"], r["holdout"]["passed"]), (0, "ok", True))
        self.assertFalse((repo / "first-check.txt").exists())
        self.assertEqual((Path(r["cwd"]) / "first-check.txt").read_text(), "once")

    def test_failed_prewalk_check_cleans_up_and_never_bootstraps(self):
        repo, sha, plan, _ = self.prewalk_fixture("printf 'failure evidence\\n'; exit 7")
        dump = self.tmp / "bootstrap.json"
        rc, r = self.prewalk_lane(repo, sha, plan, FAKE_CODEX_BOOTSTRAP_DUMP=dump)
        self.assertEqual((rc, r["status"]), (2, "usage_error"))
        self.assertIn("failure evidence", r["error"])
        self.assertFalse(dump.exists())
        self.assertFalse(any(self.home.glob("sessions/**/*.jsonl")))
        self.assertFalse(any((self.tmp / "worktrees").glob("lane-*")))
        self.assertFalse(any((self.tmp / "state" / "runs").glob("*/params.json")))

    def test_single_item_prewalk_finishes_its_plan(self):
        repo, sha, plan, items = self.prewalk_fixture()
        plan.write_text(json.dumps({"todo": items[:1], "reads": []}))
        _, r = self.prewalk_lane(repo, sha, plan)
        self.assertEqual(r["prewalk"]["mode"], "trajectory")
        records = [json.loads(line) for line in (Path(r["run_dir"]) / "prewalk_session.jsonl").read_text().splitlines()]
        calls = [record["payload"]["input"] for record in records
                 if record["type"] == "response_item" and record["payload"]["type"] == "custom_tool_call"]
        self.assertIn('"status": "completed"', calls[-1])
        self.assertNotIn('"status": "in_progress"', calls[-1])

    def test_prewalk_result_is_null_without_a_commit(self):
        rc, r = self.run_lane()
        self.assertEqual((rc, r["status"]), (0, "ok"))
        self.assertIsNone(r["prewalk"])

    def test_retry_explicit_handoff_flags_replace_previous_settings(self):
        repo = self.git_repo()
        def git(*args):
            return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
                                  check=True, capture_output=True, text=True).stdout.strip()
        for name in ("old.md", "new.md", "old.txt", "new.txt"):
            (repo / name).write_text(name + "\n")
        git("add", "-A")
        git("commit", "-qm", "first pattern")
        first = git("rev-parse", "HEAD")
        _, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--profile", "review",
                         "--context", str(repo / "old.md"), "--holdout", "true", "--protect", "old.txt",
                         "--prewalk", first, FAKE_CODEX_MODE="ok_review")
        self.assertEqual(r["status"], "ok")
        git("commit", "-q", "--allow-empty", "-m", "second pattern")
        second = git("rev-parse", "HEAD")
        command = "printf 'replacement\\n'; exit 9"
        rc, retry = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--retry-of", r["lane_id"],
                             "--context", str(repo / "new.md"), "--holdout", command, "--protect", "new.txt",
                             "--prewalk", second, FAKE_CODEX_MODE="ok_review", FAKE_CODEX_CLOBBER="old.txt")
        self.assertEqual((rc, retry["status"]), (0, "ok"))
        self.assertEqual((retry["holdout"]["cmd"], retry["holdout"]["exit_code"], retry["holdout"]["tampered"]),
                         (command, 9, []))
        params = json.loads((Path(retry["run_dir"]) / "params.json").read_text())
        self.assertEqual((params["context"], params["protect"], params["prewalk"]),
                         ([str((repo / "new.md").resolve())], ["new.txt"], second))
        brief = (Path(retry["run_dir"]) / "brief.md").read_text()
        self.assertIn("second pattern", brief)
        self.assertNotIn("old.md", brief)
        self.assertIn("The demonstrated pattern is a settled design decision: extend it to the remaining cases; "
                      "do not redesign it, do not re-implement it in parallel elsewhere, and do not delete and rewrite it. "
                      "If the pattern cannot work for a case, say so explicitly in your final report instead of silently diverging.",
                      brief)

    # --- review round 1 fixes ---
    def test_marker_must_be_first_line(self):
        self.assertEqual(self.run_lane(FAKE_CODEX_MODE="late_marker")[1]["status"], "contract_violation")

    def test_bad_args_still_emit_one_json_line(self):
        rc, r = self.lane("start", "--cwd", str(self.work))
        self.assertEqual((rc, r["status"]), (2, "usage_error"))
        rc, r = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work), "--max-time", "soon")
        self.assertEqual((rc, r["status"]), (2, "usage_error"))
        self.assertFalse((self.tmp / "state" / "runs").exists() and any((self.tmp / "state" / "runs").iterdir()))

    def test_result_exit_code_follows_status(self):
        _, r = self.run_lane(FAKE_CODEX_MODE="auth")
        rc, _ = self.lane("result", r["lane_id"])
        self.assertEqual(rc, 1)

    def test_cancel(self):
        _, s = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work),
                         FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=60)
        time.sleep(2)
        rc, c = self.lane("cancel", s["lane_id"])
        self.assertEqual(c["status"], "cancel_requested")
        rc, w = self.lane("wait", s["lane_id"], "--max", "60")
        self.assertEqual((rc, w["status"]), (1, "cancelled"))

    def test_timeout_kills_whole_group(self):
        pidfile = self.tmp / "child.pid"
        rc, r = self.run_lane("--max-time", "3s", FAKE_CODEX_MODE="slow_child", FAKE_CHILD_PIDFILE=pidfile)
        self.assertEqual(r["status"], "timeout")
        child = int(pidfile.read_text())
        time.sleep(0.5)
        with self.assertRaises(ProcessLookupError):
            os.kill(child, 0)

    def test_reservation_survives_supervisor_death(self):
        _, s = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work),
                         FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=60, LANE_MAX_CONCURRENT=1)
        status = self.tmp / "state" / "runs" / s["lane_id"] / "status.json"
        for _ in range(50):
            st = json.loads(status.read_text())
            if st.get("codex_pgid"):
                break
            time.sleep(0.2)
        os.kill(st["supervisor_pid"], 9)
        rc, b = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work), LANE_MAX_CONCURRENT=1)
        self.assertEqual((rc, b["status"]), (4, "busy"))
        os.killpg(st["codex_pgid"], 9)

    def test_add_dir_overlap_is_a_writer_clash(self):
        repo = self.git_repo()
        other = self.tmp / "other"
        other.mkdir()
        _, s = self.lane("start", "--brief", str(self.brief), "--cwd", str(repo), "--sandbox", "workspace-write",
                         "--worktree", "--add-dir", str(other), FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=4)
        rc, b = self.lane("start", "--brief", str(self.brief), "--cwd", str(other), "--sandbox", "workspace-write")
        self.assertEqual((rc, b["status"]), (4, "busy_worktree"))
        self.lane("wait", s["lane_id"], "--max", "60")

    def test_worktree_lane_can_reach_git_metadata(self):
        repo = self.git_repo()
        _, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--sandbox", "workspace-write",
                         "--worktree")
        argv = json.loads((Path(r["run_dir"]) / "argv.json").read_text())["argv"]
        dirs = [str(Path(argv[i + 1]).resolve()) for i, a in enumerate(argv) if a == "--add-dir"]
        common = (repo / ".git").resolve()
        wt_gitdir = Path(subprocess.run(["git", "-C", r["worktree"], "rev-parse", "--absolute-git-dir"],
                                        capture_output=True, text=True).stdout.strip()).resolve()
        # only what a commit on lane/* needs; never the whole .git (config, hooks, other branches)
        self.assertEqual(sorted(dirs), sorted(str(p) for p in (wt_gitdir, common / "objects", common / "refs/heads/lane",
                                                                common / "logs/refs/heads/lane")))
        # and two worktree lanes on one repo don't clash over that shared .git
        _, s = self.lane("start", "--brief", str(self.brief), "--cwd", str(repo), "--sandbox", "workspace-write",
                         "--worktree", FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=4)
        rc, t = self.lane("start", "--brief", str(self.brief), "--cwd", str(repo), "--sandbox", "workspace-write",
                          "--worktree")
        self.assertEqual(t["status"], "started")
        self.lane("wait", s["lane_id"], "--max", "60")
        self.lane("wait", t["lane_id"], "--max", "60")

    def test_weekly_window_is_picked_by_length(self):
        rl = {"limit_id": "codex", "primary": {"used_percent": 10, "window_minutes": 300, "resets_at": 1},
              "secondary": {"used_percent": 99, "window_minutes": 10080, "resets_at": 2}}
        weekly, short = LANE_MODULE.split_windows(rl)
        self.assertEqual((weekly["used_percent"], short["used_percent"]), (99, 10))

    # --- review round 2 fixes ---
    def test_marker_must_be_exact(self):
        rc, r = self.run_lane(FAKE_CODEX_MODE="ok_task")
        self.assertEqual(r["marker"], "STATUS: DONE")
        self.assertEqual(LANE_MODULE.classify(0, False, False, {}, "STATUS: DONE but unfinished\nx", "", "task"),
                         "contract_violation")

    def test_short_window_enforced_even_if_weekly_expired(self):
        d = self.home / "sessions" / "2026" / "09" / "02"
        d.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        (d / "rollout-short.jsonl").write_text(json.dumps({"timestamp": ts, "type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {"limit_id": "codex", "primary": {
                "used_percent": 99, "window_minutes": 300, "resets_at": int(time.time()) + 3600}}}}) + "\n")
        rc, r = self.run_lane()
        self.assertEqual((rc, r["status"]), (5, "capped_preflight"))

    def test_unreadable_brief_is_json(self):
        self.brief.chmod(0)
        try:
            rc, r = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work))
        finally:
            self.brief.chmod(0o644)
        self.assertIn(r["status"], ("error", "usage_error"))

    def test_cancel_kills_orphaned_worker(self):
        _, s = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work),
                         FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=60)
        status = self.tmp / "state" / "runs" / s["lane_id"] / "status.json"
        for _ in range(50):
            st = json.loads(status.read_text())
            if st.get("codex_pgid"):
                break
            time.sleep(0.2)
        os.kill(st["supervisor_pid"], 9)
        time.sleep(0.5)
        rc, c = self.lane("cancel", s["lane_id"])
        self.assertEqual(c["status"], "orphan_killed")
        self.assertFalse(LANE_MODULE.group_alive(st["codex_pgid"]))

    # --- profiles and routing ---
    def route(self, brief_text, *extra):
        self.brief.write_text(brief_text)
        return self.lane("route", "--brief", str(self.brief), "--cwd", str(self.work), *extra)

    def test_auto_review_tiers(self):
        rc, r = self.route("Goal: review the parser change.\n", "--profile", "auto", "--kind", "review")
        self.assertEqual((rc, r["profile"], r["effort"]), (0, "review", "high"))
        rc, r = self.route("Goal: review the order router and credential handling.\n", "--profile", "auto", "--kind", "review")
        self.assertEqual((r["profile"], r["effort"]), ("review-deep", "xhigh"))

    def test_auto_write_lanes(self):
        rc, r = self.route("Goal: add a flag.\nRepo: /x. Owned paths: none.\n", "--profile", "auto", "--sandbox", "workspace-write")
        self.assertEqual((rc, r["status"]), (2, "owned_paths_missing"))
        rc, r = self.route("Goal: fix position sizing.\nRepo: /x. Owned paths: src/risk.py\n", "--profile", "auto", "--sandbox", "workspace-write")
        self.assertEqual((rc, r["status"]), (2, "explicit_profile_required"))
        rc, r = self.route("Goal: add a --verbose flag.\nRepo: /x. Owned paths: cli.py, tests/test_cli.py\nDo:\n1. add flag\n2. test\n",
                           "--profile", "auto", "--sandbox", "workspace-write")
        self.assertEqual((r["profile"], r["model"], r["effort"], r["worktree"]), ("implement", "gpt-6-astra", "high", True))
        rc, r = self.route("Goal: migrate the schema and fix the race.\nRepo: /x. Owned paths: a.py\n", "--profile", "auto", "--sandbox", "workspace-write")
        self.assertEqual(r["profile"], "implement-hard")

    def test_auto_read_only(self):
        self.assertEqual(self.route("Goal: find where the timeout is set.\nRead: a.py\n", "--profile", "auto")[1]["profile"], "quick-investigate")
        self.assertEqual(self.route("Goal: draft the changelog for v2.\n", "--profile", "auto")[1]["profile"], "draft")
        self.assertEqual(self.route("Goal: explain why the cache misses on restart.\nRead: a.py, b.py, c.py\n", "--profile", "auto")[1]["profile"], "investigate")

    def test_named_profile_overrides_and_refusals(self):
        rc, r = self.route("Goal: x\n", "--profile", "review", "--effort", "xhigh")
        self.assertEqual((r["profile"], r["effort"], r["routing"]["overrides"]), ("review", "xhigh", ["effort"]))
        self.assertEqual(self.route("Goal: x\n", "--profile", "nope")[1]["status"], "unknown_profile")
        self.assertEqual(self.route("Goal: x\n", "--profile", "draft-local")[1]["status"], "profile_disabled")

    def test_retry_ladder(self):
        self.brief.write_text("Goal: review it.\n")
        _, r = self.run_lane("--profile", "review", FAKE_CODEX_MODE="ok_review")
        self.assertEqual(r["profile"], "review")
        rc, n = self.lane("route", "--brief", str(self.brief), "--cwd", str(self.work), "--retry-of", r["lane_id"])
        self.assertEqual((n["profile"], n["routing"]["decided_by"]), ("review-deep", "retry"))
        _, r2 = self.run_lane("--profile", "review-deep", FAKE_CODEX_MODE="ok_review")
        rc, e = self.lane("route", "--brief", str(self.brief), "--cwd", str(self.work), "--retry-of", r2["lane_id"])
        self.assertEqual((rc, e["status"]), (2, "escalate_to_claude"))

    def test_points_cap_off_by_default(self):
        self.seed_quota(40)
        rc, r = self.run_lane(FAKE_USED=40, FAKE_DELTA=5)
        rc, r2 = self.run_lane()
        self.assertEqual(r2["status"], "ok")


    def test_project_override(self):
        repo = self.git_repo()
        (repo / ".lane").mkdir()
        (repo / ".lane" / "profiles.json").write_text(json.dumps({"profiles": {"implement": {
            "provider": "openai", "model": "gpt-6-luna", "effort": "max", "sandbox": "workspace-write",
            "kind": "task", "worktree": True, "max_time": "30m"}}}))
        self.brief.write_text("Goal: x\nRepo: /x. Owned paths: a.py\n")
        rc, r = self.lane("route", "--brief", str(self.brief), "--cwd", str(repo), "--profile", "implement")
        self.assertEqual((r["model"], r["effort"]), ("gpt-6-luna", "max"))
        self.assertTrue(r["routing"]["project_overrides"].endswith(".lane/profiles.json"))
        rc, r = self.lane("route", "--brief", str(self.brief), "--cwd", str(self.work), "--profile", "implement")
        self.assertEqual(r["model"], "gpt-6-astra")

    def test_bad_profile_is_one_json_line(self):
        repo = self.git_repo()
        (repo / ".lane").mkdir()
        good = {"provider": "openai", "model": "m", "effort": "high", "sandbox": "read-only", "kind": "task", "max_time": "30m"}
        (repo / ".lane" / "profiles.json").write_text(json.dumps({"profiles": {
            "slow": dict(good, max_time="1 hour"), "partial": {k: v for k, v in good.items() if k != "effort"},
            "listy": [], "ultra": dict(good, effort="ultra"), "nomodel": dict(good, model=None),
            "strbool": dict(good, worktree="false")}}))
        rc, r = self.lane("route", "--brief", str(self.brief), "--cwd", str(repo), "--profile", "slow")
        self.assertEqual((rc, r["status"]), (2, "bad_profile"))
        self.assertIn("1 hour", r["error"])
        rc, r = self.lane("route", "--brief", str(self.brief), "--cwd", str(repo), "--profile", "partial")
        self.assertEqual((rc, r["status"]), (2, "bad_profile"))
        self.assertIn("effort", r["error"])
        for name in ("listy", "ultra", "nomodel", "strbool"):
            rc, r = self.lane("route", "--brief", str(self.brief), "--cwd", str(repo), "--profile", name)
            self.assertEqual((rc, r["status"]), (2, "bad_profile"), name)

    def test_explicit_provider_beats_profile(self):
        rc, r = self.route("Goal: x\n", "--profile", "opus")
        self.assertEqual(r["provider"], "claude")
        rc, r = self.route("Goal: x\n", "--profile", "opus", "--provider", "openai")
        self.assertEqual((r["provider"], r["routing"]["overrides"]), ("openai", ["provider"]))

    def test_worker_env_drops_session_secrets(self):
        os.environ["CLAUDE_CODE_MESSAGING_TOKEN"] = "x"; os.environ["SOME_API_KEY"] = "y"; os.environ["HARMLESS_VAR"] = "z"
        try:
            env = LANE_MODULE.worker_env()
        finally:
            for k in ("CLAUDE_CODE_MESSAGING_TOKEN", "SOME_API_KEY", "HARMLESS_VAR"):
                os.environ.pop(k, None)
        self.assertNotIn("CLAUDE_CODE_MESSAGING_TOKEN", env)
        self.assertNotIn("SOME_API_KEY", env)
        self.assertEqual(env.get("HARMLESS_VAR"), "z")
        self.assertIn("PATH", env)

    # --- opus lanes (claude provider) ---
    def test_opus_lane_ok_isolated_and_rules_injected(self):
        repo = self.git_repo()
        (repo / "AGENTS.md").write_text("RAIL: never touch the broker.\n")
        dump = self.tmp / "claude-dump.json"
        self.brief.write_text("Goal: x\nRepo: /x. Owned paths: a.py\n")
        rc, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--profile", "opus",
                          FAKE_CLAUDE_DUMP=dump, CLAUDE_CODE_MESSAGING_TOKEN="secret")
        self.assertEqual((rc, r["status"], r["model_verified"]), (0, "ok", "claude-opus-5-5"))
        d = json.loads(dump.read_text())
        self.assertIn("--strict-mcp-config", d["argv"])
        self.assertEqual(d["env_advisor"], "1")
        self.assertIsNone(d["env_token"])
        self.assertIn("RAIL: never touch the broker.", d["system"])
        self.assertTrue(r["worktree"])

    def test_opus_lane_capped_and_weekly_gate(self):
        rc, r = self.run_lane("--profile", "opus-review", "--kind", "review", FAKE_CLAUDE_MODE="capped")
        self.assertEqual(r["status"], "capped")
        rc, r = self.run_lane("--profile", "opus-review", "--kind", "review", FAKE_CLAUDE_MODE="review")
        self.assertEqual((rc, r["status"]), (5, "capped_preflight"))          # capped marker holds for 6h
        (self.home / "CAPPED-claude").unlink()
        rc, r = self.run_lane("--profile", "opus-review", "--kind", "review", FAKE_CLAUDE_MODE="review", FAKE_CLAUDE_UTIL="0.85")
        self.assertEqual(r["status"], "ok")
        rc, r = self.run_lane("--profile", "opus-review", "--kind", "review", FAKE_CLAUDE_MODE="review")
        self.assertEqual((rc, r["status"]), (5, "capped_preflight"))

    def test_describe_event(self):
        d = LANE_MODULE.describe_event
        self.assertEqual(d(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "/bin/zsh -lc 'pytest -q'"}})), "  $ pytest -q")
        self.assertIn("exit 1", d(json.dumps({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 1, "aggregated_output": "boom"}})))
        self.assertEqual(d(json.dumps({"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "/r/a.py"}]}}), "/r"), "  edited: a.py")
        self.assertIsNone(d(json.dumps({"type": "turn.started"})))

    # --- pre-release review fixes ---
    def test_brief_fields_stay_on_their_line(self):
        sig = LANE_MODULE.brief_signals
        self.assertEqual(sig("Goal: fix a typo.\nOwned paths:\nRead: lane\n", {})["owned_paths"], 0)
        s = sig("Goal: x\nOwned paths:\n- a.py\n- b/c.py\nRead: lane\n", {})
        self.assertEqual((s["owned_paths"], s["read_files"]), (2, 1))
        self.assertEqual(sig("Repo: /x. Owned paths: a.py, b.py and c.py.\n", {})["owned_paths"], 3)
        self.assertEqual(sig("Goal: x\nOwned paths:\n-   \n- none\nRead: lane\n", {})["owned_paths"], 0)

    def test_short_only_sample_keeps_the_weekly_one(self):
        d = self.home / "sessions" / "2026" / "09" / "03"
        d.mkdir(parents=True)
        now = int(time.time())

        def sample(age_s, minutes, used, resets):
            ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - age_s))
            return json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                "limit_id": "codex", "primary": {"used_percent": used, "window_minutes": minutes, "resets_at": resets}}}})
        (d / "rollout-w.jsonl").write_text(sample(120, 10080, 99, now + 86400) + "\n" + sample(60, 300, 10, now + 3600) + "\n")
        q = LANE_MODULE.latest_rate_limit(str(self.home))
        self.assertEqual((q["used_percent"], q["short_used_percent"]), (99, 10))
        self.assertEqual(self.run_lane()[1]["status"], "capped_preflight")

    def test_claude_quota_keeps_the_highest_reading_in_a_window(self):
        state = self.tmp / "state"
        reset = 4102444800

        def record(util, resets):
            LANE_MODULE.record_claude_rate({"unifiedWindows": {"seven_day": {"utilization": util, "resetsAt": resets}}})
        with mock.patch.object(LANE_MODULE, "STATE", state), \
                mock.patch.object(LANE_MODULE, "CLAUDE_QUOTA_FILE", state / "claude-quota.json"):
            record(0.85, reset)
            record(0.30, reset)                   # an older reading that finished later
            self.assertEqual(LANE_MODULE.claude_weekly_utilization(), 0.85)
            record(0.05, reset + 604800)          # the next window
            record(0.90, reset)                   # a late reading from the old window
            self.assertEqual(LANE_MODULE.claude_weekly_utilization(), 0.05)

    def test_bad_env_is_one_json_line(self):
        rc, r = self.lane("status", LANE_MAX_CONCURRENT="many")
        self.assertEqual((rc, r["status"]), (2, "usage_error"))
        self.assertIn("LANE_MAX_CONCURRENT", r["error"])
        for name, value in (("LANE_QUOTA_STOP", "nan"), ("LANE_MAX_CONCURRENT", "0"), ("LANE_CLAUDE_WEEKLY_STOP", "80")):
            rc, r = self.lane("status", **{name: value})
            self.assertEqual((rc, r["status"]), (2, "usage_error"), name)
            self.assertIn(name, r["error"])

    def test_cwd_with_tilde_still_finds_project_overrides(self):
        repo = self.git_repo()
        (repo / ".lane").mkdir()
        (repo / ".lane" / "profiles.json").write_text(json.dumps({"profiles": {"implement": {
            "model": "gpt-6-luna", "effort": "max", "sandbox": "workspace-write", "kind": "task", "max_time": "30m"}}}))
        rc, r = self.lane("route", "--brief", str(self.brief), "--cwd", "~/repo", "--profile", "implement", HOME=self.tmp)
        self.assertEqual(r["model"], "gpt-6-luna")

    def test_watch_ends_on_unknown_or_abandoned_lane(self):
        p = subprocess.run([sys.executable, str(LANE), "watch", "nope"], capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(p.returncode, 2)
        _, s = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work), FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=60)
        status = self.tmp / "state" / "runs" / s["lane_id"] / "status.json"
        for _ in range(50):
            st = json.loads(status.read_text())
            if st.get("codex_pgid"):
                break
            time.sleep(0.2)
        os.kill(st["supervisor_pid"], 9)
        os.killpg(st["codex_pgid"], 9)
        time.sleep(0.5)
        p = subprocess.run([sys.executable, str(LANE), "watch", s["lane_id"]], capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(p.returncode, 1)

    def test_lane_points_follow_the_account_not_the_home_path(self):
        account = self.tmp / "account"
        account.mkdir()
        (account / "auth.json").write_text("{}")
        h1, h2 = self.tmp / "h1", self.tmp / "h2"
        for h in (h1, h2):
            h.mkdir()
            (h / "auth.json").symlink_to(account / "auth.json")
        runs = self.tmp / "runs"
        (runs / "x").mkdir(parents=True)
        (runs / "x" / "result.json").write_text(json.dumps({"codex_home": str(h1), "kind": "task",
                                                             "finished_at": time.time(), "quota_delta": 10}))
        with mock.patch.object(LANE_MODULE, "RUNS", runs):
            self.assertEqual(LANE_MODULE.lane_points_this_window(str(h2), time.time() + 86400), 10)

    # --- user profiles and a local OpenAI-compatible server (spark provider) ---
    def user_profiles(self, data):
        (self.tmp / "state").mkdir(exist_ok=True)
        (self.tmp / "state" / "profiles.json").write_text(json.dumps(data))

    def test_user_profiles_sit_between_global_and_project(self):
        impl = {"model": "user-model", "effort": "high", "sandbox": "workspace-write", "kind": "task", "max_time": "30m"}
        self.user_profiles({"profiles": {"implement": impl, "mine": dict(impl, model="mine-model")}})
        rc, r = self.route("Goal: x\n", "--profile", "mine")
        self.assertEqual(r["model"], "mine-model")
        self.assertTrue(r["routing"]["user_overrides"].endswith("profiles.json"))
        self.assertEqual(self.route("Goal: x\n", "--profile", "implement")[1]["model"], "user-model")
        repo = self.git_repo()
        (repo / ".lane").mkdir()
        (repo / ".lane" / "profiles.json").write_text(json.dumps({"profiles": {"implement": dict(impl, model="project-model")}}))
        rc, r = self.lane("route", "--brief", str(self.brief), "--cwd", str(repo), "--profile", "implement")
        self.assertEqual(r["model"], "project-model")

    def spark_server(self, key, served=("served-model",), redirect_to=None, seen=None, payload=None, raw=None):
        import http.server
        import threading

        class Models(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if seen is not None:
                    seen.append(self.headers.get("Authorization"))
                if redirect_to:
                    self.send_response(302)
                    self.send_header("Location", redirect_to + "/models")
                    self.end_headers()
                    return
                if raw:
                    self.wfile.write(raw(self.headers.get("Authorization", "")).encode())
                    return
                if self.headers.get("Authorization") != f"Bearer {key}":
                    self.send_response(401)
                    self.end_headers()
                    return
                data = payload if payload is not None else {"data": [{"id": m, "max_model_len": 131072} for m in served]}
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Models)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/v1"

    def test_spark_lane_finds_its_model_and_keeps_the_key_out_of_records(self):
        key = "s3cret-test-key-123"
        base = self.spark_server(key)
        keyfile = self.tmp / "spark-key"
        keyfile.write_text(key + "\n")
        self.user_profiles({"providers": {"spark": {"base_url": base, "api_key_file": str(keyfile)}},
                            "profiles": {"spark": {"provider": "spark", "model": "auto", "effort": "xhigh",
                                                   "sandbox": "read-only", "kind": "task", "max_time": "10m"}}})
        dump = self.tmp / "worker-env.json"
        rc, r = self.run_lane("--profile", "spark", FAKE_CODEX_ENV_DUMP=dump)
        self.assertEqual((rc, r["status"], r["provider"], r["model"]), (0, "ok", "spark", "served-model"))
        self.assertEqual(json.loads(dump.read_text())["LANE_SPARK_KEY"], key)
        run = Path(r["run_dir"])
        argv = json.loads((run / "argv.json").read_text())["argv"]
        self.assertIn(f"model_providers.spark.base_url={json.dumps(base)}", argv)
        self.assertIn("model_context_window=131072", argv)
        for f in run.iterdir():
            self.assertNotIn(key, f.read_text(errors="replace"), f.name)
        rc, r = self.run_lane("--profile", "spark", "--model", "other-model")
        self.assertEqual((rc, r["status"], r["served"]), (2, "model_not_served", ["served-model"]))
        keyfile.write_text("wrong-key")
        rc, r = self.run_lane("--profile", "spark")
        self.assertEqual((rc, r["status"]), (4, "provider_unavailable"))
        self.assertNotIn("wrong-key", json.dumps(r))

    SPARK = {"provider": "spark", "model": "auto", "effort": "xhigh", "sandbox": "read-only", "kind": "task", "max_time": "10m"}

    def spark_setup(self, base, key):
        keyfile = self.tmp / "spark-key"
        keyfile.write_text(key + "\n")
        self.user_profiles({"providers": {"spark": {"base_url": base, "api_key_file": str(keyfile)}},
                            "profiles": {"spark": self.SPARK}})
        return keyfile

    def test_spark_key_never_goes_to_another_host(self):
        key = "s3cret-test-key-456"
        seen = []
        elsewhere = self.spark_server(key, seen=seen)
        self.spark_setup(self.spark_server(key, redirect_to=elsewhere), key)
        rc, r = self.run_lane("--profile", "spark")
        self.assertEqual((rc, r["status"]), (4, "provider_unavailable"))
        self.assertEqual(seen, [])                       # the redirect was not followed
        self.spark_setup(self.spark_server(key), key)
        repo = self.git_repo()
        (repo / ".lane").mkdir()
        (repo / ".lane" / "profiles.json").write_text(json.dumps({"providers": {"spark": {"base_url": elsewhere}}}))
        rc, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--profile", "spark")
        self.assertEqual(r["status"], "error")
        self.assertIn("providers", r["error"])
        self.assertEqual(seen, [])                       # a repo can't point the key somewhere else

    def test_spark_key_problems_are_refused_without_echoing_it(self):
        key = "s3cret-test-key-789"
        keyfile = self.spark_setup(self.spark_server(key), key)
        keyfile.write_text(key + "\nsecond line\n")
        rc, r = self.run_lane("--profile", "spark")
        self.assertEqual((rc, r["status"]), (2, "usage_error"))
        self.assertNotIn(key, json.dumps(r))
        keyfile.write_text("\n")
        rc, r = self.run_lane("--profile", "spark")
        self.assertEqual((rc, r["status"]), (2, "usage_error"))
        for payload in ([], {"data": None}, {"data": [{"id": 7}]}):
            self.spark_setup(self.spark_server(key, payload=payload), key)
            rc, r = self.run_lane("--profile", "spark")
            self.assertEqual(r["status"], "provider_unavailable" if payload != {"data": [{"id": 7}]} else "usage_error", payload)

    def test_spark_key_is_hidden_from_commands_and_scrubbed_from_records(self):
        key = "s3cret-test-key-abc"
        self.spark_setup(self.spark_server(key), key)
        rc, r = self.run_lane("--profile", "spark", FAKE_CODEX_LEAK_ENV="LANE_SPARK_KEY")
        self.assertEqual(r["status"], "ok")
        self.assertNotIn(key, json.dumps(r))
        run = Path(r["run_dir"])
        for f in run.iterdir():
            self.assertNotIn(key, f.read_text(errors="replace"), f.name)
        self.assertIn("<redacted>", (run / "final.md").read_text())
        argv = json.loads((run / "argv.json").read_text())["argv"]
        self.assertTrue(any(a.startswith("shell_environment_policy.exclude=") and "LANE_SPARK_KEY" in a for a in argv))

    def test_spark_key_stays_out_even_when_rotated_escaped_or_reflected(self):
        key = 'q"uo\\te-secret-42'                      # a quote and a backslash: JSON escapes both
        keyfile = self.spark_setup(self.spark_server(key), key)
        rc, r = self.run_lane("--profile", "spark", FAKE_CODEX_LEAK_ENV="LANE_SPARK_KEY", FAKE_CODEX_CLOBBER=keyfile)
        self.assertEqual(r["status"], "ok")
        escaped = json.dumps(key)[1:-1]
        for form in (key, escaped):
            self.assertNotIn(form, json.dumps(r))
            for f in Path(r["run_dir"]).iterdir():
                self.assertNotIn(form, f.read_text(errors="replace"), f.name)
        key = "s3cret-test-key-def"
        self.spark_setup(self.spark_server(key, raw=lambda auth: f"HTTP/1.1 9x9 {auth}\r\n\r\n"), key)
        rc, r = self.run_lane("--profile", "spark")
        self.assertEqual(r["status"], "provider_unavailable")
        self.assertNotIn(key, json.dumps(r))
        self.spark_setup(self.spark_server(key, served=(f"model-{key}",)), key)
        rc, r = self.run_lane("--profile", "spark", "--model", "pinned")
        self.assertEqual(r["status"], "model_not_served")
        self.assertNotIn(key, json.dumps(r))

    def test_watch_redacts_a_running_spark_lane(self):
        key = "s3cret-test-key-ghi"
        self.spark_setup(self.spark_server(key), key)
        _, s = self.lane("start", "--brief", str(self.brief), "--cwd", str(self.work), "--profile", "spark",
                         FAKE_CODEX_MODE="slow", FAKE_CODEX_SLEEP=3, FAKE_CODEX_LEAK_ENV="LANE_SPARK_KEY")
        time.sleep(1.5)
        p = subprocess.run([sys.executable, str(LANE), "watch", s["lane_id"]], capture_output=True, text=True,
                           env=self.env, timeout=30)
        self.assertIn("thinking about", p.stdout)
        self.assertNotIn(key, p.stdout)

    def test_spark_provider_fields_must_be_strings(self):
        for bad in (False, 0, [], {}):
            self.user_profiles({"providers": {"spark": {"base_url": "http://127.0.0.1:9/v1", "api_key_keychain": bad}},
                                "profiles": {"spark": self.SPARK}})
            rc, r = self.run_lane("--profile", "spark")
            self.assertEqual(r["status"], "error", bad)
            self.assertIn("must be a string", r["error"])

    # --- argv hygiene ---
    def test_answer_only_argv_and_empty_cwd_for_both_commands_and_providers(self):
        self.brief.write_text("Write an idea card from this brief.\n\n")
        report = self.tmp / "answer.md"
        report.write_text("An answer without a lane status marker.\n")
        # No git subprocess may be called, including during profile resolution.
        bindir = self.tmp / "bin"
        bindir.mkdir()
        git_called = self.tmp / "git-called"
        git_stub = bindir / "git"
        git_stub.write_text(f"#!/bin/sh\ntouch '{git_called}'\nexit 1\n")
        git_stub.chmod(0o755)
        for command in ("start", "run"):
            for provider, model in (("openai", "gpt-6-astra"), ("claude", "opus")):
                for web in (False, True):
                    with self.subTest(command=command, provider=provider, web=web):
                        dump = self.tmp / "answer-dump.json"
                        flags = ["--web-research"] if web else []
                        rc, r = self.lane(command, "--brief", str(self.brief), "--answer-only",
                                          "--provider", provider, "--model", model, *flags,
                                          FAKE_CODEX_DUMP=dump, FAKE_CLAUDE_DUMP=dump, FAKE_REPORT_FILE=report,
                                          PATH=str(bindir) + os.pathsep + self.env.get("PATH", ""))
                        if command == "start":
                            self.assertEqual((rc, r["status"]), (0, "started"))
                            rc, r = self.lane("wait", r["lane_id"], "--max", "60")
                        self.assertEqual((rc, r["status"], r["exa_calls"]), (0, "ok", 0))
                        self.assertEqual((r["worktree"], r["branch"], r["marker"]), (None, None, None))
                        self.assertEqual(r["summary"], report.read_text())
                        self.assertEqual((r["answer_only"], r["web_research"]), (True, web))
                        d = json.loads(dump.read_text())
                        self.assertEqual(d["prompt"], self.brief.read_text())
                        self.assertEqual(d["cwd_files"], [])
                        self.assertEqual(Path(d["cwd"]).resolve(), Path(r["cwd"]).resolve())
                        self.assertNotEqual(Path(r["cwd"]).resolve(), self.work.resolve())
                        self.assertFalse(Path(r["cwd"]).exists())  # cleaned after completion
                        run = Path(r["run_dir"])
                        params = json.loads((run / "params.json").read_text())
                        self.assertEqual((params["sandbox"], params["network"], params["add_dir"], params["git_dir"]),
                                         ("read-only", False, [], None))
                        self.assertIsNone(params["rules_cwd"])
                        system = ("Answer the brief directly. You may use only the three Exa web research tools. "
                                  "Use no other tools." + LANE_MODULE.EXA_PACING if web
                                  else "Answer the brief directly. Do not use tools.")
                        if web and provider == "openai":
                            system += " Use exec to call them through tools.mcp__exa__<tool>; use only direct, named tool access."
                        self.assertEqual((run / "system.md").read_text(), system + "\n")
                        argv = json.loads((run / "argv.json").read_text())["argv"]
                        self.assertEqual(argv[1:], d["argv"])
                        if provider == "claude":
                            expected = [str(FAKE_CLAUDE), "-p", "--output-format", "stream-json", "--verbose",
                                        "--model", model, "--effort", "medium",
                                        "--max-turns", str(LANE_MODULE.CLAUDE_RESEARCH_TURNS) if web else "2",
                                        "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
                                        "--strict-mcp-config", "--disable-slash-commands", "--system-prompt-file",
                                        str(run / "system.md"), "--permission-mode", "dontAsk", "--tools", ""]
                            if web:
                                expected += ["--mcp-config", str(run / "mcp.json"), "--allowedTools",
                                             "mcp__exa__web_search_exa", "mcp__exa__web_fetch_exa",
                                             "mcp__exa__web_search_advanced_exa"]
                                self.assertEqual(json.loads((run / "mcp.json").read_text()),
                                                 {"mcpServers": {"exa": {"type": "http", "url":
                                                  "https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa,web_search_advanced_exa"}}})
                            self.assertEqual(argv, expected)
                            self.assertEqual(d["env_no_rules"], "1")
                        else:
                            expected = [str(FAKE), "exec", "--ignore-user-config", "--skip-git-repo-check",
                                        "--disable", "hooks", "--disable", "multi_agent", "--disable", "context_management",
                                        "--disable", "apps", "--json", "--output-last-message", str(run / "final.md"),
                                        "-m", model, "-c", 'model_reasoning_effort="medium"', "-c", 'approval_policy="never"',
                                        "-c", f"developer_instructions={json.dumps(system)}",
                                        "-c", "shell_environment_policy.experimental_use_profile=false",
                                        "-c", 'shell_environment_policy.inherit="all"',
                                        "-c", 'shell_environment_policy.exclude=["LANE_SPARK_KEY", "DEEPSEEK_API_KEY"]',
                                        "-c", f"shell_environment_policy.set.PATH={json.dumps(str(bindir) + os.pathsep + self.env.get('PATH', ''))}",
                                        "-s", "read-only", "-C", r["cwd"]]
                            for feature in (
                                "shell_tool", "unified_exec", "apply_patch_freeform", "view_image", "image_generation",
                                "js_repl", "code_mode", "code_mode_only", "code_mode_host", "code_mode_prewarm",
                                "web_search_request", "web_search_cached", "standalone_web_search", "search_tool",
                                "tool_search", "tool_suggest", "plugins", "skill_search", "skill_mcp_dependency_install",
                                "browser_use", "computer_use", "in_app_browser", "in_app_local_automation",
                                "goals", "token_budget", "memories", "request_permissions_tool", "deferred_executor",
                                "default_mode_request_user_input", "send_message_to_user_async", "current_time_reminder",
                                "sleep_tool", "agent_message_board", "enable_fanout", "guardian_conversation_history_tools",
                                "shell_snapshot", "shell_snapshot_v2", "enable_mcp_apps", "executor_capability_discovery",
                                "remote_plugin", "artifact", "realtime_conversation",
                            ):
                                if web and feature in ("code_mode", "code_mode_only", "code_mode_host", "code_mode_prewarm"):
                                    continue
                                expected += ["--disable", feature]
                            for setting in (
                                'web_search="disabled"', 'tools.update_plan.enabled=false',
                                'tools.experimental_request_user_input.enabled=false', 'project_doc_max_bytes=0',
                                'skills.include_instructions=false', 'features.skip_host_skill_discovery=true',
                                'memories.generate_memories=false', 'memories.use_memories=false',
                                'include_permissions_instructions=false', 'include_apps_instructions=false',
                                'include_collaboration_mode_instructions=false', 'include_environment_context=false',
                                f'model_instructions_file={json.dumps(str(run / "system.md"))}', 'mcp_servers={}',
                            ):
                                expected += ["-c", setting]
                            if web:
                                expected += ["-c", 'mcp_servers.exa.url="https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa,web_search_advanced_exa"',
                                             "-c", 'mcp_servers.exa.enabled_tools=["web_search_exa", "web_fetch_exa", "web_search_advanced_exa"]',
                                             "-c", "mcp_servers.exa.required=true"]
                            self.assertEqual(argv, expected + ["-"])
        self.assertFalse(git_called.exists())

    def test_answer_only_ignores_caller_cwd_and_profile_write_permissions(self):
        repo = self.git_repo()
        (repo / "AGENTS.md").write_text("REPO RULE MUST NOT BE LOADED")
        (repo / ".lane").mkdir()
        (repo / ".lane" / "profiles.json").write_text("not valid JSON")
        for profile in ("implement", "opus"):
            rc, r = self.lane("run", "--brief", str(self.brief), "--cwd", str(repo), "--answer-only", "--profile", profile)
            self.assertEqual((rc, r["status"], r["worktree"]), (0, "ok", None))
            self.assertNotIn("REPO RULE", (Path(r["run_dir"]) / "system.md").read_text())
            self.assertIsNone(r["routing"]["project_overrides"])

    def test_answer_only_rejects_incompatible_flags(self):
        for flags in (("--network",), ("--worktree",), ("--add-dir", str(self.work)),
                      ("--sandbox", "workspace-write")):
            rc, r = self.run_lane("--answer-only", *flags)
            self.assertEqual((rc, r["status"]), (2, "usage_error"))
        self.assertEqual(self.run_lane("--web-research")[0], 2)
        self.assertEqual(self.lane("run", "--brief", str(self.brief))[0], 2)
        self.assertFalse((self.tmp / "state" / "runs").exists())

    def test_answer_only_audits_allowed_and_forbidden_calls(self):
        events = self.tmp / "events.jsonl"
        for provider in ("openai", "claude"):
            for web in (False, True):
                for names in (("mcp__exa__web_search_exa", "mcp__exa__web_fetch_exa", "mcp__exa__web_search_advanced_exa"),
                              ("mcp__exa__web_search_exa", "mcp__paid_exa__web_search_exa"),
                              ("Bash",)):
                    with self.subTest(provider=provider, web=web, names=names):
                        log = []
                        for i, name in enumerate(names):
                            if provider == "claude":
                                log.append({"type": "assistant", "message": {"content": [
                                    {"type": "tool_use", "name": name, "id": str(i), "input": {}}]}})
                            else:
                                item = {"id": str(i), "type": "command_execution", "command": "ls"}
                                if name.startswith("mcp__"):
                                    _, server, tool = name.split("__", 2)
                                    item = {"id": str(i), "type": "mcp_tool_call", "server": server, "tool": tool}
                                log.extend({"type": f"item.{stage}", "item": item} for stage in ("started", "completed"))
                        events.write_text("".join(json.dumps(ev) + "\n" for ev in log))
                        flags = ["--web-research"] if web else []
                        rc, r = self.run_lane("--provider", provider, "--answer-only", *flags, FAKE_EVENTS_FILE=events)
                        ok = web and len(names) == 3
                        self.assertEqual((rc, r["status"]), (0, "ok") if ok else (1, "tool_used"))
                        self.assertEqual(r["exa_calls"], sum(n in LANE_MODULE.EXA_TOOL_NAMES for n in names))
                        self.assertEqual(r["reason"], None if ok else "tool_used")
                        self.assertEqual(self.lane("result", r["lane_id"])[0], rc)
        # Tool audits apply only to the new modes.
        self.assertEqual(self.run_lane(FAKE_EVENTS_FILE=events)[1]["status"], "ok")

    def test_answer_only_audits_codex_raw_rollout(self):
        events = self.tmp / "raw.jsonl"
        events.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "function_call", "name": "view_image", "call_id": "call_1", "arguments": "{}"}}) + "\n")
        rc, r = self.run_lane("--answer-only", FAKE_ROLLOUT_EVENTS_FILE=events)
        self.assertEqual((rc, r["status"], r["disallowed_tools"]), (1, "tool_used", ["view_image"]))

    def test_answer_only_counts_exa_once_across_exec_and_rollout(self):
        events, raw = self.tmp / "events.jsonl", self.tmp / "raw.jsonl"
        events.write_text("".join(json.dumps({"type": f"item.{stage}", "item": {
            "type": "mcp_tool_call", "server": "exa", "tool": "web_fetch_exa", "id": "item_1"}}) + "\n"
            for stage in ("started", "completed")))
        raw.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "function_call", "namespace": "mcp__exa", "name": "web_fetch_exa", "call_id": "call_1"}}) + "\n")
        rc, r = self.run_lane("--answer-only", "--web-research", FAKE_EVENTS_FILE=events, FAKE_ROLLOUT_EVENTS_FILE=raw)
        self.assertEqual((rc, r["status"], r["exa_calls"]), (0, "ok", 1))

    def test_web_research_allows_codex_exec_with_exa_calls(self):
        events, raw = self.tmp / "events.jsonl", self.tmp / "raw.jsonl"
        source = 'text(ALL_TOOLS.filter(t => t.name.startsWith("mcp__exa__")));\n'
        for tool in ("web_search_exa", "web_fetch_exa", "web_search_advanced_exa"):
            source += f'text(await tools.mcp__exa__{tool}({{"query": "test"}}));\n'
        for stream in ("events", "rollout", "both"):
            with self.subTest(stream=stream):
                log, rollout = [], []
                for i, tool in enumerate(("web_search_exa", "web_fetch_exa", "web_search_advanced_exa")):
                    item = {"id": f"item_{i}", "type": "mcp_tool_call", "server": "exa", "tool": tool}
                    log.extend({"type": f"item.{stage}", "item": item} for stage in ("started", "updated", "completed"))
                    rollout.append({"type": "response_item", "payload": {
                        "type": "function_call", "namespace": "mcp__exa", "name": tool, "call_id": f"call_{i}"}})
                    rollout.extend({"type": "event_msg", "payload": {
                        "type": f"mcp_tool_call_{stage}", "call_id": f"call_{i}",
                        "invocation": {"server": "exa", "tool": tool}}} for stage in ("begin", "end"))
                events.write_text("".join(json.dumps(e) + "\n" for e in log) if stream != "rollout" else "")
                raw.write_text("".join(json.dumps(e) + "\n" for e in rollout) if stream != "events" else "")
                rc, r = self.run_lane("--answer-only", "--web-research", FAKE_CODEX_MODE="nomarker",
                                      FAKE_CODEX_EXEC_INPUT=source, FAKE_EVENTS_FILE=events, FAKE_ROLLOUT_EVENTS_FILE=raw)
                self.assertEqual((rc, r["status"], r["exa_calls"], r["disallowed_tools"]), (0, "ok", 3, []))
                self.assertIsNone(r["reason"])

    def test_web_research_accepts_codex_0161_item_wrappers(self):
        # Real rollout shape from Codex 0.161 (Oct 8 check): thread items wrapped in item_completed events.
        raw = self.tmp / "raw.jsonl"
        wrapped = [{"type": "UserMessage", "id": "u"}, {"type": "Reasoning", "id": "r"},
                   {"type": "McpToolCall", "id": "exec-1", "server": "exa", "tool": "web_search_exa"},
                   {"type": "AgentMessage", "id": "a"}]
        raw.write_text("".join(json.dumps({"type": "event_msg", "payload": {"type": "item_completed", "item": item}})
                               + "\n" for item in wrapped))
        rc, r = self.run_lane("--answer-only", "--web-research", FAKE_CODEX_MODE="nomarker",
                              FAKE_CODEX_EXEC_INPUT="text(await tools.mcp__exa__web_search_exa({query: 'x'}));",
                              FAKE_ROLLOUT_EVENTS_FILE=raw)
        self.assertEqual((rc, r["status"], r["exa_calls"], r["disallowed_tools"]), (0, "ok", 1, []))

    def test_web_research_ignores_unexposed_exa_names_in_exec(self):
        # Oct 8 round 1: Codex tried tools.mcp__exa__crawling_exa, which failed with "is not a function".
        source = ("text(await tools.mcp__exa__crawling_exa({url: 'u'}));\n"
                  "text(await tools.mcp__exa__web_search_exa({query: 'x'}));")
        rc, r = self.run_lane("--answer-only", "--web-research", FAKE_CODEX_MODE="nomarker",
                              FAKE_CODEX_EXEC_INPUT=source)
        self.assertNotIn("mcp__exa__crawling_exa", r["disallowed_tools"])
        rc, r = self.run_lane("--answer-only", "--web-research",
                              FAKE_CODEX_EXEC_INPUT="await tools.mcp__exa__web_search_exa({}); await tools.apply_patch('p')")
        self.assertEqual((rc, r["status"]), (1, "tool_used"))
        self.assertIn("apply_patch", r["disallowed_tools"])

    def test_web_research_rejects_forbidden_codex_exec_input_without_nested_events(self):
        cases = (
            ('await tools.apply_patch("patch")', "apply_patch"),
            ("text(await tools.clock__curr_time({}))", "clock__curr_time"),
            ("text(await tools.list_mcp_resources({}))", "list_mcp_resources"),
            ("text(await tools.list_mcp_resource_templates({}))", "list_mcp_resource_templates"),
            ("text(await tools.read_mcp_resource({}))", "read_mcp_resource"),
            ("await tools.exec_command({})", "exec_command"),
            ('await tools["mcp__exa__web_search_exa"]({})', "exec:indirect_tools"),
            ('await tools \n [ALL_TOOLS[0].name]({})', "exec:indirect_tools"),
            ('await tools /* lookup */ ["apply_patch"]("patch")', "exec:indirect_tools"),
            ('const alias = tools; await alias.apply_patch("patch")', "exec:indirect_tools"),
            ('const {apply_patch} = tools; await apply_patch("patch")', "exec:indirect_tools"),
            ('eval("tool" + "s.apply_patch(1)")', "exec:unclassified_input"),
            (r't\u006fols.apply_patch("patch")', "exec:unclassified_input"),
            ("", "exec:unclassified_input"),
        )
        for source, forbidden in cases:
            with self.subTest(source=source):
                rc, r = self.run_lane("--answer-only", "--web-research", FAKE_CODEX_EXEC_INPUT=source)
                self.assertEqual((rc, r["status"], r["reason"], r["exa_calls"]), (1, "tool_used", "tool_used", 0))
                self.assertIn(forbidden, r["disallowed_tools"])

    def test_web_research_rejects_forbidden_nested_codex_exec_activity(self):
        raw = self.tmp / "raw.jsonl"
        cases = (
            ("response_item", {"type": "custom_tool_call", "name": "apply_patch", "input": "patch"}, "apply_patch"),
            ("response_item", {"type": "function_call", "name": "clock__curr_time"}, "clock__curr_time"),
            ("response_item", {"type": "function_call", "name": "list_mcp_resources"}, "list_mcp_resources"),
            ("response_item", {"type": "local_shell_call"}, "local_shell_call"),
            ("response_item", {"type": "future_tool"}, "future_tool"),
            ("response_item", {"type": "future_tool", "name": "mcp__exa__web_fetch_exa"},
             "future_tool:mcp__exa__web_fetch_exa"),
            ("event_msg", {"type": "exec_command_begin", "command": ["ls"]}, "exec_command_begin"),
            ("event_msg", {"type": "patch_apply_begin"}, "patch_apply_begin"),
            ("event_msg", {"type": "future_tool"}, "future_tool"),
            ("event_msg", {"type": "mcp_tool_call_begin", "invocation": {"server": "other", "tool": "web_search_exa"}},
             "mcp__other__web_search_exa"),
            ("event_msg", {"type": "mcp_tool_call_end", "invocation": {"server": "exa", "tool": "unknown"}},
             "mcp__exa__unknown"),
            ("item.completed", {"type": "file_change"}, "file_change"),
            ("item.started", {"type": "future_tool"}, "future_tool"),
            ("event_msg", {"type": "item_completed", "item": {"type": "McpToolCall", "id": "m", "server": "other",
                                                              "tool": "web_search_exa"}}, "mcp__other__web_search_exa"),
            ("event_msg", {"type": "item_completed", "item": {"type": "FileChange", "id": "f"}}, "item:FileChange"),
            ("event_msg", {"type": "item_started", "item": {}}, "item:None"),
        )
        for envelope, item, forbidden in cases:
            with self.subTest(envelope=envelope, item=item):
                raw.write_text(json.dumps({"type": "response_item", "payload": {
                    "type": "mcp_tool_call", "server": "exa", "tool": "web_fetch_exa", "call_id": "exa_1"}}) + "\n" +
                    json.dumps({"type": envelope, "item" if envelope.startswith("item.") else "payload": item}) + "\n")
                rc, r = self.run_lane("--answer-only", "--web-research", FAKE_CODEX_EXEC_INPUT="text(ALL_TOOLS);",
                                      FAKE_ROLLOUT_EVENTS_FILE=raw)
                self.assertEqual((rc, r["status"], r["reason"], r["exa_calls"]), (1, "tool_used", "tool_used", 1))
                self.assertIn(forbidden, r["disallowed_tools"])

    def test_web_research_rejects_codex_exec_without_classifiable_input(self):
        raw = self.tmp / "raw.jsonl"
        raw.write_text(json.dumps({"type": "response_item", "payload": {
            "type": "custom_tool_call", "name": "exec", "call_id": "exec_1", "input": {"code": "text(ALL_TOOLS)"}}}) + "\n")
        rc, r = self.run_lane("--answer-only", "--web-research", FAKE_ROLLOUT_EVENTS_FILE=raw)
        self.assertEqual((rc, r["status"], r["disallowed_tools"]), (1, "tool_used", ["exec:unclassified_input"]))

    def test_plain_answer_only_rejects_codex_exec(self):
        rc, r = self.run_lane("--answer-only", FAKE_CODEX_EXEC_INPUT="text(ALL_TOOLS);")
        self.assertEqual((rc, r["status"], r["exa_calls"], r["disallowed_tools"]), (1, "tool_used", 0, ["exec"]))

    def test_coding_mode_does_not_audit_codex_exec(self):
        rc, r = self.run_lane(FAKE_CODEX_EXEC_INPUT='await tools.apply_patch("patch")')
        self.assertEqual((rc, r["status"]), (0, "ok"))
        self.assertNotIn("exa_calls", r)

    def test_tool_audit_catches_unknown_and_partial_calls_without_reading_answer_text(self):
        events = self.tmp / "audit.jsonl"
        for kind in ("file_change", "web_search", "todo_list", "collab_tool_call", "future_tool"):
            events.write_text(json.dumps({"type": "item.started", "item": {"id": "x", "type": kind}}))
            self.assertEqual(LANE_MODULE.audit_tool_calls(events)["other_tools"], [kind])
        events.write_text(json.dumps({"type": "stream_event", "event": {"type": "content_block_start",
            "content_block": {"type": "tool_use", "id": "x", "name": "Read"}}}))
        self.assertEqual(LANE_MODULE.audit_tool_calls(events)["other_tools"], ["Read"])
        events.write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
            "text": 'Example: {"type":"tool_use","name":"Bash"}'}}))
        self.assertEqual(LANE_MODULE.audit_tool_calls(events), {"exa_calls": 0, "other_tools": []})

    def test_argv_pins_and_isolates(self):
        _, r = self.run_lane("--model", "gpt-6-astra", "--effort", "high")
        argv = json.loads((Path(r["run_dir"]) / "argv.json").read_text())["argv"]
        for flag in ("--ignore-user-config", "apps", "hooks", "multi_agent", "--json"):
            self.assertIn(flag, argv)
        self.assertNotIn("--ephemeral", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-astra")
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertFalse(any(a.startswith("service_tier") for a in argv))
        self.assertNotIn("shell_tool", argv)
        self.assertNotIn("mcp_servers={}", argv)
        self.assertNotIn("exa_calls", r)


if __name__ == "__main__":
    unittest.main()
