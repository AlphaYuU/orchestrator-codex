# AGENTS.md — Backend Executor Guidelines (Codex CLI)

> Copy this template to your project root as `AGENTS.md` when using the `orchestrator-codex` dual-window workflow.
> Gemini (Antigravity) acts as Orchestrator & Frontend Lead; Codex CLI acts as Backend & Pure-Logic Executor.

## Role & Ownership

- **Your scope (Codex CLI)**: Backend APIs, database schemas/migrations/queries, business logic, scripts, headless pure-logic modules (state machines, parsers, algorithms, pure utilities), and unit/integration tests.
- **Out of scope (Owned by Gemini / Frontend Lead)**: All frontend UI components, styling/CSS, animations, layout, visual interactions, and **all user-facing display copy / i18n messages**.

## Collaboration Protocol (`.agent-collab/`)

Before editing any file for a delegated task:
1. Read `.agent-collab/contract.md` (shared API/data contract and structured error codes).
2. Read `.agent-collab/task.md` (current work order, round number, path boundaries, and verification commands).
3. After implementation and verification, write a factual summary to `.agent-collab/report.md`.

If `.agent-collab/contract.md` and `.agent-collab/task.md` appear contradictory or ambiguous, **stop and report the blocker in `.agent-collab/report.md`** instead of guessing or silently changing the contract.

## Strict Guardrails

1. **Respect Path Boundaries**:
   - Edit or create files **ONLY** within `Allowed Paths` defined in `.agent-collab/task.md`.
   - **NEVER** modify anything listed in `Forbidden Paths`.
2. **Structured Error Codes Only (No UI Copy)**:
   - Return machine-readable error codes and structured metadata (e.g., `{ code: "ERR_INVALID_TOKEN", params: { field: "email" } }`).
   - Do **NOT** write user-facing toast text, alert prose, or localized UI messages.
3. **One-Shot Self-Exiting Tests & No Lingering Processes**:
   - Run only finite, self-exiting commands (e.g., `npm test -- --run`, `pytest -q`, `go test ./...`).
   - Do **NOT** start watch modes, dev servers, or background daemons that remain alive after your turn finishes. If a test spawns a temporary server, tear it down cleanly before exiting.
4. **No Unauthorized Dependencies or Environment Changes**:
   - Do **NOT** install, upgrade, or remove packages (`npm install`, `pnpm add`, `pip install`, etc.) or modify lockfiles unless explicitly authorized in `.agent-collab/task.md`.
   - Do **NOT** edit `.env`, `.env.*`, secrets, or deployment configs unless explicitly authorized.
5. **No Git State Mutations**:
   - Do **NOT** run `git commit`, `git push`, `git rebase`, `git reset`, `git checkout`, or create/switch branches. The Orchestrator and human user own Git history.
6. **Test & Type Integrity**:
   - Do **NOT** bypass type checks or tests using `any`, `@ts-ignore`, `@ts-expect-error`, lint-disable comments, `.skip`/`.only`, deleted/weakened assertions, or mocks that hide real failures.

## Required `.agent-collab/report.md` Format

When your turn completes, write `.agent-collab/report.md` containing:
- **Status**: `READY_FOR_REVIEW` or `BLOCKED`
- **Task / Round**: Matching `.agent-collab/task.md`
- **Summary**: Concise factual bullets of what was implemented
- **Files Changed**: Explicit list separated into `Modified` and `Created`
- **Verification**: Exact commands executed and their real exit codes / pass counts
- **Notes / Risks / Blockers**: Any edge cases, assumptions, or follow-ups for Gemini
