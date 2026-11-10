# prism-router 文档索引

> 项目目标：取代当前两个失败率 ~50% 的 `codex` / `gemini` MCP wrapper（来自
> [GuDaStudio](https://github.com/GuDaStudio/codexmcp) / [GuDaStudio/geminimcp](https://github.com/GuDaStudio/geminimcp)），
> 吸收 [CCG codeagent-wrapper](https://github.com/fengmengmengji/ccg-workflow/tree/main/codeagent-wrapper) 的工程实践。
>
> **范围分层（2026-05-02 决定）：**
> - **v1 MVP** = 单一 MCP server，wrapper-only，P0 修复（§6.1）。**当前实施目标**。
> - **v2** = 长驻 daemon + REST/SSE API + 解耦 SPA 前端 + [agentsview](https://github.com/wesm/agentsview) 集成。**设计完整保留在 §8**，但本期不实现；待 v1 真实流量验证失败率治理效果后再启动。
>
> 本文件是顶层汇总索引。具体设计/数据/样本散落在子目录，按下方"阅读顺序"开读最高效。

## 项目状态（2026-05-03）

| 阶段 | 状态 | 产物 |
|---|---|---|
| 失败数据采集（1032 calls，跨 224 个 Claude Code session） | ✅ 完成 | `mcp-audit/calls.json` |
| 失败分类与样本提取（10 个根因桶 + 3 个多轮 + 1 个重试） | ✅ 完成 | `mcp-audit/README.md`, `mcp-audit/samples/`, `mcp-audit/fixtures.json` |
| 架构决策（v1 失败治理；v2 daemon/前端/agentsview 集成） | ✅ 完成 | `mcp-audit/design-rationale.md` §1–§8 |
| 上游 CLI 行为外部验证（VERIFY 1–6 + E1/E2） | ✅ 完成 | `mcp-audit/design-rationale.md` §9 |
| Codex 第 1 轮设计审查（4 BLOCKER + 4 ISSUE 已落地） | ✅ 完成（2026-05-02）| `design-rationale.md` 修订 |
| Codex 第 2 轮 sanity check | ✅ 完成（落地于 M1）| SESSION_ID `019de8ce-b815-7f43-8e02-a5372e46f32e` |
| v1 实现 M1（骨架 + §4.2/§4.4 纯单测） | ✅ 完成（2026-05-02，含 codex 第 2 轮 BLOCKER 修复）| `src/prism_router/{errors,events,parser}.py` + 30 测全绿；`implementation/v1-plan.md`；codex SESSION_ID `019de8ce-b815-7f43-8e02-a5372e46f32e` |
| v1 实现 M2（Backend + spawn + stdin + kill 链） | ✅ 完成（2026-05-03，含 codex 审计 3 ISSUE+1 NIT 全落地）| `src/prism_router/{paths,backend_codex,backend_gemini,runner}.py` + tests/{test_paths,test_argv,test_runner_pure}.py + `tests/integration/test_smoke.py`（local-only）；65 默认通过 / 1 POSIX skip / 3 integration deselect；codex SESSION_ID `019de9c0-c496-7f10-b695-6e59197c6db2` |
| v1 实现 M3（FastMCP 注册 + C7 smoke probe + 分类 + cap + auth pre-flight） | ✅ 完成（2026-05-03，含 codex 审计 2 BLOCKER+2 ISSUE 全落地）| `src/prism_router/{config,cap,classify,auth,server}.py` + `errors.py` 增 CodexResult/GeminiResult；`tests/test_{config,cap,classify,auth,server}.py`；136 默认通过 / 1 POSIX skip / 3 integration deselect；codex SESSION_ID `019dea0b-9a46-7b80-bf3f-aa0e9bbba3cb` |
| v1 实现 M4（fixtures.json 65 条回归 + cutover 文档 + 真实流量观察清单） | ✅ 完成（2026-05-03）| `tests/test_fixtures.py`（Strategy B-mocked-spawn，hermetic 65 通过）+ `docs/implementation/m4-cutover.md`（备份/替换/重启验证/回滚 + 1–2 周观察清单）；136 默认通过 / 1 POSIX skip / 68 deselect（3 integration + 65 fixtures）|
| v2 实现（§8 daemon/frontend/agentsview） | ⏸️ deferred | — |

## 文档地图

| 路径 | 类型 | 一句话定位 |
|---|---|---|
| `README.md` (本文件) | 索引 | 顶层导览 + 决策时间线 + 阅读顺序 |
| `implementation/v1-plan.md` | **实现接力点** | 当前 milestone 状态 + M2/M3/M4 文件清单与陷阱 + 不变量速查 + 接力检查表。**新会话进来后第二个读的文件**（CLAUDE.md 之后） |
| `mcp-audit/README.md` | 失败分类报告 | 1032 调用的失败桶分布、root cause、样本指针；先读 |
| `mcp-audit/calls.json` | 数据集 | 全量调用元数据（每条含 `tool_name/args/result/success/error/has_session/...`）；可重派生所有断言 |
| `mcp-audit/fixtures.json` | 回归集 | 65 条精选 fixture，覆盖每种失败类型；新 wrapper 必须对每条产生**不同**的输出 |
| `mcp-audit/samples/*.txt` | 原始样本 | 每种失败模式 1–2 个 verbatim 转储（PROMPT + 完整 result envelope）；root-cause 调试用 |
| `mcp-audit/design-rationale.md` | 架构决策 | §1 决策表 / §2 反模式说明 / §3 CCG 借鉴清单 / §4 新 MCP schema (含 §4.2 error envelope 三元组 / §4.4 事件解析白名单) / §5 测试策略 / §6 优先级（含 §6.1 v1 MVP / §6.2 v2 deferred / §6.3 stream_max_retries 透传 footnote）/ §7 外部链接 / §8 前端+agentsview 集成（**v2-deferred，原文保留**）/ §9 验证记录 V1–V6 |

## 决策时间线（按时间顺序的关键决策，全部已 SETTLED）

1. **保留 MCP，不切 Bash wrapper** — prism-router 是项目的 MCP 层投资，不放弃 schema/sessions/permissions。`design-rationale.md §1`
2. **MCP tool 表面保持同步** — `codex(prompt) → result`，无 progress 通知，无 start/poll/cancel 三 tool 模式。`§1` + `§2`
3. **Mid-flight observability 是陷阱（对 AI-AI）** — token 经济与决策质量双重否决；不做实时干预。`§2`
4. **CCG borrow list 锁定** — stdin 安全检测、Codex 0.80 `--json` bug 修复、Gemini `{` fallback、Reconnecting 过滤、force-kill 链、Backend 协议、bypass-approval、Windows 三件套（path norm + CREATE_NO_WINDOW + stdin pipe）。`§3.1–§3.3`
5. **VERIFY 1–4 + E1/E2 完成（2026-05-02）** — codex/gemini stdin 全平台支持、codex resume 不能救 mid-stream、gemini stdout 污染上游 PR #26264 在审、gemini session 路径用 sha256(cwd) 不是 basename。`§9`
6. **C2 stream-resume 撤销** — 原 P1 项被 V2 证伪；改为透传 `error.code:"stream_disconnected"` 让上游决策。`§6.2` + `§9 V2`
7. **三诉求架构定型（前后端分离 + API + UI 查看过程）** — 引入 `prism-router-daemon` 长驻服务承接 wrapper 内部诊断 + REST/SSE API；`prism-router-mcp` 退化为 stdio 薄壳；前端 SPA 解耦独立。`§8.1–§8.3`
8. **agentsview 不重建，做集成** — 历史/跨 agent 浏览委托 agentsview；daemon 只持久化 wrapper-internal 数据（stderr_tail / kill 链 / PID / `error.code` 三元组 / warm pool）；agentsview 是 pull 模式 file-watcher，prism-router 完全不向它推送。`§8.1`
9. **agentsview 集成策略：probe + degrade，不 bundle 不 auto-start** — `AGENTSVIEW_BASE` env 可覆盖端口；前端三态展示（UP / INSTALLED_NOT_RUNNING / NOT_INSTALLED）；`--start-agentsview` 自启 flag 拒绝，ROI 严重负。`§8.4`
10. **前端整合 agentsview 用深链而非 iframe / API 代理** — A 方案 + 三增强（状态感知链 / `thread_id ↔ agentsview_session_id` 映射缓存附 indexing 状态 / wrapper 诊断侧栏）。iframe 因 agentsview 安全中间件大概率 X-Frame-Options 拒绝；API 代理重新实现 agentsview UI 维护负担过大。`§8.5`
11. **MVP 范围 = A+（仅 v1 P0，§8 v2 设计完整保留）** — Codex 第 1 轮审查发现"P0 alone fixes the bulk"措辞夸大；同时 daemon/前端/agentsview 集成估算 3000+ LOC，应先用 v1 P0 验证失败率治理效果再决定 v2 形态。v1 目标"治~30–40%、把剩下 60% 精确分类供 v2 定向"，§8 v2 蓝图原文保留不删。`§6.1` + `§6.2`
12. **codex `stream_max_retries` 纯透传，不写 wrapper 逻辑** — V5/V6 锁定后证据显示：默认 5 次重试已耗尽（6/16 audit 样本）、每次重试整请求重发 N×token、`--config` 必须用 `model_providers.<active>.stream_max_retries` 嵌套点路径且 wrapper 无法可靠探测 active provider。Wrapper 仅做 `error.code:"stream_disconnected"` 分类 + `Reconnecting…N/M` 入 `stderr_tail`。零代码。沉淀为 `feedback_zero_code_priority.md` ROI 原则。`§6.3` + `§9 V5/V6`
13. **Codex 第 1 轮设计审查（B1–B4 + I2–I4 落地）** — `error_type` 6 值枚举重写为 `{code, stage, retryable}` 三元组（覆盖全 16 audit 桶）`§4.2`；`§4.4` 加 gemini event-type 白名单；`§6.1` P0 增 G1/C1/C3/C7；`§8.4` `AGENTSVIEW_BASE` 可配；`§8.5` 加 indexing 状态 + 强制刷新；新增 `§8.8` MCP shim ↔ daemon lifecycle edges + `§8.9` daemon 单例 + probe jitter。SESSION_ID `019de728-c5ed-73c2-a666-28be506288f6` 待 round 2 sanity。

## 验证摘要（详见 `design-rationale.md §9`）

| ID | 问题 | 结论 |
|---|---|---|
| V1 | codex exec 是否支持 stdin？ | ✅ 是，位置参数 `-` |
| V2 | codex resume 能救 mid-stream 中断？ | ❌ 否，resume = 加载历史 + 新 prompt |
| V3 | gemini stream-json 与日志通道分离？ | ⚠️ 上游 PR #26264 在审；wrapper 端必须 per-line skip + 事件白名单 |
| V4 | gemini --resume 是否用 basename 查路径？ | ❌ 否，用 sha256(cwd)；wrapper 端只需路径规范化 |
| V5 | codex `stream_max_retries` 实际行为？ | 默认 5；整请求重发（非字节级续传）；partial deltas 丢弃；N× 计费 |
| V6 | codex `--config` 嵌套 key 覆盖规则？ | 必须用 `model_providers.<active>.stream_max_retries=N`；扁平写法无效；provider 隔离 |
| E1 | gemini 是否原生支持 stdin？ | ✅ 是，官方推荐 `echo "..." \| gemini` |
| E2 | gemini `--prompt`/`-p` 是否已 deprecated？ | ✅ 是，弃用警告污染 stdout |

## 阅读顺序（按角色）

**v1 实现者（即将动手写代码的人）：**
1. `implementation/v1-plan.md` — **当前实现状态 + 下一步接力点**；含里程碑表、文件清单、陷阱速查、不变量列表
2. `mcp-audit/design-rationale.md §6.1` — v1 P0 全集 13 行表（任务源头）
3. `mcp-audit/design-rationale.md §4.2 + §4.4` — error envelope 三元组 + 事件解析白名单（实现 contract）
4. `mcp-audit/README.md` 全文 — 失败分布与根因
5. `mcp-audit/samples/codex-503-backend-down.txt` + `samples/gemini-augment-stdout-pollution.txt` — 这两个文件解释了 ~40 个失败案例
6. `mcp-audit/design-rationale.md §9` — 上游 CLI 行为外部验证记录（V1–V6 + E1/E2）
7. `mcp-audit/design-rationale.md §6.3` footnote — 解释为何 stream_max_retries 不在代码里实现
8. 浏览 `mcp-audit/fixtures.json` — 回归测试基线（M4 用）

**§1–§7、§9 全文**仅在调试 M2/M3 具体陷阱时按需回看。**§8 不读**（v2-deferred）。

**审阅者 / 决策回看：**
1. 本文件「决策时间线」 — 一屏看到所有 SETTLED 决策
2. `mcp-audit/design-rationale.md §1` 决策表 — 主表
3. `mcp-audit/design-rationale.md §6` 优先级 — v1 / v2 划分

**v2 接手者（未来某天启动 daemon/前端/agentsview）：**
1. `mcp-audit/design-rationale.md §8` 全文 — 设计原文 (v2-deferred 标记下完整保留)
2. `mcp-audit/design-rationale.md §6.2` — v2 项目清单 + 优先级
3. `mcp-audit/design-rationale.md §8.6` — REST API 表（v2 第三方对接 contract）
4. `mcp-audit/design-rationale.md §8.4` — agentsview probe 状态机
5. `mcp-audit/design-rationale.md §8.8 + §8.9` — shim ↔ daemon lifecycle + 单例 + probe jitter
6. v1 实施完成后真实流量数据 — 决定 v2 是否仍 ROI 正
7. `feedback_zero_code_priority.md` memory — ROI gate 原则，v2 决策同样适用

**集成方 / 第三方程序对接 prism-router API（v2 上线后）：**
1. `mcp-audit/design-rationale.md §8.6` REST API 表
2. `mcp-audit/design-rationale.md §8.4` agentsview 集成状态语义

## 外部依赖与参考

| 依赖 | 版本 / 链接 | 角色 |
|---|---|---|
| agentsview | <https://github.com/wesm/agentsview>（v0.26+）| 历史/跨 agent 会话视图层；daemon probe 检测；前端深链跳转 |
| openai/codex | <https://github.com/openai/codex>（v0.91+）| 被 wrap 的 CLI；stdin 协议、resume 语义、stream-json schema 已验证 |
| google-gemini/gemini-cli | <https://github.com/google-gemini/gemini-cli>（v0.36+）| 被 wrap 的 CLI；stream-json 事件 schema 已验证；上游 PR #26264 跟踪 |
| CCG codeagent-wrapper | <https://github.com/fengmengmengji/ccg-workflow/tree/main/codeagent-wrapper>（v5.10.0）| 工程实践参考（stdin 安全 / force-kill 链 / Windows 三件套） |
| 当前被替换的 codexmcp | <https://github.com/GuDaStudio/codexmcp> | 失败率 ~57%（含 MISSING） |
| 当前被替换的 geminimcp | <https://github.com/GuDaStudio/geminimcp> | 失败率 ~49%（含 MISSING） |

## 当前阻塞 / 待回答的问题

无。v1 P0 / M1–M4 已 ship。下一步是按 `implementation/m4-cutover.md` 执行 `.mcp.json`
cutover，1–2 周真实流量后回收 `calls.v2.json` 比对新失败率。

**v1 下一步**（cutover + 观察期，**用户自驱**）：
1. 按 `implementation/m4-cutover.md` 备份（落 repo 外）→ 替换 → 重启 → 验证 → 保留回滚路径
2. 真实流量跑 1–2 周，agentsview 重抽 `calls.v2.json`
3. 残余失败按 `design-rationale.md §4.2` `error.code` 分桶
4. **据真实数据重评**：v2 daemon/前端是否仍 ROI 正？v2-P0 优先级是否需要重排？

**v2 启动条件**（暂未触发）：
- v1 在真实流量中失败率治理至 ≤ 20%（从当前 ~50%）
- 用户对"看过程"的诉求经过 v1 使用后仍持续（不是想象需求而是真实痛点）
- v2 工作量评估更新（参考 v1 实际 LOC、测试覆盖、维护成本）
