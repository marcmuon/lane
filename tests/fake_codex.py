#!/usr/bin/env python3
"""Stand-in for `codex exec` in lane's tests. Behaviour comes from FAKE_CODEX_MODE; spends no quota."""

import json
import os
import sys
import time
import uuid
from pathlib import Path

args = sys.argv[1:]
mode = os.environ.get("FAKE_CODEX_MODE", "ok_task")
final = Path(args[args.index("--output-last-message") + 1])
model = args[args.index("-m") + 1]
effort = next((a.split("=", 1)[1].strip('"') for a in args if a.startswith("model_reasoning_effort=")), None)
sys.stdin.read()
thread = str(uuid.uuid4())
home = Path(os.environ["CODEX_HOME"])
used = float(os.environ.get("FAKE_USED", "40"))
resets = int(os.environ.get("FAKE_RESETS", str(int(time.time()) + 3 * 86400)))


def ev(obj):
    print(json.dumps(obj), flush=True)


def rollout(used_after):
    d = home / "sessions" / time.strftime("%Y/%m/%d")
    d.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    with (d / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{thread}.jsonl").open("w") as fh:
        fh.write(json.dumps({"timestamp": ts, "type": "turn_context", "payload": {
            "model": model, "collaboration_mode": {"settings": {"reasoning_effort": effort}}}}) + "\n")
        fh.write(json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "limit_id": "codex", "primary": {"used_percent": used_after, "window_minutes": 10080, "resets_at": resets},
            "plan_type": "pro"}}}) + "\n")


ev({"type": "thread.started", "thread_id": thread})
ev({"type": "turn.started"})
if mode == "slow_child":
    # a descendant that ignores SIGTERM, so only a whole-group KILL stops it
    import subprocess
    child = subprocess.Popen([sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"])
    Path(os.environ["FAKE_CHILD_PIDFILE"]).write_text(str(child.pid))
    time.sleep(120)
if mode == "slow":
    time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP", "5")))
    mode = "ok_task"
if mode == "capped":
    rollout(100.0)
    ev({"type": "error", "message": "You've hit your usage limit. Try again at 6:10 PM."})
    ev({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 6:10 PM."}})
    sys.exit(1)
if mode == "auth":
    ev({"type": "turn.failed", "error": {"message": "unexpected status 401 Unauthorized"}})
    sys.exit(1)
if mode == "reconnect_then_ok":
    ev({"type": "error", "message": "Reconnecting... 2/2 (stream disconnected before completion)"})
    mode = "ok_task"
texts = {"ok_task": "STATUS: DONE\nChanged a.py; ran pytest -q: 3 passed.",
         "ok_review": "VERDICT: REVISE\n1. lane:10 off-by-one. Fix: use <=.",
         "nomarker": "I did the thing.", "late_marker": "Here is my report.\nSTATUS: DONE", "noreport": "", "o_missing": "STATUS: DONE\nfrom the event stream"}
text = texts[mode]
if text:
    ev({"type": "item.completed", "item": {"id": "item_9", "type": "agent_message", "text": text}})
ev({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50}})
rollout(used + float(os.environ.get("FAKE_DELTA", "0.5")))
if mode != "o_missing":
    final.write_text(text)
sys.exit(0)
