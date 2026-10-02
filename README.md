# lane

`lane` runs one bounded coding-agent worker in the background (`codex exec`, or headless `claude -p`) and reports back in one JSON line. The full report stays on disk. It is meant to be called by another agent or a script: a Claude Code session, a workflow, a Codex orchestrator, or plain shell.

```console
$ lane run --brief add-flag.md --cwd ~/code/app --profile auto --sandbox workspace-write
{"lane_id": "20261001-214755-add-flag-5940", "status": "ok", "marker": "STATUS: DONE", "profile": "implement",
 "model": "gpt-6-astra", "effort": "high", "branch": "lane/20261001-214755-add-flag-5940",
 "worktree": "/Users/me/Projects/worktrees/lane-20261001-214755-add-flag-5940", "duration_s": 412.3,
 "quota_before": 41.0, "quota_after": 43.0, "quota_delta": 2.0,
 "report_path": "/Users/me/.lane/runs/20261001-214755-add-flag-5940/final.md", "summary": "STATUS: DONE\nAdded --verbose ..."}
```

(Trimmed; the real line has more fields.)

## Why use it

Several tools already let Claude Code hand work to Codex. If you want `/codex` commands inside Claude Code, OpenAI's [Codex plugin for Claude Code](https://github.com/openai/codex-plugin-cc) is simpler. `lane` is for running many bounded workers from scripts without running out of subscription quota or letting two workers edit the same files. Three things set it apart:

1. **It refuses work before you hit a usage limit.** Before each start, lane reads the rate-limit samples Codex writes to its own session logs. It refuses task lanes once the account reaches 90% of its weekly limit (95% for reviews, 98% of any shorter window such as the 5-hour one). After a worker hits a usage limit, lane stops using that Codex home for 6 hours. A Codex result records how much weekly quota the lane used whenever the before and after readings fall in the same weekly window.
2. **It picks the model with fixed rules, not tokens.** `--profile auto` reads the brief (owned paths, files to read, numbered steps, keywords) and picks a profile from `profiles.json`. A write lane whose Goal or Do mentions a risk keyword (broker, credential, payment and others) is refused until you name a profile. When a lane fails, `--retry-of <lane_id>` moves one step up a ladder you define, ending in `escalate_to_claude`: do it yourself.
3. **One writer per checkout.** A write lane reserves its whole repository checkout (or its own worktree), plus any `--add-dir`. A second write lane on an overlapping path is refused (`busy_worktree`). Lanes with `--worktree` each get their own checkout, so they don't clash, unless they share an `--add-dir`.

It also keeps the worker contained. Codex workers run without your user config, hooks, MCP servers or nested agents; Claude workers get the repo's project settings and no MCP servers. Every lane runs with an explicit model and effort, and the result also records the model the worker reported (and, for Codex, the effort). Secret-looking environment variables are stripped. A wall-clock limit kills the worker's whole process group, and if any of it survives, the lane ends in `error` and keeps its paths reserved. The report must open with `STATUS: DONE|BLOCKED` or `VERDICT: LGTM|REVISE`, so the caller never has to guess whether it worked.

It is one Python file with no dependencies.

## Install

