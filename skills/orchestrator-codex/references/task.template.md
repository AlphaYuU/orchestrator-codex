# Backend Subagent Task Work Order

> Saved at `.agent-collab/task.md` (Git-ignored). Self-contained work order for Codex CLI.

## Metadata

- **Task ID**: `<unique delivery ID, e.g. TASK-001-a1>`
- **Mode**: `<AUDIT | SURGICAL_FIX | FEATURE_IMPL>`
  - `AUDIT`: Read-only investigation with a structured final reply collected from the transcript.
  - `SURGICAL_FIX`: Minimal targeted fix strictly within `Allowed Paths` + regression test. Zero drive-by refactoring!
  - `FEATURE_IMPL`: Module/feature implementation on dedicated branch aligned with `docs/architecture.md` + `.agent-collab/contract.md` + full tests.
- **Round**: `<1 | 2 | 3>`. A clarification follow-up uses a fresh delivery ID and the same round.
- **Git Branch**: `<exact branch name, e.g. feat/order-state-machine>`
- **Baseline Commit**: `<short SHA, e.g. a1b2c3d>`
- **Branch Switched**: `<No | Yes>`
- **Architecture Reference**: `docs/architecture.md` (required for `FEATURE_IMPL`)
- **Contract Reference**: `.agent-collab/contract.md`

> **[MANDATORY IF `Branch Switched: Yes`]**
> The repository branch has been switched by Gemini. **Before editing anything, you MUST re-read every target file directly from disk.** NEVER rely on code cached in earlier conversation turns.

## 1. Objective

`<Clear, bounded goal for this round.>`

## 2. Path Boundaries

### Allowed Paths (Edit / Create ONLY Here)
- `<For AUDIT mode: no edit or create paths>`
- `<For SURGICAL_FIX / FEATURE_IMPL: explicit source/test paths>`
- `.agent-collab/results/<TASK_ID>/<ROUND>/report.md` (writing modes)
- `.agent-collab/results/<TASK_ID>/<ROUND>/done.flag` (writing modes)

### Forbidden Paths (Do NOT Touch)
- `<e.g. src/components/**, src/app/**, src/styles/**>` (Frontend UI, styling, and user-facing copy)
- `docs/architecture.md` (Maintained exclusively by Gemini)
- `package.json`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `poetry.lock`, `go.sum` *(unless explicitly allowed above)*
- `.env`, `.env.*`, secrets, CI/CD workflows

## 3. Requirements & Stop-on-Ambiguity Rule

1. Follow `.agent-collab/contract.md` and `docs/architecture.md` strictly.
2. Return **structured error codes only** — NEVER write user-facing UI messages.
3. **Stop on ambiguity**. Suspend affected work when business rules, architecture, the contract, or a product choice needs clarification. Return `Status: BLOCKED_NEED_CLARIFICATION` through the task mode's result channel. Include the blocker, `file:line` evidence, and 2–3 viable options. List completed checks and those blocked by the decision.
4. *(If Round 2 or 3)* Fix these exact review findings:
   - `<path/to/file.ts:42> — <defect and expected fix>`

## 4. Verification Commands (One-Shot & Self-Exiting Only)

For work ready for review, run these finite commands before delivering the result. Use read-only inspection commands for `AUDIT`.

```bash
<e.g. npm run typecheck>
<e.g. npm test -- --run src/server/>
```

## 5. Hard Prohibitions

- **NO Git mutations**: NEVER run `git checkout`, `git switch`, `git branch`, `git commit`, `git push`, `git reset`, `git rebase`, `git merge`, or `git stash`.
- **NO lingering processes**: NEVER leave dev servers, watchers, or background daemons running.
- **NO unauthorized packages or `.env` edits**: Do not install/remove dependencies, modify lockfiles, or edit `.env*`/secrets.
- **NO test/type shortcuts**: Never use `any`, `@ts-ignore`, `@ts-expect-error`, `.skip`, or weaken/delete assertions.

## 6. Completion Signal Ordering (CRITICAL)

1. Begin the complete result with:
   - `Status: READY_FOR_REVIEW` or `Status: BLOCKED_NEED_CLARIFICATION`
   - `Task ID: <TASK_ID>`
   - `Round: <ROUND>`
   - `Summary` (for `AUDIT`: concrete `file:line` evidence chain)
   - `Files Changed` (`Modified` and `Created` lists; `None` for `AUDIT`)
   - `Verification` (exact commands + real exit codes / test counts)
   - `Blockers / Options` (mandatory 2–3 options if `BLOCKED_NEED_CLARIFICATION`)
2. For `AUDIT`, deliver it as the final reply and let Gemini read it from the transcript.
3. For writing modes, finish `report.md` in the delivery directory, then atomically publish its sibling `done.flag` containing `{"task_id":"TASK-001-a1","round":1}` with the current ID and round.
