# Collaboration Contract

> Saved at `.agent-collab/contract.md` (Git-ignored). Authored and maintained by Gemini (Orchestrator & Frontend Lead) **after** clearing all ambiguities with the user.

## 1. Feature & Alignment

- **Feature / Issue Name**: `<short title>`
- **Architecture Blueprint**: `docs/architecture.md` *(read first if present)*
- **Target Git Branch**: `<current branch or confirmed feat/<slug> / fix/<slug>>`
- **Problem Statement**: `<1-3 sentences describing the agreed goal>`
- **Out of Scope**: `<explicit non-goals to prevent scope creep>`

## 2. Clarified User Decisions (Ask-Don't-Guess Log)

> Record every product/edge-case decision confirmed with the user before dispatch so Codex never guesses.

1. `<Decision 1 — e.g. Duplicate submissions return ERR_CONFLICT (409) instead of silently upserting>`
2. `<Decision 2 — e.g. Pagination defaults to limit=20, max=100>`

## 3. Role & Ownership Split

| Area | Owner | Responsibilities |
| :--- | :--- | :--- |
| **Frontend / UI / Copy / Architecture** | Gemini (Antigravity) | UI components, styling, animations, all user-facing display copy & i18n, `docs/architecture.md`, Git branch/commit lifecycle |
| **Backend / Pure Logic / Tests** | Codex CLI (Subagent) | API handlers, DB schema/queries, business logic, scripts, pure utilities, unit & integration tests |

## 4. Interface & Data Contract

### A. API / Module Signatures

- **Endpoint / Entry Point**: `<e.g. POST /api/v1/orders or export function parseX(...)>`
- **Request / Input Schema**:
  ```json
  {
    "exampleField": "string"
  }
  ```
- **Success Response / Output Schema**:
  ```json
  {
    "ok": true,
    "data": {}
  }
  ```

### B. Data Model / Persistence Changes

- `<Tables, collections, migrations, indexes, or "None">`

### C. Invariants & Edge Cases

- `<Idempotency, concurrency, ordering, null/empty handling, limits>`

## 5. Structured Error Codes (No User-Facing Copy in Backend)

> **Rule**: Codex returns `code` and optional machine-readable `params` only. Gemini maps codes to all human-readable UI text on the frontend.

| Error Code | HTTP / Status | Machine `params` | Trigger Condition | UI Copy Owner |
| :--- | :--- | :--- | :--- | :--- |
| `ERR_VALIDATION_FAILED` | `400` | `{ field, reason }` | Input fails schema validation | Gemini |
| `ERR_NOT_FOUND` | `404` | `{ resource, id }` | Target entity does not exist | Gemini |
| `ERR_CONFLICT` | `409` | `{ key }` | Duplicate or state conflict | Gemini |

## 6. Acceptance Criteria

- [ ] Implementation aligns with `docs/architecture.md` and every schema/error code above.
- [ ] All changes stay strictly within `Allowed Paths` for the declared task `Mode`.
- [ ] Tests cover happy paths, clarified edge cases, and regressions without weakened assertions or type escapes.
- [ ] All one-shot verification commands exit with code `0` when independently re-run by Gemini.
