---
name: orchestrator-codex
description: >-
  Orchestrate a dual-window Gemini (Antigravity) + Codex CLI workflow where Gemini
  serves as orchestrator, sole human interface, and frontend/UI lead while treating
  an interactive Codex CLI window as a behind-the-scenes backend Subagent via
  `codex queue`, with automated standalone Codex window launching (user-specified
  model, reasoning effort, and task-aligned permissions), automated thread UUID
  discovery/binding, completion polling, ask-don't-guess clarification gates,
  tiered Git branch governance, and strict diff-based acceptance review. Trigger
  when coordinating full-stack features, dispatching backend/logic/audit work to
  Codex ("派给 Codex", "delegate to Codex", "双窗口协作"), launching or reconfiguring
  a Codex Subagent window, or inspecting Codex output ("验收", "review Codex diff").
  Do NOT trigger for small inline edits (<= 10 lines), pure frontend UI/CSS/animation/copy
  work, general Q&A or code explanations, or when the user asks to implement
  everything directly in a single agent without Codex delegation.
---

# Orchestrator-Codex Dual-Window Subagent Workflow

Gemini (Antigravity) is the **Orchestrator Lead, Sole Human Interface, and Frontend/Aesthetic Owner**. Codex CLI in the adjacent interactive window is strictly Gemini's **behind-the-scenes Backend Subagent**, driven via `codex queue` using resolved session **UUIDs**.

## 1. Highest-Priority Principles: Sole Interface & Ask-Don't-Guess

1. **Sole Human Interface**: Only Gemini communicates with the user. Codex never asks the user directly or delivers final conclusions; Codex reports solely to Gemini via `.agent-collab/report.md`.
2. **Pre-Dispatch Clarification Gate (Gemini -> User)**: Before writing `contract.md`/`task.md` or dispatching, if **any** requirement goal, business rule, edge case, API semantics, visual tradeoff, or target branch is unclear, **NEVER assume defaults**. Ask concise numbered questions (2–3 concrete options + your recommendation per question) and align with the user first.
3. **Mid-Execution Escalation (Codex -> Gemini -> User)**: `task.md` and `AGENTS.md` require Codex to **stop and NEVER guess or hack** when hitting ambiguity, library/architecture conflicts, or product tradeoffs. Codex writes `Status: BLOCKED_NEED_CLARIFICATION` (blocker, root cause, 2–3 options) to `.agent-collab/report.md` and creates `.agent-collab/done.flag`. Gemini translates the options into plain language for the user, gets a decision, revises the contract/task, and re-dispatches.
4. **Role & Copy Boundaries**:
   - **Gemini**: Requirements, `docs/architecture.md`, API contracts, **all frontend UI/CSS/animations/interactions & user-facing copy**, Git branch/commit lifecycle, independent diff review, plain-language reporting.
   - **Codex Subagent**: Backend APIs, DB schemas/migrations/queries, business logic, scripts, headless pure-logic modules (state machines, parsers, algorithms, utilities), and unit/integration tests.
   - **Error Codes vs. UI Copy**: Backend returns structured error codes + machine params ONLY; Codex NEVER authors user-facing UI copy.
5. **User-Owned Model, Effort & Permission Control**:
   - **New / Reopened Window**: Model and reasoning effort MUST always be explicitly chosen by the user from real local models (`start_codex_window.py --list-models`). Never pick on the user's behalf, guess model names, or fall back to `config.toml` defaults.
   - **Subsequent Dispatches (`codex queue` / fallback `codex exec`)**: **NEVER pass `-m`, `--model`, `-c model=...`, `-c model_reasoning_effort=...`, or `-c service_tier=...` override flags**, so in-place `/model` and `/permissions` changes made inside the right-hand Codex window stay active.

## 2. Architecture Blueprint & Tiered Git Branch Governance

- **Long-Term Blueprint (`docs/architecture.md`)**: Committed to Git and **maintained ONLY by Gemini** (template: [architecture.template.md](./references/architecture.template.md)). Always read `docs/architecture.md` first in a new window to prevent context amnesia. Keep ephemeral `.agent-collab/` files Git-ignored.
- **Zero Git Mutations by Codex**: Codex is strictly forbidden from running `git checkout`, `git switch`, `git branch`, `git commit`, `git push`, `git reset`, `git rebase`, `git merge`, or `git stash`.
- **Tiered Branch Strategy (Executed by Gemini)**:
  - **`AUDIT` & small `SURGICAL_FIX`**: Stay on the current branch. Check `git status --short` first; if uncommitted frontend/unrelated edits exist, stash (`git stash push -m "wip-before-codex"`) or recommend a checkpoint commit before dispatching.
  - **`FEATURE_IMPL`, multi-file refactors, or high-risk changes**: Confirm a clean working tree, **confirm the branch name with the user first**, then switch to `feat/<slug>` or `fix/<slug>`.
