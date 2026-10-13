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
resumed = len(args) > 1 and args[1] == "resume"
prompt = args[-1] if resumed else sys.stdin.read()
bootstrap = prompt == "Reply with exactly READY."
if os.environ.get("FAKE_CODEX_DUMP"):
    Path(os.environ["FAKE_CODEX_DUMP"]).write_text(json.dumps({
        "argv": args, "cwd": os.getcwd(), "cwd_files": os.listdir(), "prompt": prompt}))
thread = args[2] if resumed else str(uuid.uuid4())
home = Path(os.environ["CODEX_HOME"])
used = float(os.environ.get("FAKE_USED", "40"))
resets = int(os.environ.get("FAKE_RESETS", str(int(time.time()) + 3 * 86400)))


def ev(obj):
    print(json.dumps(obj), flush=True)


def rollout(used_after):
    d = home / "sessions" / time.strftime("%Y/%m/%d")
    d.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    path = next(d.glob(f"rollout-*-{thread}.jsonl"), None) if resumed else None
    path = path or d / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{thread}.jsonl"
    with path.open("a" if resumed else "w") as fh:
        fh.write(json.dumps({"timestamp": ts, "type": "turn_context", "payload": {
            "model": model, "collaboration_mode": {"settings": {"reasoning_effort": effort}}}}) + "\n")
        fh.write(json.dumps({"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "limit_id": "codex", "primary": {"used_percent": used_after, "window_minutes": 10080, "resets_at": resets},
            "plan_type": "pro"}}}) + "\n")
        if "FAKE_CODEX_EXEC_INPUT" in os.environ:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "exec", "call_id": "exec_1",
                "input": os.environ["FAKE_CODEX_EXEC_INPUT"]}}) + "\n")
        if os.environ.get("FAKE_ROLLOUT_EVENTS_FILE"):
            fh.write(Path(os.environ["FAKE_ROLLOUT_EVENTS_FILE"]).read_text())
        if "FAKE_CODEX_EXEC_INPUT" in os.environ:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": "exec_1", "output": "Script completed"}}) + "\n")


if os.environ.get("FAKE_CODEX_ENV_DUMP"):
    Path(os.environ["FAKE_CODEX_ENV_DUMP"]).write_text(json.dumps({"LANE_SPARK_KEY": os.environ.get("LANE_SPARK_KEY")}))
ev({"type": "thread.started", "thread_id": thread})
ev({"type": "turn.started"})
if bootstrap:
    # Entirely synthetic: no user/account identifiers or content from real rollouts.
    bootstrap_mode = os.environ.get("FAKE_CODEX_BOOTSTRAP_MODE", "ok")
    if os.environ.get("FAKE_CODEX_BOOTSTRAP_DUMP"):
        Path(os.environ["FAKE_CODEX_BOOTSTRAP_DUMP"]).write_text(json.dumps({"argv": args, "cwd": os.getcwd(), "prompt": prompt}))
    if bootstrap_mode == "fail":
        ev({"type": "turn.failed", "error": {"message": "synthetic bootstrap failure"}})
        sys.exit(1)
    if bootstrap_mode == "slow":
        time.sleep(float(os.environ.get("FAKE_CODEX_SLEEP", "5")))
    if bootstrap_mode != "missing":
        d = home / "sessions" / time.strftime("%Y/%m/%d")
        d.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        records = [
            {"type": "session_meta", "payload": {"id": thread, "cwd": os.getcwd()}},
            {"type": "response_item", "payload": {"type": "message", "role": "developer", "content": [
                {"type": "input_text", "text": "Synthetic worker instructions."}]}},
            {"type": "turn_context", "payload": {"model": model, "effort": effort}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
            {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": prompt}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": "READY"}]}},
            {"type": "event_msg", "payload": {"type": "agent_message", "message": "READY"}},
        ]
        if bootstrap_mode == "unknown_shape":
            records[4]["payload"]["content"] = [{"type": "input_text", "text": "Unexpected prompt"}]
        path = d / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S')}-{thread}.jsonl"
        path.write_text("{invalid json\n" if bootstrap_mode == "malformed" else "".join(
            json.dumps({"timestamp": ts, "ordinal": i, **record}) + "\n" for i, record in enumerate(records)))
    final.write_text("READY")
    ev({"type": "item.completed", "item": {"type": "agent_message", "text": "READY"}})
    ev({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 1}})
    sys.exit(0)
if resumed:
    unsupported = {"-s", "--sandbox", "-C", "--cd", "--add-dir"}.intersection(args)
    if unsupported:
        ev({"type": "turn.failed", "error": {"message": f"resume does not accept {sorted(unsupported)}"}})
        sys.exit(2)
    paths = list((home / "sessions").glob(f"**/rollout-*-{thread}.jsonl"))
    history = [json.loads(line) for line in paths[0].read_text().splitlines()]
    if os.environ.get("FAKE_CODEX_DUMP"):
        Path(os.environ["FAKE_CODEX_DUMP"]).write_text(json.dumps({
            "argv": args, "cwd": os.getcwd(), "cwd_files": os.listdir(), "prompt": prompt, "history": history}))
if os.environ.get("FAKE_EVENTS_FILE"):
    for line in Path(os.environ["FAKE_EVENTS_FILE"]).read_text().splitlines():
        ev(json.loads(line))
_leak = os.environ.get(os.environ.get("FAKE_CODEX_LEAK_ENV", ""), "")
if _leak:
    ev({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": f"thinking about {_leak}"}})
if os.environ.get("FAKE_CODEX_CLOBBER"):
    Path(os.environ["FAKE_CODEX_CLOBBER"]).write_text("rotated-key\n")
if os.environ.get("FAKE_CODEX_REMOVE"):
    Path(os.environ["FAKE_CODEX_REMOVE"]).unlink()
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
leak = os.environ.get(os.environ.get("FAKE_CODEX_LEAK_ENV", ""), "")
if leak:
    print(f"worker saw {leak}", file=sys.stderr)
texts = {"ok_task": "STATUS: DONE\nChanged a.py; ran pytest -q: 3 passed." + (f" key={leak}" if leak else ""),
         "ok_review": "VERDICT: REVISE\n1. lane:10 off-by-one. Fix: use <=.",
         "nomarker": "I did the thing.", "late_marker": "Here is my report.\nSTATUS: DONE", "noreport": "", "o_missing": "STATUS: DONE\nfrom the event stream"}
text = texts[mode]
if os.environ.get("FAKE_REPORT_FILE"):
    text = Path(os.environ["FAKE_REPORT_FILE"]).read_text()
if text:
    ev({"type": "item.completed", "item": {"id": "item_9", "type": "agent_message", "text": text}})
ev({"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 50}})
rollout(used + float(os.environ.get("FAKE_DELTA", "0.5")))
if mode != "o_missing":
    final.write_text(text)
sys.exit(0)
