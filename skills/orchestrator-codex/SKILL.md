---
name: orchestrator-codex
description: >-
  Coordinate Antigravity (Gemini) and a Codex CLI backend subagent via project-owned
  thread UUIDs. Use for explicit Codex delegation, session management, and independent
  acceptance of backend, logic, or audit work. Gemini owns user communication and
  frontend decisions. Keep small intuitive edits, frontend-only work, general Q&A,
  and user-requested single-agent work in Gemini.
---

# Orchestrator-Codex Dual-Window Workflow

Gemini leads the workflow, owns the frontend and user-facing copy, and communicates with the user. Codex handles backend logic, scripts, audits, and tests within the work order. Gemini manages Git, dispatch, and independent review.

## 1. Clarify scope and agree on a branch

- Before writing a contract or dispatching, clarify goals, business rules, edge cases, interfaces, permissions, and the target branch. Give the user 2–3 practical options with a recommendation when a decision is needed. Record the decision in the work order.
- Choose `AUDIT` for read-only investigation, `SURGICAL_FIX` for a limited fix with a targeted regression test, or `FEATURE_IMPL` for a feature, multi-file refactor, or high-risk change. Handle intuitive configuration or typo edits of up to 10 lines directly when they have no interface or safety effects.
- For `FEATURE_IMPL`, Gemini first reads or authors the minimum `docs/architecture.md` blueprint and records the interface, data, error, and edge-case decisions in the contract. Obtain the user's agreement on both before dispatch. Use the [architecture](./references/architecture.template.md), [contract](./references/contract.template.md), and [task](./references/task.template.md) templates.
- `AUDIT` stays on the current branch. Record the HEAD, staged, unstaged, and untracked baseline. Audit a dirty tree in place. Keep the working tree and Git state as found, with `git stash` forbidden.
- A `SURGICAL_FIX` may target `main` only when the user explicitly approves, the working tree is clean, and the change is confined to a small allowlisted fix and its regression test. Public interfaces, schemas, dependencies, permission boundaries, and multi-module changes require a confirmed `fix/<slug>` branch. Use a confirmed `feat/<slug>` or `fix/<slug>` branch for `FEATURE_IMPL`. Gemini creates or switches to the agreed branch.
- Record `Git Branch`, `Baseline Commit`, and `Branch Switched` in `task.md`. On the first dispatch after switching, require Codex to reread the target files from disk. Keep Git writes with Gemini. Gemini obtains approval before commits or merges.
- Before medium or large work, obtain confirmation of the role split, architecture or contract, branch, task mode, and Codex setup. If the target repository uses the optional [AGENTS template](./references/AGENTS.template.md), apply its corresponding task mode.

## 2. Verify CLI capability and resolve a session

On Windows use `where.exe codex`. On Bash or Zsh use `type -a codex`. Check `codex --version` and `codex queue --help` on the selected CLI. Require `queue --resume`, local `-C`, and a daemon that supports queueing if using shared local app-server. For isolated desktops, verify `codex app-server daemon --help` and `codex app-server proxy --help`. Use the same resolved executable and `CODEX_HOME` for the launcher, the session store, and subsequent queue calls.

Check that `.agent-collab/` is Git-ignored before creating project collaboration files. Use absolute project paths in commands. When overriding Codex home, pass the same `--codex-home` to the Python entry points and set that `CODEX_HOME` in the shell running Codex.

```bash
python -B "<SKILL_DIR>/scripts/find_codex_thread.py" --cwd "<ABS_PROJECT_ROOT>"
```

- `CACHED` and `AUTO_SELECTED` indicate a verified live binding. Reuse its `THREAD_ID` when the session's effective permissions fit the task and the user has not requested reconfiguration. Daemon bindings use `thread/loaded/list` and `thread/read`. Visible discovery uses a live local rollout writer. A past rollout alone is historical.
- `MULTIPLE_MATCHES` requires the user's selection. Show UUID short code, model, effort, last activity, and first substantive message. Show whether the list was truncated. Include option `[0] Open and configure a new session`. Pass a chosen live UUID to `--select`. An explicit selection needs proof of project ownership and liveness.
- `NOT_FOUND`, `HISTORICAL`, `DAEMON_SESSION_OFFLINE`, and `SANDBOX_SESSION_OFFLINE` lead to the new-session flow unless the user explicitly requests historical resume. `--refresh` rechecks the binding and preserves managed ownership and stopped records. `START_PENDING` means a visible launch is waiting for its readiness message. Check the window and refresh.
- `SESSION_PROBE_FAILED` leaves liveness unknown. Resolve the failure before launching or dispatching. For queue failures, refresh and determine whether the particular delivery was accepted before retrying. A resume warning can accompany queue exit `0`. Inspect stderr as well as the exit code.

