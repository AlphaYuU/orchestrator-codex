# Backend Task Work Order

> Saved at `.agent-collab/task.md`. Self-contained brief for Codex CLI.

## Metadata

- **Task ID**: `<e.g. TASK-001>`
- **Round**: `1 / 3`
- **Contract Reference**: `.agent-collab/contract.md`

## 1. Objective

`<Clear, bounded description of what to implement or fix in this round.>`

## 2. Path Boundaries

### Allowed Paths (Edit / Create ONLY Here)
- `<e.g. src/server/...>`
- `<e.g. src/lib/parser/...>`
- `<e.g. tests/unit/...>`
- `.agent-collab/report.md`

### Forbidden Paths (Do NOT Touch)
- `<e.g. src/components/**, src/app/**, src/styles/**>` (Frontend UI, styling, and user-facing copy)
- `package.json`, `package-lock.json`, `pnpm-lock.yaml`, `yarn.lock`, `poetry.lock`, `go.sum` *(unless explicitly allowed above)*
- `.env`, `.env.*`, secrets, CI/CD workflows

## 3. Implementation Requirements

1. Follow `.agent-collab/contract.md` strictly for types, signatures, and behavior.
2. Return **structured error codes only** — do NOT author user-facing UI messages.
3. Cover happy paths and edge cases with focused unit/integration tests.
4. *(If Round 2 or 3)* Fix these specific review findings:
   - `<path/to/file.ts:42> — <defect and expected fix>`

## 4. Verification Commands (One-Shot & Self-Exiting Only)

Run these exact commands before writing `.agent-collab/report.md`:

```bash
# Replace with project-specific finite commands (NEVER watch mode or dev servers)
<e.g. npm run typecheck>
<e.g. npm test -- --run src/server/>
```

## 5. Hard Prohibitions

- **NO lingering processes**: Do not leave dev servers, watchers, or background daemons running.
- **NO unauthorized packages**: Do not install/remove dependencies or touch lockfiles.
- **NO `.env` or secret edits**: Never read/write production secrets or modify `.env*`.
- **NO Git mutations**: Never run `git commit`, `git push`, `git reset`, `git rebase`, or switch branches.
- **NO test/type shortcuts**: Never use `any`, `@ts-ignore`, `@ts-expect-error`, `.skip`, or delete assertions to force green tests.

## 6. Expected Output (`.agent-collab/report.md`)

Write `.agent-collab/report.md` with:
1. **Status**: `READY_FOR_REVIEW` or `BLOCKED`
2. **Summary**: Brief factual summary of changes
3. **Files Changed**: Separate lists of `Modified` and `Created` paths
4. **Verification Results**: Exact commands run, exit codes, and test counts
5. **Open Risks / Blockers**: Anything Gemini should verify during diff review
