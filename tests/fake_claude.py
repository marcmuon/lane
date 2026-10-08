#!/usr/bin/env python3
"""Stand-in for `claude -p --output-format stream-json` in lane's tests. FAKE_CLAUDE_MODE picks the ending."""
import json, os, sys, uuid
from pathlib import Path
args = sys.argv[1:]
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
prompt = sys.stdin.read()
sid = str(uuid.uuid4())
model = args[args.index("--model") + 1]
dump = os.environ.get("FAKE_CLAUDE_DUMP")
if dump:
    sysflag = "--system-prompt-file" if "--system-prompt-file" in args else "--append-system-prompt-file"
    sysf = args[args.index(sysflag) + 1]
    json.dump({"argv": args, "env_advisor": os.environ.get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL"),
               "env_token": os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN"), "system": open(sysf).read(),
               "env_no_rules": os.environ.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS"),
               "cwd": os.getcwd(), "cwd_files": os.listdir(), "prompt": prompt}, open(dump, "w"))
def ev(o): print(json.dumps(o), flush=True)
ev({"type": "system", "subtype": "init", "model": "claude-opus-5-5" if model == "opus" else model, "session_id": sid})
if os.environ.get("FAKE_EVENTS_FILE"):
    for line in Path(os.environ["FAKE_EVENTS_FILE"]).read_text().splitlines():
        ev(json.loads(line))
ev({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "seven_day", "utilization": float(os.environ.get("FAKE_CLAUDE_UTIL", "0.3")),
    "resetsAt": 4102444800, "unifiedWindows": {"seven_day": {"utilization": float(os.environ.get("FAKE_CLAUDE_UTIL", "0.3")), "resetsAt": 4102444800},
    "five_hour": {"utilization": 0.1}}}})
if mode == "capped":
    ev({"type": "assistant", "error": "rate_limit", "message": {"content": [{"type": "text", "text": "You've hit your session limit · resets 3:45pm"}]}})
    ev({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "You've hit your session limit", "session_id": sid})
    sys.exit(1)
text = {"ok": "STATUS: DONE\nChanged a.py; tests pass.", "review": "VERDICT: LGTM\nNo findings.", "nomarker": "done."}[mode]
if os.environ.get("FAKE_REPORT_FILE"):
    with open(os.environ["FAKE_REPORT_FILE"]) as report:
        text = report.read()
ev({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
ev({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid, "usage": {"input_tokens": 900, "output_tokens": 40}})
