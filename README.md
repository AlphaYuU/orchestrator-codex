# orchestrator-codex

> **中文简介**：一套面向 **Antigravity (Gemini)** 的自定义 Agent Skill，将 Gemini 定位为「编排总指挥 + 唯一对人接口 + 前端与视觉审美主力」，将右侧交互式窗口中的 **Codex CLI** 严格定位为幕后后端 **Subagent**。通过 `codex queue`（按 UUID 投递）+ 自动化发现/等待脚本 + 宁问勿猜澄清门禁 + 长期架构蓝图 + 分级 Git 分支治理 + 3 种显式任务模式（`AUDIT` / `SURGICAL_FIX` / `FEATURE_IMPL`），实现「双窗口自选模型、免手动抄 UUID、免手动喊验收」的闭环工程协作。
>
> **English Overview**: A custom Agent Skill for **Antigravity (Gemini)** turning Gemini into the **Orchestrator Lead, Sole Human Interface, and Frontend/Aesthetic Owner** while treating an adjacent interactive **Codex CLI** window strictly as a behind-the-scenes **Backend Subagent** driven by `codex queue`. Features Ask-Don't-Guess clarification gates (`BLOCKED_NEED_CLARIFICATION`), zero-dependency UUID auto-discovery (`find_codex_thread.py`) and completion polling (`wait_codex_done.py`), a long-term architecture blueprint (`docs/architecture.md`), tiered Git branch governance, 3 explicit task modes (`AUDIT` / `SURGICAL_FIX` / `FEATURE_IMPL`), diff-first independent review, and a 3-round circuit breaker — while keeping model, reasoning effort, speed mode, and permission approvals 100% under manual user control in both windows.

---

## Core Design / 核心设计

### 1. Sole Human Interface & Ask-Don't-Guess / 唯一对接 + 宁问勿猜（最高优先级）
- **Gemini as Sole Human Interface / 唯一对人窗口**：用户只与 Gemini 对接；Codex 作为纯幕后 Subagent，不直接向用户提问或输出最终结论。
- **Pre-Dispatch Clarification Gate / 派单前澄清门禁**：需求目标、业务规则、边界条件、接口语义、视觉取舍或分支归属有任何不明确之处，Gemini 严禁脑补默认方案，先用编号问题（附 2~3 个选项与推荐项）向用户快速对齐再派单。
- **Mid-Execution Escalation / 执行中遇疑挂起**：Codex 遇到歧义、库/架构冲突或产品级取舍时严禁猜写或硬凑 Hack，立即停止高风险改动，在 `.agent-collab/report.md` 标注 `Status: BLOCKED_NEED_CLARIFICATION`（附卡点、根因与 2~3 个可行方案）并创建 `done.flag`，由 Gemini 翻译为人话选项向用户请示拍板。
- **Strict Model & Control Decoupling / 双端自选控制**：`codex queue` **严禁传 `-m` / `--model` 或推理/速度覆盖参数**；模型、思考强度、快速模式与权限审批全部由用户在右窗自行控制。
- **Gray-Area Guardrail / 灰区边界**：后端仅返回结构化错误码与机器参数，所有面向用户的展示文案与 i18n 由 Gemini 在前端统一掌控。