```bash
python -B "<SKILL_DIR>/scripts/find_codex_thread.py" --cwd "<ABS_PROJECT_ROOT>" --refresh
python -B "<SKILL_DIR>/scripts/find_codex_thread.py" --cwd "<ABS_PROJECT_ROOT>" --select "<UUID>"
```

## 3. Start, resume, or stop

For a new or resumed session, require the user's explicit model and reasoning effort. Probe the local catalog when either choice is missing. The output identifies `models_cache.json` as its source. Default output excludes hidden models. Use `--include-hidden` explicitly and `--limit` to cap the listed models. A missing catalog requires the user to verify model availability.

```bash
python -B "<SKILL_DIR>/scripts/start_codex_window.py" --list-models
```

| Task mode | Windows Default, macOS, Linux | Windows isolated `exebox-*` |
|---|---|---|
| `AUDIT` | `--sandbox read-only` | `--sandbox read-only --approval never` |
| `SURGICAL_FIX` or `FEATURE_IMPL` | `--sandbox workspace-write --approval on-request` | `--sandbox workspace-write --approval never` |

Permissions are part of the user's decision. Require explicit authorization for `danger-full-access` or for `never` on a visible session. Use a verified `read-only` session for `AUDIT` and reconfigure or use an authorized read-only fallback if the current session has writable permissions. A failed or unsupported Windows desktop probe returns `DESKTOP_UNAVAILABLE`.

**Visible Windows Default, macOS, and Linux**

```bash
python -B "<SKILL_DIR>/scripts/start_codex_window.py" --cwd "<ABS_PROJECT_ROOT>" --model "<MODEL>" --effort "<EFFORT>" --sandbox "<SANDBOX>" --timeout 25
```

For visible writing tasks, also pass `--approval on-request`. Launcher success is confirmed by a unique readiness message in the target rollout. If multiple uncorrelated new sessions appear, ask the user to select one. Confirm directory trust or login prompts in the visible window when necessary, then run discovery with `--refresh`.

**Isolated Windows desktop**

Prepare the work order and the first `TASK_MESSAGE` from section 4 before launching. Use `--dispatch-message` on the same RPC connection as `thread/start` so startup does not depend on a fresh idle thread remaining loaded until another CLI invocation.

```bash
python -B "<SKILL_DIR>/scripts/start_codex_window.py" --cwd "<ABS_PROJECT_ROOT>" --model "<MODEL>" --effort "<EFFORT>" --sandbox "<SANDBOX>" --approval never --dispatch-message "<TASK_MESSAGE>" --timeout 25
```

`STARTED_AND_BOUND` or `RESUMED_AND_BOUND` with `DISPATCH_STATE: started` and `TURN_ID` confirms that the daemon accepted and started this delivery. Use that turn ID in transcript wait. `ALREADY_BOUND` keeps an existing binding and leaves this invocation's message unsent. `CONFIG_MISMATCH` stops before dispatch when the returned model, effort, sandbox, or approval is different. `DISPATCH_UNKNOWN` requires checking the rollout or queue before any retry. Treat `DAEMON_UNAVAILABLE` as a startup failure and use its diagnostic to resolve the CLI or daemon prerequisite.

**Historical resume and cleanup**

List history only when the user asks to continue a saved conversation. Show numbered entries and any truncation. Confirm the UUID, model, effort, and sandbox before resuming.

```bash
python -B "<SKILL_DIR>/scripts/find_codex_thread.py" --cwd "<ABS_PROJECT_ROOT>" --list-history
python -B "<SKILL_DIR>/scripts/start_codex_window.py" --cwd "<ABS_PROJECT_ROOT>" --resume "<UUID>" --model "<MODEL>" --effort "<EFFORT>" --sandbox "<SANDBOX>" --timeout 25
python -B "<SKILL_DIR>/scripts/start_codex_window.py" --cwd "<ABS_PROJECT_ROOT>" --resume "<UUID>" --model "<MODEL>" --effort "<EFFORT>" --sandbox "<SANDBOX>" --approval never --dispatch-message "<TASK_MESSAGE>" --timeout 25
```

The first resume command targets a visible window. Append `--approval on-request` for an authorized visible writing task. The second resume command targets the daemon and sends the delivery in the same connection using `excludeTurns: true` on `thread/resume`. Both paths validate UUID ownership. Use `--force-new` only for a user-authorized replacement, including an explicit replacement before a resume. Visible windows remain under user control. For a live managed binding, replacement stops if safe cleanup fails.

