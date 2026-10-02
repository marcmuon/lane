# Lane reference

[Back to the quick start](../README.md)

- [Commands](#commands)
- [Result example](#result-example)
- [Quota checks](#quota-checks)
- [Checkout locking](#checkout-locking)
- [Worker isolation](#worker-isolation)
- [Profiles](#profiles)
- [Your own profiles](#your-own-profiles)
- [Providers](#providers)
- [Configuration](#configuration)
- [Tests](#tests)

## Commands

```bash
lane start --brief /tmp/add-flag.md --cwd ~/code/app --profile auto --sandbox workspace-write   # returns at once
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

### Results and exit codes

| `status` | Meaning |
|---|---|
| `ok` | Finished, and the report's first non-blank line is the required marker (markdown bold is ignored). |
| `contract_violation` | Finished, but that line isn't exactly `STATUS: ...` or `VERDICT: ...`. |
| `no_report` | Finished with no final message. |
| `capped` | The worker hit a usage limit. The account is held for 6 hours. |
| `auth_401` | The worker's login failed. |
| `timeout`, `cancelled` | Stopped by `--max-time` or `lane cancel`. |
| `error` | Anything else. `summary` holds the worker's error or the tail of its stderr. |

Refusals, before anything runs: `busy` (no free slot), `busy_worktree` (another write lane holds the path), `provider_unavailable` (a self-hosted server didn't answer), `capped_preflight`, `budget_spent`, `usage_error`, `bad_profile`, `unknown_profile`, `profile_disabled`, `model_not_served`, `owned_paths_missing`, `explicit_profile_required`, `escalate_to_claude`.

Exit codes: 0 success (finished `ok`, `started`, `routed`, cancel requested), 1 finished not ok or error, 2 usage or routing refusal, 3 still running, 4 no slot, path held or server down (`busy`, `busy_worktree`, `provider_unavailable`), 5 quota refusal.

## Result example

The JSON result includes the worker's report marker, selected model and effort, worktree, timing, and quota readings when available:

```console
$ lane run --brief /tmp/add-flag.md --cwd ~/code/app --profile auto --sandbox workspace-write
{"lane_id": "20261001-214755-add-flag-5940", "status": "ok", "marker": "STATUS: DONE", "profile": "implement",
 "model": "gpt-6-astra", "effort": "high", "branch": "lane/20261001-214755-add-flag-5940",
 "worktree": "/Users/me/Projects/worktrees/lane-20261001-214755-add-flag-5940", "duration_s": 412.3,
 "quota_before": 41.0, "quota_after": 43.0, "quota_delta": 2.0,
 "report_path": "/Users/me/.lane/runs/20261001-214755-add-flag-5940/final.md", "summary": "STATUS: DONE\nAdded --verbose ..."}
```

This example is shortened; the real line has more fields. `status: ok` means the worker exited successfully and supplied a valid report marker. Read `marker` to distinguish `STATUS: DONE` from `STATUS: BLOCKED`, or `VERDICT: LGTM` from `VERDICT: REVISE`. It does not independently prove the task succeeded.

## Quota checks

Before each start, lane reads the rate-limit samples Codex writes to its own session logs. It refuses task lanes once the account reaches 90% of its weekly limit (95% for reviews, 98% of any shorter window such as the 5-hour one). After a worker hits a usage limit, lane stops using that Codex home for 6 hours. A Codex result records how much weekly quota the lane used whenever the before and after readings fall in the same weekly window.

Quota checks use recorded account usage, so they cannot guarantee a worker will finish before a limit is reached. A lane's measured usage is the change in account usage during its run, divided equally among lanes that overlapped it. Your interactive usage during that time contributes to the same total. See [known limits](../README.md#known-limits).

## Checkout locking

A write lane reserves its whole repository checkout (or its own worktree), plus any `--add-dir`. A second write lane on an overlapping path is refused (`busy_worktree`). Lanes with `--worktree` each get their own checkout, so they don't clash, unless they share an `--add-dir`.

## Worker isolation

Codex workers run without your user config, hooks, MCP servers or nested agents; Claude workers get the repo's project settings and no MCP servers. Every lane runs with an explicit model and effort, and the result also records the model the worker reported (and, for Codex, the effort). Secret-looking environment variables are stripped. A wall-clock limit kills the worker's whole process group, and if any of it survives, the lane ends in `error` and keeps its paths reserved. A worktree lane's brief also names its working copy, so a model that follows the brief's `Repo:` path doesn't edit the original checkout. The report must open with `STATUS: DONE|BLOCKED` or `VERDICT: LGTM|REVISE`. Lane checks this report format; the caller still needs to assess the result.

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
| `draft-local` | whatever your self-hosted server serves | disabled until you set it up (see [Providers](#providers)) |

Ladders for `--retry-of`: implement-light → implement → implement-hard → opus → escalate_to_claude, and review → review-deep → escalate_to_claude.

The `auto` rules:

- A review goes to `review-deep` on any risk keyword, two or more hard keywords, or a brief over 12 KB.
- A write lane needs `Owned paths:` and no risk keyword. Four or more paths, two hard keywords, more than six steps or a brief over 4,096 bytes means `implement-hard`. A simple keyword, no hard keyword, at most two paths and under 2,000 bytes means `implement-light`. Everything else gets `implement`.
- A read-only brief with a draft keyword and no hard keyword gets `draft`. One with a simple keyword, no hard keyword, at most two files to read and under 1,500 bytes gets `quick-investigate`. Everything else gets `investigate`.

Keywords count only in the `Goal:` and `Do:` sections.

The keyword lists live in `profiles.json`.

## Your own profiles

Profiles load in three layers: the shipped `profiles.json`, then your own `~/.lane/profiles.json` (`$LANE_STATE/profiles.json`), then `<repo>/.lane/profiles.json` for lanes started in that repo. Each layer replaces whole entries by name in its `profiles`, `ladder`, `keywords` and `providers` sections, and `lane route` shows `routing.user_overrides` and `routing.project_overrides` when a layer applied.

Keep machine-specific settings, like a server address, in your own file rather than in a repository. Only your own file can set `providers`; a repo's `.lane/profiles.json` that tries is an error, so a checkout can't send your API key to a server of its choosing.

## Providers

- `openai` (default): `codex exec` with `CODEX_HOME` set to the lane home.
- `claude`: headless `claude -p` with project settings only, no MCP servers and no slash commands. Its `git push`, `gh`, web fetch and web search tools are denied; these are tool rules, not a sandbox (see [known limits](../README.md#known-limits)). The repo's `CLAUDE.md` and `AGENTS.md` are passed in as rules. New Claude lanes are refused once the last 7-day reading Claude Code reported reaches 80%.
- `spark`: Codex against a self-hosted, OpenAI-compatible server that speaks the Responses API, such as vLLM on a DGX Spark. See [Self-hosted server](#self-hosted-server).
- `deepseek`: experimental and untested. Points Codex at DeepSeek's API; the key comes from `DEEPSEEK_API_KEY` or the macOS Keychain.

### Self-hosted server

Describe the server in your own `~/.lane/profiles.json`:

```json
{
  "providers": {"spark": {"base_url": "http://my-server:8000/v1", "api_key_file": "~/.config/my-server/api-key"}},
  "profiles": {"spark": {"provider": "spark", "model": "auto", "effort": "medium", "sandbox": "read-only",
                         "kind": "task", "max_time": "30m"}},
  "ladder": {"spark": "investigate"}
}
```

Then run `lane run --brief ... --cwd ... --profile spark`. `auto` routing never picks a self-hosted profile; name it.

- **Model.** `"model": "auto"` asks the server's `/models` at each start and uses the one model it serves, so switching models on the server needs no profile change. It refuses if the server lists none or several. A pinned model that isn't being served is refused with `model_not_served`. lane also passes the served context length to Codex.
- **Availability.** A start is refused with `provider_unavailable` (exit 4) if the server doesn't answer `/models`, so no worker runs against a server that is down.
- **Key.** Use `api_key_file`, or `api_key_keychain` (a Keychain service name). With neither, lane uses the Keychain service in `LANE_SPARK_KEYCHAIN_SERVICE` if set, and otherwise sends no key. A configured key source that is empty, unreadable or more than one line is refused, not skipped. lane sends the key only to `base_url`, without following redirects or using a proxy. It reaches Codex in an environment variable that commands the model runs can't see. If the worker prints it anyway, `lane watch` shows `<redacted>` in its place, and lane replaces it in the run's records when the run ends. Until then, or if the supervisor dies mid-run, the raw files in the run directory can still hold it.
- **Effort.** Some models accept only certain reasoning efforts. If the server rejects one with HTTP 400, change the profile's `effort`.

## Configuration

| Variable | Default | |
|---|---|---|
| `LANE_CODEX_HOME` | `~/.codex-lane` | Codex home for lanes without `--codex-home` |
| `LANE_STATE` | `~/.lane` | run directories, your own `profiles.json`, lock, Claude quota cache |
| `LANE_WORKTREE_ROOT` | `~/Projects/worktrees` | where `--worktree` lanes get their checkout |
| `LANE_MAX_CONCURRENT` | 4 | lanes running at once, across all callers |
| `LANE_QUOTA_STOP`, `LANE_QUOTA_STOP_REVIEW`, `LANE_QUOTA_STOP_SHORT` | 90, 95, 98 | refusal thresholds in percent |
| `LANE_WEEKLY_POINTS` | 0 (off) | optional cap on weekly quota points task lanes may use per account |
| `LANE_CLAUDE_WEEKLY_STOP` | 0.80 | 7-day utilization at which Claude lanes stop |
| `LANE_CLAUDE_MAX_TURNS` | 250 | |
| `LANE_PROFILES` | `profiles.json` next to `lane` | |
| `LANE_CODEX_BIN`, `LANE_CLAUDE_BIN` | `codex`, `claude` | |
| `LANE_SPARK_BASE_URL`, `LANE_SPARK_KEYCHAIN_SERVICE` | | self-hosted server, if not set in your profiles file |
| `LANE_DEEPSEEK_CATALOG` | | experimental DeepSeek provider |

Each run lives in `$LANE_STATE/runs/<lane_id>/`, which can hold `brief.md` (with the output contract appended), `params.json`, `argv.json`, `events.jsonl`, `stderr.log`, `final.md` and `result.json`.

## Tests

```bash
python3 -m unittest discover -s tests
```

About 90 seconds. Codex and Claude are replaced by small fakes in `tests/`, so the suite spends no quota.