### 2. Dual-Window Automation / 双窗口自动化脚本（零第三方依赖）
- **`scripts/find_codex_thread.py`（自动发现与重绑 UUID）**：
  - 优先读取 `.agent-collab/session.json` 缓存；无缓存或传入 `--refresh` 时按修改时间倒序扫描 `~/.codex/sessions/**/rollout-*.jsonl`，解析首行 `session_meta` 中的 `id` 与 `cwd`（兼容 Windows 盘符大小写、正反斜杠、`\\?\` 前缀及中文路径）。
  - 单近期会话自动缓存并**一律传 UUID** 派单（不再依赖 `/rename` 名称）；多候选时列精简编号让用户回复 `1`/`2` 后用 `--select <UUID>` 锁定。
  - 若 `codex queue` 报会话失效，自动 `--refresh` 重扫并重试一次；大功能合入后准备做无关新模块时，提醒用户在右窗 `/new` + 发 `hi` 保鲜上下文。
- **`scripts/wait_codex_done.py`（完工信号自动轮询）**：
  - 支持 `--path .agent-collab/done.flag --timeout 600 --interval 3` 以及派单前清理 `--clean`。
  - 硬性要求 Codex 先写完整 `report.md`，最后再创建空文件 `done.flag`。
  - 检测到 `done.flag` 后输出 `CODEX_DONE` 并自动删除 `done.flag` 无缝进入验收（或处理 `BLOCKED_NEED_CLARIFICATION`）；超时输出 `CODEX_TIMEOUT` 提醒查看右窗是否卡在权限审批框。

### 3. Architecture Blueprint, Tiered Git Branching & 3 Task Modes / 架构蓝图、分支治理与 3 种任务模式
- **Long-Term Blueprint vs Ephemeral Collab / 长期架构与临时协作分离**：
  - `docs/architecture.md`（Git 跟踪、**仅由 Gemini 维护**）：记录模块划分、数据流、核心接口与目录归属，防新窗口失忆。
  - `.agent-collab/`（Git 忽略）：存放临时 `contract.md`、`task.md`、`report.md`、`session.json`、`done.flag`。
- **Tiered Git Branch Governance / 分级 Git 分支治理**：
  - **严禁 Codex 执行任何 Git 变更命令**（`checkout`/`switch`/`branch`/`commit`/`push`/`reset`/`rebase`/`merge`/`stash`）。
  - 只读巡检与小修小补留在当前分支（先做好未提交前端改动的暂存隔离）；新功能开发、多文件后端重构或高风险变更，先由 Gemini 确认工作区干净并与用户确认分支名后再切到 `feat/<slug>` 或 `fix/<slug>`。
  - 切换分支后的首次派单强制在 `task.md` 顶部标注分支名、基线 Commit 与 `Branch Switched: Yes`，要求 Codex **必须从磁盘重新读取目标文件**，严禁依赖旧对话记忆。
- **3 Explicit Task Modes / 3 种显式任务模式（验收标准自动对齐）**：
  - `AUDIT`：纯只读排查（仅允许写 `report.md` + `done.flag`，验收核查工作区零源码变动 + `file:line` 证据链）。
  - `SURGICAL_FIX`：定点修复（严格白名单最小 Diff + 回归测试，严防顺手重构）。
  - `FEATURE_IMPL`：模块开发（新分支 + 架构契约，验收核对 `docs/architecture.md` 一致性 + 逐一审阅 `??` 新增源码 + 全量检查）。

---

## Repository Structure / 仓库结构

```text
orchestrator-codex/
├── README.md
├── .gitignore
└── skills/
    └── orchestrator-codex/
        ├── SKILL.md
        ├── scripts/
        │   ├── find_codex_thread.py
        │   └── wait_codex_done.py
        └── references/
            ├── AGENTS.template.md
            ├── architecture.template.md
            ├── contract.template.md
            └── task.template.md
```

---

## Prerequisites / 前置条件

1. **Antigravity** (global config directory: `~/.gemini/config/`).
2. **Codex CLI** with `codex queue` support (`codex queue --help`).
3. **Python 3.8+** (standard library only, zero `pip install` needed).
4. **Git** initialized in your working repository.

---

## Installation / 本地全局安装（实时同步）

Clone this repository locally, then register `skills/orchestrator-codex` globally in Antigravity so any local edits take effect immediately without copying files.

### macOS / Linux (Symlink / 软链接)

```bash
git clone https://github.com/AlphaYuU/orchestrator-codex.git ~/orchestrator-codex
mkdir -p ~/.gemini/config/skills
ln -s ~/orchestrator-codex/skills/orchestrator-codex ~/.gemini/config/skills/orchestrator-codex
```

Verify / 验证：

```bash
ls -la ~/.gemini/config/skills/orchestrator-codex/SKILL.md
```

### Windows (Recommended: `~/.gemini/config/skills.json` Manifest)

> **Why not `mklink /J` / Directory Junction on Windows?** Antigravity's file scanner silently skips Windows NTFS Directory Junctions (`ReparsePoint` / `ModeIrregular`). Registering the real directory via Antigravity's official global discovery manifest `~/.gemini/config/skills.json` works across Antigravity App, CLI (`agy`), and IDE with zero copying and no admin rights required.
>
> **为什么 Windows 不推荐目录联接（Junction）？** Antigravity 底层扫描器在 Windows 上会静默跳过 NTFS Junction；使用官方全局清单 `~/.gemini/config/skills.json` 直接指向仓库真实路径即可零复制实时生效，且无需管理员权限（注意保存为 **UTF-8 无 BOM**）。

```powershell
git clone https://github.com/AlphaYuU/orchestrator-codex.git D:\Project\Skill\orchestrator-codex
New-Item -ItemType Directory -Force "$HOME\.gemini\config" | Out-Null

