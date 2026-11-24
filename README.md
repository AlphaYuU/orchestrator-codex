# orchestrator-codex

让 **Antigravity (Gemini)** 与 **Codex CLI** 组队写代码的双窗口协作 Skill。  
A dual-window Agent Skill pairing **Antigravity (Gemini)** with **Codex CLI** for collaborative coding.

## 功能 / Features

你只需和 Antigravity 对话，它会自动把后端、逻辑和排查任务派给另一个窗口里的 Codex，完成后自动检查代码并汇报。模型和权限由你在各自窗口手动控制，需求不清时会先提问确认，绝不瞎猜或私自提交 Git。

Talk only to Antigravity; it automatically dispatches backend, logic, and audit tasks to Codex in the other window, then reviews diffs and reports back. You keep full manual control over models and approvals in both windows, with clarification before guessing and no autonomous Git commits.

## 流程 / Workflow

`提需求` → `对齐细节` → `自动派给 Codex 执行` → `自动检查代码与测试` → `确认后提交`。小改动（≤10 行）或纯前端样式由 Antigravity 直接完成。

`State your request` → `Align details` → `Auto-dispatch to Codex` → `Auto-review diff & tests` → `Commit on approval`. Small edits (≤10 lines) and pure UI tweaks are handled directly by Antigravity.

## 如何使用 / How to Use

环境要求 / Prerequisites: **Antigravity**, **Codex CLI** (`codex queue`), **Python 3.8+**, **Git**.

### 1. 安装 / Install

**macOS / Linux:**

```bash
git clone https://github.com/AlphaYuU/orchestrator-codex.git ~/orchestrator-codex
mkdir -p ~/.gemini/config/skills
ln -s ~/orchestrator-codex/skills/orchestrator-codex ~/.gemini/config/skills/orchestrator-codex
```

**Windows (PowerShell):**

```powershell
git clone https://github.com/AlphaYuU/orchestrator-codex.git D:\Project\Skill\orchestrator-codex
New-Item -ItemType Directory -Force "$HOME\.gemini\config" | Out-Null
# If skills.json already exists, merge the path entry into "entries"
$json = '{"entries":[{"path":"D:\\Project\\Skill\\orchestrator-codex\\skills"}]}'
[System.IO.File]::WriteAllText("$HOME\.gemini\config\skills.json", "$json`n", (New-Object System.Text.UTF8Encoding($false)))
```

> Windows 请通过 `skills.json` 注册（勿用 `mklink /J`），安装后可在 `/skills` 或 **Customizations → Installed** 确认。  
> On Windows, register via `skills.json` (avoid `mklink /J`). Verify in `/skills` or **Customizations → Installed**.

### 2. 日常使用 / Daily Usage

分别打开 Antigravity 和**同一项目目录**下的 Codex CLI（首次在 Codex 里发一句 `hi` 初始化），之后直接和 Antigravity 提需求即可；如果等待超时，检查 Codex 窗口是否弹出了权限确认。

Open Antigravity and Codex CLI in the **same repo** (send `hi` once in Codex to initialize), then simply prompt Antigravity. If waiting times out, check the Codex window for a permission prompt.