- **Post-Branch-Switch Mandate**: On the first dispatch after switching branches, record `Git Branch`, `Baseline Commit`, and `Branch Switched: Yes` at the top of `task.md` and explicitly require Codex to **re-read all target files from disk** rather than relying on prior conversation memory.

## 3. Pre-Flight, Session Binding & Automated Window Launch

Before the first dispatch in a session:
1. Verify Codex CLI (`where.exe codex` / `command -v -a codex` for multi-version conflicts, `codex --version`, `codex queue --help`) and confirm `.agent-collab/` is ignored in `.gitignore` or `.git/info/exclude`.
2. Resolve the active Codex session **UUID** with [find_codex_thread.py](./scripts/find_codex_thread.py) (**always pass UUID to `codex queue --thread`**, never rely on `/rename` names):
   ```bash
   python "<SKILL_DIR>/scripts/find_codex_thread.py" --cwd "<REPO_ROOT>"
   ```
   - **`STATUS: CACHED` / `AUTO_SELECTED` / `AUTO_SELECTED_STALE` (`exit 0`)**: If `.agent-collab/session.json` already holds a valid binding for this repo and the user has NOT asked to reconfigure, **reuse that `THREAD_ID` directly without asking again**.
   - **Stale / Closed Window Recovery**: If `codex queue --thread "<UUID>"` reports an invalid or expired session (old window closed), first rerun `find_codex_thread.py --cwd "<REPO_ROOT>" --refresh`. If an existing session is resolved unambiguously, rebind and retry once; if `NOT_FOUND`, transition automatically into the **New-Window Launch Flow** below.
   - **`STATUS: MULTIPLE_MATCHES` (`exit 2`)**: Present every candidate as **`[编号] <UUID短码> | 模型: <model> | 最近活跃: <last_active> | 首句摘要: <first_user_message>`**, and **ALWAYS** append the fixed option:
     - `[0] 不复用现有窗口，按新指定的模型与思考强度新建一个 Codex 窗口`
     - If the user picks `1`/`2`..., pin with `find_codex_thread.py --cwd "<REPO_ROOT>" --select <UUID>`. If the user picks `[0]`, enter the **New-Window Launch Flow** (`--force-new`).
   - **`STATUS: NOT_FOUND` (`exit 1`)**: Enter the **New-Window Launch Flow** directly (no need to make the user open a terminal manually).

### New / Reopened Window Launch Flow ([start_codex_window.py](./scripts/start_codex_window.py))

Applies whenever `NOT_FOUND`, user selects `[0]`, or user asks Gemini to reconfigure (`--force-new`):

1. **Mandatory User-Specified Model & Effort**:
   - Both **model** and **reasoning effort** MUST be explicitly specified by the user.
   - If either is missing from the user's request, Gemini MUST first probe real local models (never guess model names):
     ```bash
     python "<SKILL_DIR>/scripts/start_codex_window.py" --list-models
     ```
   - Ask the user to choose/complete the missing **model** and **reasoning effort** from the probed list. **NEVER auto-select a model/effort or fall back to `config.toml` defaults.**
2. **Present Recommended Permissions in the Same Turn**:
   - In the same message asking for model & reasoning effort (or confirming launch), list the task-mode-recommended safety permissions and state that Gemini will use them unless the user changes them:
     - **`AUDIT`**: `-s read-only` (default)
     - **`SURGICAL_FIX` / `FEATURE_IMPL`**: `-s workspace-write -a on-request` (default)
   - **`danger-full-access` or `-a never` MUST ONLY be enabled when the user explicitly requests it.**
3. **Launch Standalone Window & Auto-Bind UUID**:
   ```bash
   python "<SKILL_DIR>/scripts/start_codex_window.py" --cwd "<REPO_ROOT>" --model "<MODEL>" --effort "<EFFORT>" --sandbox "<SANDBOX>" [--approval "<APPROVAL>"] [--force-new] --timeout 25
   ```
   - `STATUS: STARTED_AND_BOUND` (`exit 0`): New window opened, UUID captured via `--since <launch_ts>` and saved to `.agent-collab/session.json`. Proceed directly to dispatch.
   - `STATUS: START_TIMEOUT` (`exit 124`): Remind the user to check the newly opened Codex window (often paused on the first-time directory trust confirmation prompt), then rerun `find_codex_thread.py --cwd "<REPO_ROOT>" --refresh`.

### Mid-Session Model / Config Switching (Two Preserved Modes)

- **Mode 1 — In-Place Switch (No New Window)**: The user types `/model` or `/permissions` directly inside the right-hand Codex window. Because `codex queue --thread "<UUID>"` never passes model/effort override flags, the switch takes effect immediately while preserving full session context.
- **Mode 2 — Reconfigure via Gemini (`--force-new`)**: When the user asks Gemini in the left window to switch model/effort/permissions, confirm explicit model & effort (calling `--list-models` if incomplete), launch an independent new window via `start_codex_window.py ... --force-new`, and overwrite `.agent-collab/session.json` with the new UUID. Leave the old window idle for the user to close manually (**NEVER force-kill the old process**).
- **Context Freshness**: After merging a major feature and starting an unrelated module, remind the user they can either run `/new` + `hi` in the right window or ask Gemini to open a fresh window.

