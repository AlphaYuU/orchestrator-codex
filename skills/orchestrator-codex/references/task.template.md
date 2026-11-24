# Backend Subagent Task Work Order

> Saved at `.agent-collab/task.md` (Git-ignored). Self-contained work order for Codex CLI.

## Metadata

- **Task ID**: `<e.g. TASK-001>`
- **Mode**: `<AUDIT | SURGICAL_FIX | FEATURE_IMPL>`
  - `AUDIT`: Pure read-only investigation. Edit ONLY `.agent-collab/report.md` + create `.agent-collab/done.flag`. Zero source edits!
  - `SURGICAL_FIX`: Minimal targeted fix strictly within `Allowed Paths` + regression test. Zero drive-by refactoring!
  - `FEATURE_IMPL`: Module/feature implementation on dedicated branch aligned with `docs/architecture.md` + `.agent-collab/contract.md` + full tests.
- **Round**: `1 / 3`
- **Git Branch**: `<exact branch name, e.g. feat/order-state-machine>`
- **Baseline Commit**: `<short SHA, e.g. a1b2c3d>`
- **Branch Switched**: `<No | Yes>`
- **Architecture Reference**: `docs/architecture.md` *(if present)*
- **Contract Reference**: `.agent-collab/contract.md`

> **[MANDATORY IF `Branch Switched: Yes`]**
> The repository branch has been switched by Gemini. **Before editing anything, you MUST re-read every target file directly from disk.** NEVER rely on code cached in earlier conversation turns.

## 1. Objective

`<Clear, bounded goal for this round.>`

## 2. Path Boundaries

### Allowed Paths (Edit / Create ONLY Here)
- `<For AUDIT mode: ONLY .agent-collab/report.md and .agent-collab/done.flag>`
- `<For SURGICAL_FIX / FEATURE_IMPL: explicit source/test paths>`
- `.agent-collab/report.md`
- `.agent-collab/done.flag`

### Forbidden Paths (Do NOT Touch)
- `<e.g. src/components/**, src/app/**, src/styles/**>` (Frontend UI, styling, and user-facing copy)
- `docs/architecture.md` (Maintained exclusively by Gemini)
- `package.json`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `poetry.lock`, `go.sum` *(unless explicitly allowed above)*
- `.env`, `.env.*`, secrets, CI/CD workflows

## 3. Requirements & Stop-on-Ambiguity Rule

1. Follow `.agent-collab/contract.md` and `docs/architecture.md` strictly.
2. Return **structured error codes only** — NEVER write user-facing UI messages.
3. **Stop-on-Ambiguity Rule (Hard Requirement)**: If you encounter requirement ambiguity, underlying library/architecture conflict, or a product-level tradeoff, **NEVER guess, silently change the contract, or ship a hack**. Stop high-risk edits immediately, write `Status: BLOCKED_NEED_CLARIFICATION` at the top of `.agent-collab/report.md` with the blocker, root cause (`file:line`), and **2–3 concrete viable options**, then create `.agent-collab/done.flag`.
4. *(If Round 2 or 3)* Fix these exact review findings:
   - `<path/to/file.ts:42> — <defect and expected fix>`

## 4. Verification Commands (One-Shot & Self-Exiting Only)

Run these finite commands before writing `.agent-collab/report.md` *(for `AUDIT` mode, read-only inspection commands only)*:

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

1. Write the **complete** `.agent-collab/report.md` FIRST, starting at the top with:
   - `Status: READY_FOR_REVIEW` *(or `Status: BLOCKED_NEED_CLARIFICATION`)*
   - `Task / Mode / Round`
   - `Summary` (for `AUDIT`: concrete `file:line` evidence chain)
   - `Files Changed` (`Modified` and `Created` lists; `None` for `AUDIT`)
   - `Verification` (exact commands + real exit codes / test counts)
   - `Blockers / Options` (mandatory 2–3 options if `BLOCKED_NEED_CLARIFICATION`)
2. **ONLY AFTER `.agent-collab/report.md` is fully written**, create the empty file `.agent-collab/done.flag` as your very last step.
