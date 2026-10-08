# lane

**Background jobs for coding agents.**

Lane runs coding tasks with Codex or Claude and returns a structured result with a saved report. Call it from your coding agent or a shell script to delegate implementation, investigation, and review.

Your main agent plans a change, hands a specific task to Lane, and continues other work while the worker runs. Lane checks subscription quota before starting, applies a time limit, and can give each worker its own Git checkout.

```bash
lane start --brief /tmp/add-flag.md --cwd ~/code/app --profile implement
```

This starts a Codex worker in a separate Git worktree and returns a job ID. Use `lane run` with the same arguments to wait for the result instead.

## Why use it

- **Check quota before launching.** Refuse new work when recorded subscription usage reaches your configured limits.
- **Keep concurrent writers apart.** Give workers separate Git worktrees, or refuse a second writer to an occupied checkout.
- **Choose model settings from presets.** Pick a profile, or let fixed rules choose one from the task description.
- **Get results your agent can use.** Receive JSON with the worker's outcome, report path, and worktree, with full logs saved on disk.

Lane is one Python file with no third-party Python dependencies. It uses your existing Codex or Claude CLI login.

## Install

Requires Python 3.9+, Git, and the [Codex CLI](https://github.com/openai/codex) signed in for the quick start below. [Claude Code](https://code.claude.com/docs) is optional for Claude workers. Tested on macOS; Linux is untested.

```bash
git clone https://github.com/marcmuon/lane.git
mkdir -p ~/.local/bin
ln -s "$PWD/lane/lane" ~/.local/bin/lane
export PATH="$HOME/.local/bin:$PATH"

# Give Lane a separate Codex home, sharing your existing login and quota.
mkdir -p ~/.codex-lane
ln -s ~/.codex/auth.json ~/.codex-lane/auth.json
```

## Run your first task

Use an existing Git repository. The example below assumes a Python CLI at `~/code/app`; replace the project and file paths with your own.

**1. Write a brief.** Save this task description as `/tmp/add-flag.md`:

```markdown
Goal: add a --verbose flag to the CLI.
Owned paths: app/cli.py, tests/test_cli.py.
Read: app/cli.py, tests/test_cli.py.
Do:
1. Add --verbose that sets the log level to DEBUG.
2. Add a test.
Acceptance: `pytest -q tests/test_cli.py` passes.
```

**2. Run it.** The `implement` profile selects the model and effort, creates a separate Git worktree, and allows up to 60 minutes:

```bash
lane run --brief /tmp/add-flag.md --cwd ~/code/app --profile implement
```

A worktree is a separate checkout on its own branch. It starts from your repository's current commit, so commit any changes the worker needs first.

**3. Inspect the result.** Lane prints one JSON line. Selected fields from a successful run look like this:

```json
{"lane_id":"…","status":"ok","marker":"STATUS: DONE","worktree":"…","report_path":"…"}
```

Run `lane result <lane_id>` for a readable summary in your terminal. Read the file at `report_path` and inspect the changes in `worktree` before integrating them. The `marker` reports whether the worker says it finished or was blocked; `status: ok` alone does not mean the task was completed.

To return immediately and check back later:

```bash
lane start --brief /tmp/add-flag.md --cwd ~/code/app --profile implement
lane watch <lane_id>
lane result <lane_id>
```

## Use it from your agent

Give your agent the repository path, a task brief, and a profile. It can call `lane start`, continue other work, then call `lane wait <lane_id>` to collect the JSON result. The calling agent decides what to do next.

For Claude Code, copy the [example relay agent](examples/claude-code-agent.md) to `~/.claude/agents/lane.md`. It starts a worker, waits, and returns the result to the caller. From a main Claude Code session, you can also run `lane run` as a background Bash command.

Common profiles are `investigate` for read-only exploration, `implement` for code changes in a worktree, `review` for a review, and `opus` for implementation with Claude. To let Lane choose a Codex write profile, use `--profile auto --sandbox workspace-write` with `Owned paths:` in the brief. See the [profile reference](docs/reference.md#profiles) for routing rules and retries.

## Answer a brief or research the web

Use `--answer-only` to get a written answer from Codex or Claude. Lane sends the brief unchanged, with a minimal instruction to answer directly. It adds no output contract, repository rules, or skills instructions. The answer is returned in `summary` and saved to `report_path`; no `STATUS:` or `VERDICT:` marker is required.

```bash
lane run --answer-only --brief /tmp/idea-card.md --model gpt-6-astra
lane run --answer-only --brief /tmp/idea-card.md --provider claude --model opus
```

`--cwd` is optional and ignored in this mode. Lane creates an empty temporary directory, uses read-only permissions, performs no Git operations, and removes the directory when the worker finishes. `--worktree`, `--network`, `--add-dir`, and `--sandbox workspace-write` are rejected. Profiles can still choose the model, effort, and time limit; their write/worktree settings are overridden and repository profiles are not loaded.

Add `--web-research` to permit only the keyless Exa MCP server:

```bash
lane run --answer-only --web-research --brief /tmp/idea-card.md --model gpt-6-astra
lane start --answer-only --web-research --brief /tmp/idea-card.md --provider claude --model opus
```

Both flags work with `start` and `run` (and can be previewed with `route`). `--web-research` requires `--answer-only`. Its only server is `exa`, at `https://mcp.exa.ai/mcp?tools=web_search_exa,web_fetch_exa,web_search_advanced_exa`. Only `mcp__exa__web_search_exa`, `mcp__exa__web_fetch_exa`, and `mcp__exa__web_search_advanced_exa` are allowed. Lane never configures a keyed Exa server or the claude.ai Exa connector. MCP networking does not require granting shell network access.

Claude gets `--tools ""`, `--strict-mcp-config`, `--disable-slash-commands`, an empty settings-source list, disabled hooks and memory/rules loading, and a replacement system prompt. Its turn limit is 2 for answers and 8 for web research. Research adds a single-server MCP config and an exact tool allowlist.

Codex gets `--ignore-user-config`, disabled shell commands, image, browser, search, plugin, agent, memory, goal, and other optional tools, explicit `web_search="disabled"`, and disabled plan/input tools. Plain `--answer-only` also disables `code_mode`, `code_mode_only`, `code_mode_host`, and `code_mode_prewarm`. Web research leaves those four features at their defaults so Codex can reach Exa through the JavaScript `exec` wrapper. Research configures only `mcp_servers.exa`, with the three enabled tools.

**Codex CLI limitation:** checked against the installed 0.161.0 binary's feature names and [upstream tool registration](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/spec_plan.rs). Some tools are selected by model metadata instead of feature switches: `--disable apply_patch_freeform` does not guarantee removal of `apply_patch`; clock, asynchronous message, or model-forced code-mode tools may remain. With an MCP server, Codex also registers `list_mcp_resources`, `list_mcp_resource_templates`, and `read_mcp_resource` without a separate off switch. Lane cannot promise an empty/exact-only Codex tool surface through CLI settings alone. The read-only sandbox and the audit below are still enforced; live provider validation must check the installed model/CLI combination.

After either mode finishes, Lane audits tool calls in the event log and, for Codex, its raw rollout (the CLI event stream omits some tools). Codex web research permits the `exec` wrapper and reading `ALL_TOOLS`. Its input must use direct `tools.mcp__exa__<tool>` references: references to other tools, computed `tools[...]` access, aliases of `tools`, and unclassifiable inputs are rejected. Nested calls must use server `exa` and one of the three allowed tools; unknown activity is rejected. `exa_calls` counts the nested Exa calls, once across start/end events, rather than counting the wrapper.

Any forbidden call, or any tool call (including `exec`) without `--web-research`, produces `status: "tool_used"`, `reason: "tool_used"`, and exit code 1. **Discard that answer.** Results include `exa_calls`, `disallowed_tools`, `answer_only`, and `web_research`. The audit detects a violation after the run; it is not a tool execution firewall. Existing coding modes retain their current behavior.

## Commands

| Command | What it does |
| --- | --- |
| `lane run` | Start a worker and wait for its result |
| `lane start` | Start a worker and return immediately |
| `lane wait <lane_id>` | Wait for a running worker's result |
| `lane result <lane_id>` | Read a saved result |
| `lane status` / `lane watch` | List recent jobs or follow running jobs |
| `lane route` | Preview profile selection without starting a worker |
| `lane quota` | Check recorded account usage |
| `lane cancel <lane_id>` | Stop a worker |

`run`, `start`, and `route` take the brief and repository arguments shown above. Script output is JSON; `status` and `result` show readable text in a terminal, and `watch` always shows text. [Full command reference and exit codes →](docs/reference.md#commands)

## Known limits

- **Claude write workers have no filesystem sandbox.** A worktree separates checkouts, but does not restrict shell access. Run Claude write workers only on repositories you trust. Codex workers use Codex's OS sandbox.
- **Quota readings are account-wide and approximate.** Overlapping workers and your interactive sessions affect the measurements. Checks before launch cannot guarantee a job will finish before a usage limit.
- **Worktrees share the repository's Git metadata.** Each worktree has separate files, but shares the Git directory needed for commits.
- **Reports still need review.** Lane checks the required opening marker, not the correctness of the work. Lane imposes no report length limit.

## Reference

- [Profiles, routing rules, and retries](docs/reference.md#profiles)
- [Quota checks and thresholds](docs/reference.md#quota-checks)
- [Worker isolation](docs/reference.md#worker-isolation)
- [Your own profiles, kept out of the repo](docs/reference.md#your-own-profiles)
- [Providers, including a self-hosted model server](docs/reference.md#providers)
- [Configuration](docs/reference.md#configuration)
- [Tests](docs/reference.md#tests)

If you mainly want `/codex` commands inside Claude Code, see OpenAI's [Codex plugin for Claude Code](https://github.com/openai/codex-plugin-cc). Lane is aimed at scripts and agents that need to manage repeated or concurrent jobs.

## License

[MIT](LICENSE)
