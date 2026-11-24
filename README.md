# orchestrator-codex

> **中文简介**：一套面向 **Antigravity (Gemini)** 的自定义 Agent Skill，将 Gemini 定位为「编排总指挥 + 唯一对人窗口 + 前端与视觉审美主力」，通过 `codex queue` 将后端 API、数据库、脚本、纯逻辑与单元测试派单给另一个交互式窗口中的 **Codex CLI**，并建立基于文件契约（`.agent-collab/`）与独立 Diff 审查的闭环验收机制。双端模型完全由用户手动选择，严禁越权传参覆盖。
>
> **English Overview**: A custom Agent Skill for **Antigravity (Gemini)** that turns Gemini into an **Orchestrator Lead, Sole Human Interface, and Frontend/Visual Owner**, delegating bounded backend APIs, database work, scripts, pure-logic modules, and unit tests to an interactive **Codex CLI** session via `codex queue`. Includes file-based contracts (`.agent-collab/`), strict scope guardrails, independent diff-first verification, and a 3-round rejection circuit breaker—while keeping model selection 100% under manual human control on both sides.

---

## Core Design / 核心设计

- **Clear Role Split / 角色边界清晰**
  - **Gemini (Antigravity)**: Requirements alignment, architecture & contracts, **all frontend UI / CSS / animations / visual interactions & user-facing copy**, independent diff-based acceptance review, and human-friendly reporting.
  - **Codex CLI**: Backend APIs, DB schemas/migrations/queries, business logic, scripts, headless pure-logic modules (state machines, parsers, algorithms, pure utilities), and unit/integration tests.
  - **Gray-Area Guardrail / 灰区边界**: Backend returns **structured error codes** only; Codex never writes user-facing UI copy.
- **Strict Model Decoupling / 模型解耦**
  - Open Antigravity and Codex CLI side by side and choose models manually.
  - `codex queue --thread "<name-or-uuid>" --message "..."` **never** passes `-m`, `--model`, or `-c model=...`.
  - Optional `codex exec --sandbox workspace-write` fallback only when no interactive Codex window is open (still never overriding the model).
- **Engineering Guardrails & Verification Loop / 工程护栏与验收闭环**
  - **Pre-flight CLI check**: Verifies `codex --version`, `codex queue --help`, and checks for multi-version binary conflicts in `PATH`.
  - **Dispatch threshold**: Small edits (`<= 10` lines) are handled directly by Gemini; medium/large tasks confirm a 3–5 line split & contract summary with the user first.
  - **Git baseline isolation**: Checks `git status` before dispatching and advises stashing or checkpointing uncommitted frontend changes.
  - **File contract (`.agent-collab/`)**: `contract.md`, `task.md` (`Allowed Paths`, `Forbidden Paths`, one-shot self-exiting test commands, no lingering processes / unauthorized installs / `.env` edits / Git commits), and `report.md`.
  - **Independent diff-first review**: Reads `report.md`, inspects `git status --short`, `git diff --stat`, lock/snapshot-excluded `git diff`, **individually reads every untracked (`??`) source file**, audits for `any` / skipped or deleted assertions, and personally re-runs tests.
  - **3-round circuit breaker**: Surgical `file:line` rejections via `codex queue`, tripping a circuit breaker after 3 failed rounds.

---

## Repository Structure / 仓库结构

```text
orchestrator-codex/
├── README.md
├── .gitignore
└── skills/
    └── orchestrator-codex/
        ├── SKILL.md
        └── references/
            ├── AGENTS.template.md
            ├── contract.template.md
            └── task.template.md
```

---

## Prerequisites / 前置条件

1. **Antigravity** (global skills directory: `~/.gemini/config/skills/`).
2. **Codex CLI** with `codex queue` support (`codex queue --help`).
3. **Git** initialized in your working repository.

---

## Installation / 本地全局安装（软链接同步）

Clone this repository locally, then mount `skills/orchestrator-codex` into Antigravity's global skills folder `~/.gemini/config/skills/orchestrator-codex`. Using a symlink / directory junction ensures any local edits take effect immediately.

### macOS / Linux

```bash
git clone https://github.com/AlphaYuU/orchestrator-codex.git ~/orchestrator-codex
mkdir -p ~/.gemini/config/skills
ln -s ~/orchestrator-codex/skills/orchestrator-codex ~/.gemini/config/skills/orchestrator-codex
```

Verify / 验证：

```bash
ls -la ~/.gemini/config/skills/orchestrator-codex/SKILL.md
```

### Windows (PowerShell — Directory Junction, No Admin Required)

```powershell
git clone https://github.com/AlphaYuU/orchestrator-codex.git D:\Project\Skill\orchestrator-codex
New-Item -ItemType Directory -Force "$HOME\.gemini\config\skills" | Out-Null
New-Item -ItemType Junction -Path "$HOME\.gemini\config\skills\orchestrator-codex" -Target "D:\Project\Skill\orchestrator-codex\skills\orchestrator-codex"
```

Verify / 验证：

```powershell
Get-Item "$HOME\.gemini\config\skills\orchestrator-codex" | Select-Object FullName, LinkType, Target
Test-Path "$HOME\.gemini\config\skills\orchestrator-codex\SKILL.md"
```

### Optional: Add `AGENTS.md` to Your Target Project / 可选：项目根目录规范

Copy `skills/orchestrator-codex/references/AGENTS.template.md` to `<your-project>/AGENTS.md` and add `.agent-collab/` to `<your-project>/.gitignore` so Codex automatically honors backend boundaries in every turn.

---

## Daily Dual-Window Workflow / 双窗口日常使用步骤

1. **Open two windows side by side / 左右打开双窗口**
   - Left window: **Antigravity (Gemini)** — manually pick your preferred orchestrator/frontend model.
   - Right window: **Codex CLI** in the same project root — manually pick your preferred backend model, and optionally run `/rename backend-worker` (or copy the session UUID).
2. **Align requirements in Antigravity / 在 Antigravity 对齐需求**
   - Tell Gemini what you want to build and provide the Codex thread name/UUID (e.g., *"把后端订单状态机派给 Codex 线程 `backend-worker`，前端界面由你负责"*).
   - For small edits (`<= 10` lines), Gemini applies them directly. For medium/large work, Gemini checks `codex queue` & `git status`, then presents a **3–5 line** split & contract confirmation.
3. **Approve & dispatch / 确认并派单**
   - After your approval, Gemini writes `.agent-collab/contract.md` and `.agent-collab/task.md`, then queues the task via `codex queue --thread "<thread>" --message "..."` (without overriding the model).
   - You can watch Codex execute live in the right window while Gemini works on frontend UI/copy (if baseline is clean) or waits.
4. **Trigger independent review / 通知验收**
   - Once Codex finishes and writes `.agent-collab/report.md`, tell Antigravity **"验收"** (or **"review"**).
   - Gemini audits `report.md`, `git status`, `git diff`, every `??` untracked file, checks for boundary violations or test shortcuts, and runs the tests itself.
5. **Accept, iterate, or circuit-break / 通过、打回或熔断**
   - **Pass**: Gemini summarizes verified results in plain language and asks before committing.
   - **Fail**: Gemini sends exact `file:line` fix instructions back via `codex queue` (up to **3 rounds**, after which a circuit breaker hands control back to you with a full postmortem).

---

## Inspiration & References / 参考与致谢

- Inspired by [`amElnagdy/delegate-skills`](https://github.com/amElnagdy/delegate-skills) (diff-first review, pre-flight CLI checks, orchestrator-owned Git commits).
- Built for the **Agent Skills** open format and **Codex CLI `codex queue`** cross-session supervision workflow.
