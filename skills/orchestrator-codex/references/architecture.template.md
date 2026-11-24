# Project Architecture Blueprint

> Saved at `docs/architecture.md` (committed to Git).
> **Maintained EXCLUSIVELY by Gemini (Orchestrator & Frontend Lead).**
> Every new Antigravity window MUST read this file first to prevent architectural drift and context amnesia. Codex CLI may read this file as reference but must NEVER alter architectural ownership or contracts unilaterally.

## 1. System Overview & Core Principles

- **Project Name**: `<project name>`
- **Primary Goal**: `<1-3 sentences describing the product and technical scope>`
- **Non-Negotiable Engineering Principles**:
  - Frontend UI, styling, interactions, and all user-facing display copy are owned by Gemini.
  - Backend APIs, data persistence, pure logic, and automated tests are executed by Codex CLI under `.agent-collab/` work orders.
  - Backend returns structured error codes only; UI copy lives strictly on the frontend.

## 2. Module & Directory Ownership Map

| Path / Module | Responsibility | Owner | Notes |
| :--- | :--- | :--- | :--- |
| `src/app/`, `src/components/`, `src/styles/` | UI views, design system, animations, i18n/copy | **Gemini** | Codex forbidden |
| `src/server/`, `src/api/` | HTTP/RPC handlers, auth, validation, orchestration | **Codex** | Via `task.md` allowlist |
| `src/domain/`, `src/lib/` | Headless pure logic, state machines, parsers | **Codex** | Must stay UI-free |
| `db/`, `prisma/`, `migrations/` | Schemas, queries, migrations | **Codex** | Requires migration review |
| `docs/architecture.md` | Long-term system blueprint & ADRs | **Gemini** | Git-tracked |
| `.agent-collab/` | Ephemeral task contracts, reports, session cache, flags | **Shared (Git-ignored)** | Never commit |

## 3. Data Flow & Runtime Boundaries

```text
[User] -> [Frontend UI / Copy (Gemini)]
            -> [Typed API / IPC Contract]
              -> [Backend Handlers & Pure Logic (Codex)]
                -> [Database / External Services]
```

- **State Ownership**: `<Where canonical state lives, caching strategy, auth/session model>`
- **Boundary Rules**: `<Serialization formats, idempotency keys, pagination conventions, rate limits>`

## 4. Core Interface & Structured Error Code Registry

### API / Module Conventions
- **Base Prefix / Versioning**: `<e.g. /api/v1>`
- **Success Envelope**: `{ "ok": true, "data": ... }`
- **Error Envelope**: `{ "ok": false, "error": { "code": "ERR_...", "params": {} } }`

### Global Error Codes
| Error Code | HTTP / Status | Machine `params` | Meaning | UI Copy Owner |
| :--- | :--- | :--- | :--- | :--- |
| `ERR_VALIDATION_FAILED` | `400` | `{ field, reason }` | Schema or invariant failure | Gemini |
| `ERR_UNAUTHORIZED` | `401` | `{}` | Missing or expired auth | Gemini |
| `ERR_NOT_FOUND` | `404` | `{ resource, id }` | Entity does not exist | Gemini |
| `ERR_CONFLICT` | `409` | `{ key }` | State or uniqueness conflict | Gemini |

## 5. Git Branching & Integration Rules

- **Main Branch**: `main` (always releasable; Gemini never lets Codex mutate Git state).
- **Read-Only Audits / Small Fixes**: Stay on current branch after isolating uncommitted frontend work.
- **Features / Multi-File Refactors**: Confirm branch name with user -> `feat/<slug>` or `fix/<slug>`.

## 6. Architecture Decision Log (ADR)

| Date | Decision | Rationale | Impacted Paths |
| :--- | :--- | :--- | :--- |
| `<YYYY-MM-DD>` | `<Short decision summary>` | `<Why chosen over alternatives>` | `<paths>` |