```bash
python -B "<SKILL_DIR>/scripts/start_codex_window.py" --cwd "<ABS_PROJECT_ROOT>" --stop
```

In daemon mode, read `TURN_STATUS` and `UNSUBSCRIBE_STATUS` separately. `STOPPED` confirms the project binding is marked stopped after the turn is verified idle or terminal. `notSubscribed` applies only to the current proxy. `SKIPPED_VISIBLE_WINDOW` directs the user to close the window. A legacy background PID requires project UUID ownership and a matching Windows process fingerprint. A legacy PID-only record lacks the evidence required for automatic termination. `--dry-run` is for start or resume planning. Combining it with `--stop` returns `INVALID_ARGUMENTS`.

## 4. Dispatch and wait

Use one delivery ID and one acceptance round for each dispatch. Pass the complete task message as one argument. Start it with the exact marker and include the mode, authorized scope, verification, project branch, and expected result. Use a fresh delivery ID when sending a clarification follow-up in the same round.

```text
[orchestrator-task:TASK-001-a1 round:1]
Read the project architecture and the agreed work order or the inline scope below. Follow the allowed paths and verification requirements. Report the exact blocker and options if a user decision is needed.
```

For `AUDIT`, use the same structured result in Codex's final reply, with `Status`, `Task ID`, and `Round` in the first 20 lines. Use `Status: READY_FOR_REVIEW` or `Status: BLOCKED_NEED_CLARIFICATION`. Codex reads only and does not create report or flag files. Gemini may record the reply if authorized. When the entire project must stay write-free, use an inline work order and an already verified read-only binding or the approved stdout fallback.

For writing tasks, have Codex write `report.md` under `.agent-collab/results/<TASK_ID>/<ROUND>/` following the [task template](./references/task.template.md). The report starts with the same three fields. When it is complete, atomically publish its sibling `done.flag` containing JSON with the current `task_id` string and `round` integer. Gemini prepares the directory and checks cleanup before dispatch.

```bash
python -B "<SKILL_DIR>/scripts/wait_codex_done.py" --cwd "<ABS_PROJECT_ROOT>" --clean --task-id "<TASK_ID>" --round <ROUND>
```

Use this full queue command for rounds 1, 2, and 3 on an already verified live binding when the launcher has not sent this delivery. Set the marker and reporting fields to the active delivery ID and round. Respect in-place `/model` or `/permissions` changes in visible windows. Keep model, effort, and service-tier override flags out of queue calls.

```bash
codex queue --thread "<UUID>" --resume -C "<ABS_PROJECT_ROOT>" --message "<TASK_MESSAGE>"
```

Use the first command for writing tasks. Use the second for a queued `AUDIT`, or append `--turn-id "<TURN_ID>"` when the daemon launcher returned one. Transcript wait only reads the project-owned rollout. It tolerates lazy creation and waits for the matching turn to finish before extracting its final reply.

```bash
python -B "<SKILL_DIR>/scripts/wait_codex_done.py" --cwd "<ABS_PROJECT_ROOT>" --task-id "<TASK_ID>" --round <ROUND> --timeout 600 --interval 3
python -B "<SKILL_DIR>/scripts/wait_codex_done.py" --cwd "<ABS_PROJECT_ROOT>" --mode transcript --thread "<UUID>" --task-id "<TASK_ID>" --round <ROUND> --timeout 600 --interval 3 --json
```

`CODEX_DONE` with `REPORT_STATUS`/`report_status` is the completion signal. In flag mode, the waiter checks the report and flag against both identifiers before consuming the flag. `--keep-flag` preserves it after verification. `--task-id` is mandatory when waiting. `--round` defaults to `1`. `--path` and `--report` are flag-mode overrides resolved relative to `--cwd` unless absolute. `--clean` without a task ID targets the legacy default flag and report. A failed cleanup blocks dispatch. On timeouts or an unknown turn state, verify delivery before proceeding.

When there is no suitable session and the user explicitly approves standalone execution, use stdout and the same mode-aligned sandbox. Use `read-only` for `AUDIT` and `workspace-write` for authorized writing tasks. Keep model, effort, and service-tier override flags out of this fallback.

```bash
codex exec -s "<SANDBOX>" -C "<ABS_PROJECT_ROOT>" "<TASK_MESSAGE>"
```

## 5. Review, clarify, and apply the three-round limit

