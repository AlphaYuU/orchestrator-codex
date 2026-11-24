# Collaboration Contract

> Saved at `.agent-collab/contract.md`. Authored and maintained by Gemini (Orchestrator & Frontend Lead) after aligning with the user.

## 1. Feature & Goal

- **Feature Name**: `<short feature title>`
- **Problem Statement**: `<1-3 sentences describing the user goal>`
- **Out of Scope**: `<explicit non-goals to prevent scope creep>`

## 2. Role & Ownership Split

| Area | Owner | Responsibilities |
| :--- | :--- | :--- |
| **Frontend / UI / Copy** | Gemini (Antigravity) | UI components, styles, animations, user-facing labels/toasts/error copy, client integration |
| **Backend / Pure Logic / Tests** | Codex CLI | API handlers, DB schema/queries, domain logic, pure utilities, unit & integration tests |

## 3. Interface & Data Contract

### A. API / Function Signatures

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

- `<Idempotency, concurrency, pagination, null/empty handling, limits>`

## 4. Structured Error Codes (No User-Facing Copy in Backend)

> **Rule**: Codex returns `code` and optional machine-readable `params` only. Gemini owns all human-readable UI text and i18n mapping on the frontend.

| Error Code | HTTP / Status | Machine `params` | Trigger Condition | UI Copy Owner |
| :--- | :--- | :--- | :--- | :--- |
| `ERR_VALIDATION_FAILED` | `400` | `{ field, reason }` | Input fails schema validation | Gemini |
| `ERR_NOT_FOUND` | `404` | `{ resource, id }` | Target entity does not exist | Gemini |
| `ERR_CONFLICT` | `409` | `{ key }` | Duplicate or state conflict | Gemini |

## 5. Acceptance Criteria

- [ ] All endpoints / modules match the schemas and error codes above.
- [ ] All changes stay strictly within `Allowed Paths` in `.agent-collab/task.md`.
- [ ] Unit and integration tests cover happy paths and key edge cases without weakened assertions or type escapes.
- [ ] All one-shot verification commands exit with code `0` when independently re-run by Gemini.
