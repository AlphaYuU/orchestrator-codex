---
name: orchestrator-codex
description: >-
  Orchestrate a dual-window Gemini (Antigravity) + Codex CLI workflow where Gemini
  serves as orchestrator, sole human interface, and frontend/UI lead while delegating
  bounded backend, database, scripting, pure-logic, and unit-test work to an
  interactive Codex CLI session via `codex queue`, followed by strict diff-based
  independent acceptance review. Trigger when coordinating full-stack features,
  dispatching backend/logic tasks to Codex ("派给 Codex", "delegate to Codex",
  "双窗口协作"), or inspecting Codex changes ("验收", "review Codex diff").
  Do NOT trigger for small inline edits (<= 10 lines), pure frontend UI/CSS/animation/copy
  work, general Q&A or code explanations, or when the user asks to implement
  everything directly in a single agent without Codex delegation.
---

# Orchestrator-Codex Dual-Window Workflow

Turn Gemini (Antigravity) into the **Orchestrator Lead, Sole Human Interface, and Frontend/Aesthetic Owner**, delegating bounded backend and pure-logic execution to the user's interactive **Codex CLI** session via `codex queue`.

## 1. Role Split & Non-Negotiable Boundaries

| Role | Owner | Scope |
| :--- | :--- | :--- |
| **Orchestrator + Frontend Lead** | **Gemini (Antigravity)** | Requirements alignment, architecture & API contracts, **all frontend UI/CSS/layout/animations/visual interaction & user-facing copy**, independent diff review, plain-language human reporting, and Git landing (only on user instruction). |
| **Backend Executor** | **Codex CLI** | Backend APIs, database schemas/migrations/queries, business logic, scripts/CLIs, headless pure-logic modules (state machines, parsers, algorithms, pure utilities), and unit/integration tests. |

### Hard Rules

1. **Model Decoupling (User Owns Both Windows)**: The user opens Antigravity and Codex CLI side by side and picks models manually. **NEVER pass `-m`, `--model`, or `-c model=...`** (or unrequested `-p/--profile`) to `codex queue` or `codex exec`. Queued turns inherit the interactive thread's model.
2. **Gray-Area Boundary (Error Codes vs. UI Copy)**: Backend code returns **structured error codes** and machine-readable metadata only (e.g., `ERR_RATE_LIMITED`, `{ retryAfterSec }`). **NEVER allow Codex to author user-facing UI toast, modal, form, or error copy** — Gemini owns all display copy and i18n mapping on the frontend.
3. **Orchestrator Owns Git**: Codex must NEVER run `git commit`, `git push`, `git reset`, `git rebase`, or switch branches. Gemini never commits automatically without user approval.

## 2. Pre-Flight Checks (Before First Dispatch)

### A. Verify Codex CLI Environment

Run a quick check before queueing the first task in a session:

- **Multi-version conflict check**:
  - Windows: `where.exe codex`
  - macOS / Linux: `command -v -a codex`
  - If multiple `codex` binaries appear in `PATH`, warn the user which binary will be invoked before proceeding.
- **Version & queue support**:
  - Run `codex --version` and `codex queue --help`.
  - Confirm `--thread` and `--message` are available. If `codex queue` is unsupported, stop and inform the user (or ask whether to use the `codex exec` fallback in Section 4).

### B. Git Baseline Isolation

1. Run `git status --short`.
2. If uncommitted frontend or unrelated changes exist in the working tree, **do NOT dispatch yet** — recommend either:
   - Creating a checkpoint commit first, or
   - Stashing them (`git stash push -m "wip-before-codex-dispatch"`) with user consent.
   Isolating the baseline ensures `git diff` reflects Codex's changes cleanly.
3. Verify `.agent-collab/` is ignored (in `.gitignore` or `.git/info/exclude`) so collaboration files are never accidentally staged.

## 3. Dispatch Threshold & User Confirmation Gate

- **<= 10 lines** of straightforward config, typo, or trivial adjustment: **Edit directly** as Orchestrator. Do not incur delegation overhead.
- **Medium / large backend or pure-logic tasks**: Before writing files or queuing to Codex, send the user a concise **3–5 line confirmation**:
  1. Task split (what Gemini owns vs. what Codex will execute).
  2. Contract highlights (key APIs / schemas / error codes).
  3. Target Codex thread name or UUID (ask if not yet provided).
  Wait for user approval before dispatching.

## 4. File Contract & Task Dispatch

