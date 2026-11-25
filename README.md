# orchestrator-codex

让 **Antigravity (Gemini)** 担任编排总指挥与前端负责人，自动把后端、逻辑与排查任务派给右侧 **Codex CLI** Subagent 的双窗口协作 Skill。支持由 Gemini 自动拉起独立 Codex 窗口并按你的选择配置模型、思考强度与权限，自动绑定会话 UUID、遇疑请示，并在完工后自动完成 Diff 验收。

A dual-window Agent Skill where **Antigravity (Gemini)** serves as orchestrator lead and frontend owner while delegating backend, logic, and audit tasks to a right-hand **Codex CLI** Subagent. Gemini can automatically launch a standalone Codex window with your chosen model, reasoning effort, and permissions, bind its session UUID, escalate ambiguities to you, and run automated Diff review upon completion.

## 使用方式 / How to Use

1. **在项目打开 Antigravity (Gemini) 提需求**：无需手动开终端；若你已在同项目手动打开过 Codex CLI 并发送过 `hi`，Gemini 也会直接识别并复用。
2. **对齐需求、分支与配置**：需要新开 Codex 窗口时，按提示补充模型与思考强度（安全权限按任务模式自动推荐），Gemini 会弹出独立新窗口并自动绑定 UUID；中途既可在右侧窗口直接输入 `/model` 原地切换，也可让 Gemini 重开新窗重配。
3. **自动派单与 Diff 验收**：Gemini 派单后自动轮询完工（若 Codex 遇歧义挂起，Gemini 会转述选项向你请示），完成后自动核查 `git diff`、新增文件与测试并汇报，不通过会自动打回修改（最多 3 轮）。

1. **Prompt Antigravity (Gemini) in your repo**: No need to open a terminal manually — and if you already have a Codex CLI window open in the same repo (initialized with `hi`), Gemini will detect and reuse it directly.
2. **Align scope, branch, and window config**: When a new Codex window is needed, specify your model and reasoning effort from the real local list (safety permissions are recommended automatically by task mode). Gemini opens a standalone window and binds its UUID; mid-session you can switch in place with `/model` in the right window or ask Gemini to open a reconfigured window.
3. **Auto-dispatch and Diff acceptance review**: Gemini polls until completion (escalating to you if Codex blocks on ambiguity), then independently verifies `git diff`, new untracked files, and tests before reporting back — automatically requesting fixes on failure (up to 3 rounds).

## 安装 / Installation

环境要求 / Prerequisites: **Antigravity**, **Codex CLI** (`codex queue`), **Python 3.8+**, **Git**.

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

> Windows 请使用 UTF-8 无 BOM 编码的 `~/.gemini/config/skills.json` 注册（跳过 NTFS Junction / 勿用 `mklink /J`），安装后可在 `/skills` 或 **Customizations → Installed** 确认。  
> On Windows, register via UTF-8 (no BOM) `~/.gemini/config/skills.json` (skip NTFS Junction / avoid `mklink /J`). Verify in `/skills` or **Customizations → Installed**.

**可选项目初始化 / Optional Project Init:**

- 将 `.agent-collab/` 加入目标仓库的 `.gitignore` 或 `.git/info/exclude`（避免临时协作文件进入 Git）。
- 可按需将 `skills/orchestrator-codex/references/AGENTS.template.md` 复制为项目根目录的 `AGENTS.md`，强化 Codex Subagent 护栏。