- For `BLOCKED_NEED_CLARIFICATION`, suspend the affected edits. Codex reports the blocker, evidence, and 2–3 viable options in the result channel for its mode. Gemini presents them to the user and waits for a decision. Update the contract and use a fresh delivery ID at the same acceptance round. A clarification by itself does not consume a review round.
- Independently verify reported evidence. Review the full footprint first, including staged, unstaged, and untracked files. Compare against the recorded baseline and account for prior dirty changes without moving or reverting them.

```bash
git --no-optional-locks status --short --untracked-files=all
git diff --cached "<BASELINE_COMMIT>" --
git diff --
git diff "<BASELINE_COMMIT>" --
git ls-files --others --exclude-standard
```

After examining all changed paths, a focused diff may omit these exact lockfile names. Still review lockfiles changed outside the work order.

```bash
git diff "<BASELINE_COMMIT>" -- . ":(exclude,glob)**/package-lock.json" ":(exclude,glob)**/pnpm-lock.yaml" ":(exclude,glob)**/yarn.lock" ":(exclude,glob)**/poetry.lock" ":(exclude,glob)**/Cargo.lock"
```

- For `AUDIT`, confirm the project has no audit-induced edits and verify the cited evidence. For `SURGICAL_FIX`, re-run the targeted regression and check the allowlist. For `FEATURE_IMPL`, verify the architecture and interface contract, inspect new files, and re-run the required build, typecheck, and tests. Check for weakened assertions, suppressed type errors, changes to dependencies or secrets, and UI copy outside the agreed scope.
- On `PASS`, report verified results and obtain approval before any commit or merge. On `FAIL` in rounds 1 or 2, increment the round once and dispatch precise file-and-line corrections through the same sequence in section 4. Pause on a scope or Git violation and escalate. If acceptance fails in round 3, halt dispatch and present the failed criteria and recovery options. Get a new user decision before any further work.

## CLI result contract

Check both the reported status and the exit code. Python examples accept `--json` for structured status. Help exits `0`. Argument parser errors exit `2` with usage text. `codex_daemon_rpc.py` is an imported transport and has no public command.

| Entry point | Exit | Status values |
|---|---:|---|
| `find_codex_thread.py` | 0 | `CACHED`, `AUTO_SELECTED`, `SELECTED`, `HISTORY_LISTED` |
| | 1 | `NOT_FOUND`, `SESSION_PROBE_FAILED` |
| | 2 | `MULTIPLE_MATCHES`, `INVALID_ARGUMENTS`, `INVALID_SESSION` |
| | 3 | `DAEMON_SESSION_OFFLINE`, `SANDBOX_SESSION_OFFLINE`, `HISTORICAL`, `START_PENDING` |
| `start_codex_window.py` | 0 | `MODELS_LISTED`, `DRY_RUN`, `STARTED_AND_BOUND`, `RESUMED_AND_BOUND`, `ALREADY_BOUND`, `STOPPED`, `ALREADY_EXITED`, `NO_ACTIVE_SESSION`, `SKIPPED_VISIBLE_WINDOW` |
| | 1 | `NO_MODELS_CACHE`, `DESKTOP_UNAVAILABLE`, `DAEMON_UNAVAILABLE`, `DAEMON_RPC_ERROR`, `SESSION_PROBE_FAILED`, `CONFIG_MISMATCH`, `BINDING_CONFLICT`, `DISPATCH_UNKNOWN`, `STOP_FAILED`, `LAUNCH_FAILED`, `OPERATION_FAILED` |
| | 2 | `INVALID_ARGUMENTS`, `INVALID_SESSION`, `MULTIPLE_MATCHES` |
| | 3 | `START_PENDING` |
| | 124 | `START_TIMEOUT`, `RESUME_TIMEOUT` |
| `wait_codex_done.py` | 0 | `CLEANED`, `CODEX_DONE` |
| | 1 | `CLEAN_FAILED`, `WAIT_FAILED`, `TURN_FAILED`, `INVALID_REPORT` |
| | 2 | `INVALID_ARGUMENTS`, `INVALID_SESSION` |
| | 124 | `CODEX_TIMEOUT` |
| | 130 | `CODEX_INTERRUPTED` |

The launcher's internal visible-readiness probe uses `BOOTSTRAP_READY`/`0`, `MULTIPLE_MATCHES`/`2`, and `BOOTSTRAP_PENDING`/`3`. Public discovery lists at most 5 candidates or 10 history entries by default. `--limit` changes the cap and the output reports the total and truncation. Use `--timeout` for the total operation budget. See each script's `--help` for optional store and display overrides.