# Writes BOM-free UTF-8 skills.json (if skills.json already exists, merge the entry into "entries")
$json = @'
{
  "entries": [
    {
      "path": "D:\\Project\\Skill\\orchestrator-codex\\skills"
    }
  ]
}
'@
[System.IO.File]::WriteAllText("$HOME\.gemini\config\skills.json", "$json`n", (New-Object System.Text.UTF8Encoding($false)))
```

Verify / 验证：

- **Antigravity CLI (`agy`)**: Run `/skills reload`, then `/skills` and confirm `orchestrator-codex` is listed.
- **Antigravity App / IDE**: Open **Customizations → Installed** (or **Settings → Projects → Customizations**) and confirm `orchestrator-codex` appears with a **Global** badge.

### Optional Project Setup / 可选：目标项目初始化

- Copy `skills/orchestrator-codex/references/AGENTS.template.md` to `<your-project>/AGENTS.md` to enforce Codex Subagent guardrails on every turn.
- Copy `skills/orchestrator-codex/references/architecture.template.md` to `<your-project>/docs/architecture.md` (maintained by Gemini) to preserve long-term architectural memory across new windows.
- Add `.agent-collab/` to `<your-project>/.gitignore`.

---

## Daily Dual-Window Workflow / 双窗口日常使用步骤

1. **Open two windows side by side / 左右打开双窗口**
   - Left window: **Antigravity (Gemini)** — manually pick your orchestrator/frontend model.
   - Right window: **Codex CLI** in the same project root — manually pick your backend model, reasoning effort, speed mode, and approval mode. On first use (or after a major feature merge), send `hi` (or `/new` + `hi`) so the session log is initialized.
2. **Clarify & align in Antigravity / 在 Antigravity 澄清需求与分支**
   - Describe what you want in Antigravity. Gemini reads `docs/architecture.md` first, clears any ambiguities via numbered options (**Ask-Don't-Guess**), confirms the task `Mode` (`AUDIT` / `SURGICAL_FIX` / `FEATURE_IMPL`), and switches to a user-confirmed `feat/<slug>` / `fix/<slug>` branch when needed.
   - Small intuitive edits (`<= 10` lines) are applied directly by Gemini without dispatching.
3. **Auto-bind UUID, dispatch & auto-wait / 自动绑 UUID、派单并轮询完工**
   - Gemini runs `find_codex_thread.py` to auto-discover and cache the project's Codex **UUID** in `.agent-collab/session.json` (asking `1`/`2` only if multiple sessions match).
   - Gemini cleans old `report.md`/`done.flag`, writes `.agent-collab/contract.md` and `task.md`, queues via `codex queue --thread "<UUID>" --message "..."` (zero model/reasoning overrides), and automatically runs `wait_codex_done.py`.
4. **Seamless handoff (clarification or diff review) / 无缝接力（请示或验收）**
   - Once Codex writes `report.md` and creates `done.flag`, `wait_codex_done.py` prints `CODEX_DONE` and auto-deletes `done.flag`.
   - If Codex reported `Status: BLOCKED_NEED_CLARIFICATION`, Gemini translates the 2–3 options into plain language for your decision before re-dispatching.
   - Otherwise, Gemini immediately runs mode-aligned independent diff review (`git status --short`, `git diff`, reading every `??` new file, and personally running verification commands).
5. **Accept, iterate, or circuit-break / 通过、打回或熔断**
   - **Pass**: Gemini summarizes verified results in plain language, updates `docs/architecture.md` if needed, and asks before committing.
   - **Fail**: Gemini queues targeted `file:line` fixes back via `codex queue --thread "<UUID>"` and re-polls (up to **3 rounds**, after which a circuit breaker hands control back to you with a full postmortem).
   - **Timeout**: If `wait_codex_done.py` outputs `CODEX_TIMEOUT`, check the right Codex window for a pending permission approval prompt.

---

## Inspiration & References / 参考与致谢

- Inspired by [`amElnagdy/delegate-skills`](https://github.com/amElnagdy/delegate-skills) (diff-first review, pre-flight CLI checks, orchestrator-owned Git commits).
- Built for the **Agent Skills** open format and **Codex CLI `codex queue`** cross-session supervision workflow.
