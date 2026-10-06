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
        dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        self.assertIn(str((repo / ".git").resolve()), [str(Path(d).resolve()) for d in dirs])
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
    def test_argv_pins_and_isolates(self):
        _, r = self.run_lane("--model", "gpt-6-astra", "--effort", "high")
        argv = json.loads((Path(r["run_dir"]) / "argv.json").read_text())["argv"]
        for flag in ("--ignore-user-config", "apps", "hooks", "multi_agent", "--json"):
            self.assertIn(flag, argv)
        self.assertNotIn("--ephemeral", argv)
        self.assertEqual(argv[argv.index("-m") + 1], "gpt-6-astra")
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertFalse(any(a.startswith("service_tier") for a in argv))


if __name__ == "__main__":
    unittest.main()