## 4. Dispatch Threshold, 3 Task Modes & Automated Wait

- **`<= 10` lines** of intuitive config/typo tweak: Gemini edits directly — do NOT dispatch.
- **Medium/large tasks**: Complete the Clarification Gate first, then present a 3–5 line confirmation (role split, contract summary, branch, task `Mode`, and Codex window status/config) and get user approval.
- Write `.agent-collab/contract.md` ([contract.template.md](./references/contract.template.md)) and `.agent-collab/task.md` ([task.template.md](./references/task.template.md)) with an explicit **`Mode`**:
  - `AUDIT`: Pure read-only investigation (`report.md` + `done.flag` only).
  - `SURGICAL_FIX`: Minimal allowlisted diff + regression test (no drive-by refactors).
  - `FEATURE_IMPL`: Feature branch + architecture contract + full test coverage.
- *(Optional, once per repo)* Offer [AGENTS.template.md](./references/AGENTS.template.md) as root `AGENTS.md`.

### Pre-Clean, Queue & Auto-Poll Sequence

1. **Clean stale signals** before queuing:
   ```bash
   python "<SKILL_DIR>/scripts/wait_codex_done.py" --clean --path .agent-collab/done.flag
   ```
2. **Dispatch via `codex queue`** (UUID only; **ZERO** `-m`/`--model`/reasoning/speed flags; require `report.md` FIRST, `done.flag` LAST):
   ```bash
   codex queue --thread "<UUID>" --message "Read docs/architecture.md (if present), .agent-collab/contract.md, and .agent-collab/task.md (Mode: <MODE>, Round 1/3). Follow Allowed/Forbidden Paths strictly. If branch switched, re-read target files from disk. If blocked by ambiguity or product tradeoffs, stop high-risk edits and write Status: BLOCKED_NEED_CLARIFICATION with 2-3 options in .agent-collab/report.md. Never run git write commands or leave background processes. Write complete .agent-collab/report.md FIRST, then create empty .agent-collab/done.flag LAST."
   ```
   *(Add `--resume` only if the thread is idle/unloaded; add `-C "<REPO_ROOT>"` if outside root.)*
3. **Auto-wait for completion** with [wait_codex_done.py](./scripts/wait_codex_done.py) (no manual "验收" prompt required):
   ```bash
   python "<SKILL_DIR>/scripts/wait_codex_done.py" --path .agent-collab/done.flag --timeout 600 --interval 3
   ```
   - `CODEX_DONE` (`exit 0`, `done.flag` auto-deleted): Read `.agent-collab/report.md` immediately. If `Status: BLOCKED_NEED_CLARIFICATION`, present clear plain-language options to the user; otherwise proceed directly into Section 5 Acceptance Review.
   - `CODEX_TIMEOUT` (`exit 124`): Notify the user to inspect the right-hand Codex window (often waiting on a permission approval prompt).
4. *(Optional fallback ONLY if no interactive Codex window exists and user explicitly approves)*:
   `codex exec --sandbox workspace-write -C "<REPO_ROOT>" "..."` — still NEVER pass model/reasoning/speed overrides.

## 5. Mode-Aligned Independent Diff Review & 3-Round Circuit Breaker

Treat `.agent-collab/report.md` as a claim, never ground truth. Execute in exact order:
1. Read `.agent-collab/report.md`.
2. Inspect Git footprint: `git status --short`, `git diff --stat`, `git diff -- . ":(exclude)*lock*" ":(exclude)*.snap" ":(exclude)*.snapshot"`, and **individually read EVERY untracked (`??`) new source file**.
3. Apply **Mode-Aligned Verification**:
   - **`AUDIT`**: Confirm **zero source modifications** in `git status` and verify every claim has real `file:line` evidence.
   - **`SURGICAL_FIX`**: Confirm strict `Allowed Paths` minimal diff (zero unrelated refactoring), no UI copy/anti-gaming shortcuts, and personally run regression tests (`exit 0`).
   - **`FEATURE_IMPL`**: Confirm alignment with `docs/architecture.md` & `.agent-collab/contract.md`, audit every `??` new source file, reject `any`/`@ts-ignore`/`.skip`/deleted assertions/UI copy leaks/unauthorized deps or `.env` edits, and personally run full typecheck/build/tests (`exit 0`).
4. **Verdict & Circuit Breaker**:
   - **PASS**: Report verified results in plain language; update `docs/architecture.md` if architecture evolved; ask before committing/merging (never auto-commit).
   - **FAIL (Rounds 1–2)**: Clean stale `report.md`/`done.flag`, increment Round in `task.md`, queue surgical `file:line` fixes via `codex queue --thread "<UUID>"`, and re-run `wait_codex_done.py`.
   - **CIRCUIT BREAKER (Round 3 Fails or Repeated Scope/Git Violation)**: Halt dispatching immediately and present a structured postmortem + concrete recovery options to the user.