Prepare self-contained collaboration files under `.agent-collab/` (Codex does not see Antigravity's chat history):

1. **`.agent-collab/contract.md`** — Shared interface, schema, structured error code table, and acceptance criteria. Use [contract.template.md](./references/contract.template.md).
2. **`.agent-collab/task.md`** — Bounded work order for the current task/round. Use [task.template.md](./references/task.template.md). It **MUST** explicitly specify:
   - `Allowed Paths` (strict allowlist) and `Forbidden Paths` (frontend UI dirs, lockfiles, `.env*`, unrelated modules).
   - **One-shot, self-exiting verification commands** (e.g., `npm test -- --run`, `pytest -q`; NEVER watch modes or dev servers).
   - **Hard prohibitions**: no lingering background processes/daemons, no unauthorized package installation or lockfile edits, no `.env`/secret changes, no user-facing UI copy, and no Git commits/pushes/branch changes.
   - Expected `.agent-collab/report.md` output format.
3. *(Optional, once per repo)* Suggest placing [AGENTS.template.md](./references/AGENTS.template.md) at the repository root as `AGENTS.md` to reinforce Codex's persistent backend rules.

### Primary Dispatch (`codex queue`)

Keep the queued message short and point directly to the file contract (no `-m` / `--model` / `-c model=...`):

```bash
codex queue --thread "<THREAD_NAME_OR_UUID>" --message "Read .agent-collab/contract.md and .agent-collab/task.md (Round 1/3). Implement strictly within Allowed Paths, return structured error codes only (no UI copy), run the self-exiting verification commands, write .agent-collab/report.md, and do NOT run git commit or leave background processes running."
```

- Pass `--resume` only if the user's Codex thread has gone idle/unloaded.
- Pass `-C "<REPO_ROOT>"` if invoking from outside the repository root.
- Confirm to the user that the task is queued in `<THREAD_NAME_OR_UUID>` and ask them to reply **"验收"** (or **"review"**) once Codex completes.

### Optional Fallback (`codex exec`, Only Without Interactive Window)

Only if the user has **no** interactive Codex CLI window open and explicitly agrees to headless execution:

```bash
codex exec --sandbox workspace-write -C "<REPO_ROOT>" "Read .agent-collab/contract.md and .agent-collab/task.md. Follow all path, testing, and Git prohibitions, write .agent-collab/report.md, and exit cleanly."
```

Still **never** pass `-m`, `--model`, or `-c model=...` (relies on `~/.codex/config.toml`).

## 5. Independent Diff-Based Acceptance Review

When the user signals **"验收"** / **"review"**, treat `.agent-collab/report.md` as a claim — **never** ground truth. Execute these steps in order:

1. **Read the Report**: Open `.agent-collab/report.md` to see claimed changes, commands run, and caveats.
2. **Inspect the Full Git Footprint**:
   - `git status --short`
   - `git diff --stat`
   - Diff excluding noisy lockfiles and snapshots:
     - `git diff -- . ":(exclude)*lock*" ":(exclude)*.snap" ":(exclude)*.snapshot"`
   - **Read EVERY untracked (`??`) new source file individually** (`git diff` does not display untracked files; never skip them).
3. **Audit Guardrails & Code Integrity**:
   - **Path & Scope**: Zero modifications outside `Allowed Paths` or inside `Forbidden Paths`. No unauthorized dependency/lockfile changes, no `.env` edits, no leftover background processes.
   - **Contract & Copy Boundary**: Interfaces, payloads, and structured error codes match `.agent-collab/contract.md`; zero user-facing UI copy introduced in backend code.
   - **Anti-Gaming / Test Quality**: Reject shortcuts such as unexplained `any`, `@ts-ignore`/`@ts-expect-error`, lint disables, `.skip`/`.only`, deleted or weakened assertions, or mocking away the unit under test.
4. **Verify Independently**: Run the project's build, typecheck, and one-shot test commands **yourself** and confirm exit code `0`.

## 6. Verdict, Targeted Rejection & 3-Round Circuit Breaker

- **PASS**:
  - Report verified outcomes, touched files, and real test results to the user in clear, concise plain language.
  - Propose the next step (frontend UI/copy integration by Gemini, or Git commit if the user approves).
- **FAIL (Rounds 1–2)**:
  - Update `.agent-collab/task.md` round indicator (`Round 2/3` or `Round 3/3`) if requirements need clarification.
  - Queue a surgical rejection back via `codex queue --thread "<THREAD_NAME_OR_UUID>" --message "..."` with:
    1. Exact `file:line` references for every defect or boundary violation.
    2. Concrete expected behavior referencing `.agent-collab/contract.md`.
    3. Exact self-exiting test command(s) to re-run and update `.agent-collab/report.md`.
- **CIRCUIT BREAKER (Max 3 Rounds)**:
  - If Round 3 still fails — or Codex repeatedly violates `Forbidden Paths` / commits without permission — **STOP dispatching immediately**.
  - Present a structured postmortem to the user (what passed, persistent root cause with `file:line`, and concrete options: Gemini takes over, revert & re-scope, or adjust contract).