Requirements: Python 3.9+, git, the [Codex CLI](https://github.com/openai/codex) signed in, and optionally the [Claude Code](https://code.claude.com/docs) CLI for Claude lanes. Tested on macOS. It uses only POSIX process and file-lock calls, so Linux should work, but it hasn't been tested there.

```bash
git clone https://github.com/marcmuon/lane.git
ln -s "$PWD/lane/lane" ~/.local/bin/lane

# lane runs Codex from its own home so it never touches your interactive config.
# Link your login into it: same account, same quota.
mkdir -p ~/.codex-lane && ln -s ~/.codex/auth.json ~/.codex-lane/auth.json
```

## Use

Write a brief. `--profile auto` reads these labels:

```markdown
Goal: add a --verbose flag to the CLI.
Repo: /Users/me/code/app. Owned paths: app/cli.py, tests/test_cli.py.
Read: app/cli.py.
Do:
1. Add --verbose that sets the log level to DEBUG.
2. Add a test.
Acceptance: `pytest -q tests/test_cli.py` passes.
```

Then:

```bash
lane start --brief b.md --cwd ~/code/app --profile auto --sandbox workspace-write   # returns at once
lane wait <lane_id> --max 540       # blocks; exits 3 with status "running" if --max runs out, so call it again
lane run   ...                      # start + wait
lane route ...                      # dry run: print the profile, model and effort it would use
lane result <lane_id>               # the result again
lane status [--all]                 # recent lanes
lane watch [<lane_id>]              # follow one lane, or all running lanes, as readable lines
lane quota [--codex-home DIR]       # weekly use for an account
lane cancel <lane_id>
```

Every command prints one JSON line on stdout. The exceptions: in a terminal, `status` and `result` print readable text (`LANE_JSON=1` keeps JSON), `watch` always prints text, and `--help` prints help.

Other `start` flags: `--kind task|review`, `--sandbox read-only|workspace-write`, `--worktree`, `--max-time 45m`, `--model` and `--effort` (each overrides one field of the profile), `--provider`, `--add-dir DIR`, `--network`, `--codex-home DIR`, `--wait-slot SECONDS` (wait for a free slot instead of returning `busy`), `--label`, and `--force` (skip the quota checks).

### Endings

| `status` | Meaning |
|---|---|
| `ok` | Finished, and the report's first non-blank line is the required marker (markdown bold is ignored). |
| `contract_violation` | Finished, but that line isn't exactly `STATUS: ...` or `VERDICT: ...`. |
| `no_report` | Finished with no final message. |
| `capped` | The worker hit a usage limit. The account is held for 6 hours. |
| `auth_401` | The worker's login failed. |
| `timeout`, `cancelled` | Stopped by `--max-time` or `lane cancel`. |
| `error` | Anything else. `summary` holds the worker's error or the tail of its stderr. |

Refusals, before anything runs: `busy` (no free slot), `busy_worktree` (another write lane holds the path), `capped_preflight`, `budget_spent`, `usage_error`, `bad_profile`, `unknown_profile`, `profile_disabled`, `owned_paths_missing`, `explicit_profile_required`, `escalate_to_claude`.

Exit codes: 0 success (finished `ok`, `started`, `routed`, cancel requested), 1 finished not ok or error, 2 usage or routing refusal, 3 still running, 4 no slot or path held (`busy`, `busy_worktree`), 5 quota refusal.

## Profiles

`profiles.json` maps an intent to provider, model, effort, sandbox, kind, worktree and time limit:

| Profile | Model and effort | Notes |
|---|---|---|
| `quick-investigate` | gpt-6-astra low | read-only, 15m |
| `investigate` | gpt-6-astra high | read-only, 30m |
| `draft` | gpt-6-astra low | read-only, 30m |
| `implement-light` | gpt-6-astra medium | own worktree, 45m |
| `implement` | gpt-6-astra high | own worktree, 60m |
| `implement-hard` | gpt-6-astra xhigh | own worktree, 90m |
| `review`, `review-deep` | gpt-6-astra high, xhigh | read-only, 30m and 45m |
| `opus`, `opus-review` | Claude Opus xhigh, high | headless `claude -p`; `opus` gets its own worktree |
| `luna-trial` | gpt-6-luna max | own worktree, 45m |
| `draft-local` | a local model | disabled until you set it up |

Ladders for `--retry-of`: implement-light → implement → implement-hard → opus → escalate_to_claude, and review → review-deep → escalate_to_claude.

The `auto` rules:
- A review goes to `review-deep` on any risk keyword, two or more hard keywords, or a brief over 12 KB.
- A write lane needs `Owned paths:` and no risk keyword. Four or more paths, two hard keywords, more than six steps or a brief over 4,096 bytes means `implement-hard`. A simple keyword, no hard keyword, at most two paths and under 2,000 bytes means `implement-light`. Everything else gets `implement`.
- A read-only brief with a draft keyword and no hard keyword gets `draft`. One with a simple keyword, no hard keyword, at most two files to read and under 1,500 bytes gets `quick-investigate`. Everything else gets `investigate`.

Keywords count only in the `Goal:` and `Do:` sections.

The keyword lists live in `profiles.json`.

To change profiles for one repo, put a `.lane/profiles.json` in it. Its `profiles`, `ladder` and `keywords` entries replace the global ones with the same name, and `lane route` shows `routing.project_overrides` when that happened.

## Providers

- `openai` (default): `codex exec` with `CODEX_HOME` set to the lane home.
- `claude`: headless `claude -p` with project settings only, no MCP servers and no slash commands. Its `git push`, `gh`, web fetch and web search tools are denied; these are tool rules, not a sandbox (see Known limits). The repo's `CLAUDE.md` and `AGENTS.md` are passed in as rules. New Claude lanes are refused once the last 7-day reading Claude Code reported reaches 80%.
- `deepseek`, `spark`: experimental and untested. They point Codex at DeepSeek's API or at a local vLLM server. Keys come from the environment or the macOS Keychain.

## Configuration

| Variable | Default | |
|---|---|---|
| `LANE_CODEX_HOME` | `~/.codex-lane` | Codex home for lanes without `--codex-home` |
| `LANE_STATE` | `~/.lane` | run directories, lock, Claude quota cache |
| `LANE_WORKTREE_ROOT` | `~/Projects/worktrees` | where `--worktree` lanes get their checkout |
| `LANE_MAX_CONCURRENT` | 4 | lanes running at once, across all callers |
| `LANE_QUOTA_STOP`, `LANE_QUOTA_STOP_REVIEW`, `LANE_QUOTA_STOP_SHORT` | 90, 95, 98 | refusal thresholds in percent |
| `LANE_WEEKLY_POINTS` | 0 (off) | optional cap on weekly quota points task lanes may use per account |
| `LANE_CLAUDE_WEEKLY_STOP` | 0.80 | 7-day utilization at which Claude lanes stop |
| `LANE_CLAUDE_MAX_TURNS` | 250 | |
| `LANE_PROFILES` | `profiles.json` next to `lane` | |
| `LANE_CODEX_BIN`, `LANE_CLAUDE_BIN` | `codex`, `claude` | |
| `LANE_DEEPSEEK_CATALOG`, `LANE_SPARK_BASE_URL`, `LANE_SPARK_KEYCHAIN_SERVICE` | | experimental providers |

Each run lives in `$LANE_STATE/runs/<lane_id>/`, which can hold `brief.md` (with the output contract appended), `params.json`, `argv.json`, `events.jsonl`, `stderr.log`, `final.md` and `result.json`.

## From Claude Code

[`examples/claude-code-agent.md`](examples/claude-code-agent.md) is a small Haiku subagent that runs `lane start`, waits, and returns the JSON verbatim. Copy it to `~/.claude/agents/lane.md` and call it from a workflow with `agent(prompt, {agentType: 'lane', model: 'haiku'})`. From a main session, run `lane run ...` as a background Bash command instead.

## Known limits

- **Claude write lanes have no filesystem sandbox.** Codex workers run inside Codex's OS sandbox. A `claude` lane with `--sandbox workspace-write` can run any shell command as you. Run Claude write lanes with `--worktree`, on repos you trust.
- **Quota attribution is approximate.** Codex reports only account-wide usage, so a lane's cost is the change in account usage during its run, split evenly across lanes that overlapped it. Your own interactive use during a lane is charged to that lane, which errs on the strict side.
- **Worktree lanes share the repo's `.git`.** A worktree lane gets write access to the shared git directory so it can commit. Git's own locks protect refs and objects.
- **Reports aren't length-checked.** The task contract asks for at most 150 words; lane only checks the first line.

## Tests

```bash
python3 -m unittest discover -s tests
```

About 90 seconds. Codex and Claude are replaced by small fakes in `tests/`, so the suite spends no quota.

## License

MIT
