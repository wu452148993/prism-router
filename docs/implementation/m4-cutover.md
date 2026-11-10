# M4 Cutover — `.mcp.json` 替换 + 真实流量观察

> **范围。** M4 = 65 fixture 回归 + 替换文档 + 流量观察清单。
> 替换 `.mcp.json` 是**用户操作位**，不自动执行；M4 agent 只产出本文档。

## 0. 前置检查

```bash
cd D:/projects/prism-router
.venv/Scripts/python -m pytest -q                # 期望: 136 passed / 1 skipped / 68 deselected
.venv/Scripts/python -m pytest -m fixtures -q    # 期望: 65 passed / 140 deselected
codex --version                                  # 期望: codex-cli 0.116.0+
gemini --version                                 # 期望: 0.37.2+
```

任一失败 → 停下，修回归，**不要替换** `.mcp.json`。

## 1. 备份当前配置

把备份放到 **repo 外**，杜绝 `.mcp.json` 中 token 被 `git add .` 误带入：

```bash
cp D:/projects/prism-router/.mcp.json "$HOME/.prism-router.mcp.json.backup"
```

`.gitignore` 同时把 `.mcp.json*` 全部排除作为兜底，但首选位置仍是 repo 外。
**不要 commit / 分享备份**。

## 2. 替换 codex / gemini 块

打开 `.mcp.json`，**删除**当前 `codex` 与 `gemini` 两个 server 条目（来自
GuDaStudio `uvx --from git+...` 形态），替换为单条 `prism` 条目：

```json
"prism": {
  "type": "stdio",
  "command": "uv",
  "args": [
    "run",
    "--directory",
    "D:/projects/prism-router",
    "prism-router"
  ],
  "env": {},
  "alwaysLoad": true
}
```

**字段说明：**
- `command`/`args` 走 `uv run --directory <repo>` 调用 `pyproject.toml` 的
  `[project.scripts] prism-router` 入口（`prism_router.server:main`，stdio 模式）
- `alwaysLoad: true` — 与原 codex/gemini 条目对齐，避免 Claude Code 懒加载
- `env: {}` — 默认值；如需调时序/审批可加 `CODEX_TIMEOUT` /
  `CODEX_REQUIRE_APPROVAL` / `GEMINI_REQUIRE_APPROVAL` / `PRISM_LOG_LEVEL` /
  `GEMINI_MODEL`（见 `src/prism_router/config.py`）

**已 `pip install -e .` 的备选写法**（不依赖 uv 解析路径）：

```json
"prism": {
  "type": "stdio",
  "command": "prism-router",
  "args": [],
  "env": {},
  "alwaysLoad": true
}
```

## 3. 重启 + 验证

1. 完全退出 Claude Code（不是仅关窗口）
2. 重新启动 Claude Code 加载新 `.mcp.json`
3. 在新会话中确认两个 tool 在场：
   - `mcp__prism__codex`
   - `mcp__prism__gemini`
4. 跑短 prompt 烟测：`mcp__prism__codex(PROMPT="reply LGTM", cd="D:/projects/prism-router", sandbox="read-only")`
   - 期望 `success=true`、`agent_messages` 含 "LGTM"、`error=null`
5. C7 验证：临时把 `codex` binary 从 PATH 暂移（或 rename），重启 Claude Code
   → 期望 tools/list **不再列出** `mcp__prism__codex`（C7 条件注册生效）；
   显式调用时 Claude Code 应报 native "no such tool"（具体提示文字取决于 UX 版本）；
   恢复 binary 并重启验证恢复

## 4. 回滚

任一步骤异常：

```bash
cp "$HOME/.prism-router.mcp.json.backup" D:/projects/prism-router/.mcp.json
```

完全退出 + 重启 Claude Code。GuDaStudio 注册恢复。报告异常到 `prism-router`
issue 跟踪。

## 5. 真实流量观察清单（1–2 周）

替换生效后立即开始计时。不需要 agent 介入；**用户活动**。

### 5.1 数据采集

- 持续使用 Claude Code 的 `mcp__prism__codex` / `mcp__prism__gemini`，覆盖
  日常工作 + 故意触发的失败模式（断网、过长 PROMPT、停用 auth、不存在的 cwd）
- 每次失败手抄一行：`<timestamp> <tool> <error.code> <error.stage> <fixture-id-if-matches>`

### 5.2 第 7–14 天回收

1. 重做 audit 抽取（参考 `mcp-audit/README.md` 抽取流程；agentsview
   `/api/v1/sessions` 拉新 N 个 session 跨期）
2. 生成新 `calls.v2.json`（不覆盖 v1 `calls.json`）
3. 失败率对比：
   - 旧基线（GuDaStudio）：~50%（含 MISSING）
   - v1 治理目标：≤ 30–40%（`design-rationale.md §6.1`）
4. 残余失败按 `§4.2` `error.code` 分桶：
   ```
   proxy_503 / auth_401 / stream_disconnected / cli_too_long / ...
   ```
5. 决策点：
   - 若失败率 ≤ 20%：v2 daemon/前端 ROI 重新评估，可能仍 deferred
   - 若失败率 21–40%：v2 优先级按残余 code 桶排序（重点桶 → P0）
   - 若失败率 > 40%：v1 治理未达预期，先排查（典型：fixture 没覆盖到的真实根因桶）

### 5.3 v2 启动条件（暂不触发）

来自 `docs/README.md`「v2 启动条件」：

- v1 真实流量失败率治理至 ≤ 20%
- 「看过程」诉求经过 v1 使用后仍持续
- v2 工作量评估更新（参考 v1 LOC、测试覆盖、维护成本）

满足 → 切到 `design-rationale.md §8` daemon/REST/SSE/前端 SPA/agentsview
集成蓝图（v2-deferred 原文保留）。

## 附：env 调参速查

超时四件套定义在 `src/prism_router/runner.py`，其余在 `src/prism_router/config.py`。

| env | 默认 | 来源 | 用途 |
|---|---|---|---|
| `CODEX_TIMEOUT` | 7200 | runner.py | 整次 run 超时（秒） |
| `CODEX_POST_MESSAGE_DELAY` | 1.0 | runner.py | `turn.completed` 后延迟再 SIGTERM |
| `CODEX_GRACEFUL_TIMEOUT` | 5.0 | runner.py | terminate → kill 之间窗口 |
| `CODEX_FORCE_KILL_DELAY` | 5.0 | runner.py | kill 后等 OS 回收 |
| `CODEX_REQUIRE_APPROVAL` | false | config.py | true → 关闭默认 yolo bypass |
| `GEMINI_REQUIRE_APPROVAL` | true | config.py | false → 恢复默认 `-y`（gemini 取得写/shell 工具，见 `docs/bugs/gemini-default-write-capability.md`） |
| `GEMINI_MODEL` | (空) | config.py | 默认 model；调用方 `model=` 覆盖 |
| `PRISM_LOG_LEVEL` | info | config.py | logging level（debug/info/warning/error） |
