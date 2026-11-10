# prism-router v1 implementation plan

> **接力点。** 下一个上下文进来后只需读：
>
> 1. `CLAUDE.md`（自动加载，强制约束）
> 2. **本文件**（实现状态 + 不变量 + 下一步任务定义）
> 3. `docs/mcp-audit/design-rationale.md` §6.1（v1 P0 13 行清单 — 任务源头）
> 4. 仅在需要细节时回看 §4.2（envelope）/ §4.4（parser）/ §9（外部验证）
>
> §8（v2 daemon/前端/agentsview）**不读**。本期不实现。
>
> 本文件是**实现侧**的状态机。`design-rationale.md` 是**决策侧**的归档。两份分工，禁止重复。

## 0. 项目状态总览

| 里程碑 | 范围（一句话）| 状态 | 可验证物 |
|---|---|---|---|
| M1 | 工程骨架 + §4.2/§4.4 纯单测 | ✅ 完成 | `pytest -q` 30 通过；详见 §1 |
| M2 | codex/gemini Backend + asyncio spawn + stdin pipe + force-kill 链 | ✅ 完成（2026-05-03，含 codex 审计 3 ISSUE+1 NIT 全落地）| `pytest -q` 65 通过 / 1 skip(POSIX) / 3 deselect(integration)；codex SESSION_ID `019de9c0-c496-7f10-b695-6e59197c6db2`；详见 §1 |
| M3 | FastMCP 注册 + C7 smoke probe + 错误分类 + oversize cap + auth pre-flight | ✅ 完成（2026-05-03，含 codex 审计 2 BLOCKER+2 ISSUE 全落地）| `pytest -q` 136 通过 / 1 skip(POSIX) / 3 deselect(integration)；codex SESSION_ID `019dea0b-9a46-7b80-bf3f-aa0e9bbba3cb`；详见 §1 |
| M4 | `fixtures.json` 65 条回归 + `.mcp.json` 替换 cutover doc + 真实流量观察清单 | ✅ 完成（2026-05-03）| `pytest -q` 136 / 1 skip / 68 deselect；`pytest -m fixtures` 65 通过；`docs/implementation/m4-cutover.md`；详见 §5 |

**M3 落地的关键修复（codex 审计驱动）：**
1. BLOCKER 1 — `CODEX_REQUIRE_APPROVAL` 接入 `SpawnConfig.yolo`（`yolo or not require_approval`），默认 bypass 生效；操作员可通过 env 强制要求审批，调用方 `yolo=True` 仍可覆写。
2. BLOCKER 2 — classify 升级：error_events 走完后 fallback 扫描 `stderr_tail`（gemini 还含 noise_log）的 503/401/disconnect/invalid-session，不再因 codex bare-failure 文本只在 stderr 而误归 spawn_failed。新增 `no_session/spawn` 路径覆盖 gemini-invalid-session-id。
3. ISSUE 1 — spawn 时异常捕获从 `FileNotFoundError` 扩到 `OSError`（含 PermissionError/ENOEXEC），envelope 契约不被破坏。
4. ISSUE 2 — auth pre-flight 豁免直连官方 `api.openai.com`：`OPENAI_BASE_URL=https://api.openai.com/...` + OAuth in `auth.json` 是合法路径，不再误报。

P0 全集 = §6.1（design-rationale），13 行表。M2/M3 各覆盖一部分；本文件 §3/§4 给出每一项的**实现位置**。

## 1. 当前源码状态（M4 落盘）

**M1 + M2 + M3 + M4 已实现**（默认 `pytest -q` 136 通过 / 1 skip / 68 deselect；
`pytest -m fixtures` 65 通过；M4 仅新增 `tests/test_fixtures.py` +
`docs/implementation/m4-cutover.md`，未触碰 `src/prism_router/`，M3 ship 不变）：

```
src/prism_router/
  __init__.py
  errors.py        # M1 + M3：CodexResult / GeminiResult TypedDict 加入；TypedDict 改 typing_extensions（Pydantic v2.13 要求）
  events.py        # M1
  parser.py        # M1
  backend.py       # M1
  paths.py         # M2
  backend_codex.py # M2
  backend_gemini.py# M2
  runner.py        # M2
  config.py        # M3：ServerConfig（CODEX_REQUIRE_APPROVAL / GEMINI_MODEL / PRISM_LOG_LEVEL）frozen dataclass
  cap.py           # M3：cap_envelope — UTF-8 char-count 100K hard cap
  classify.py      # M3：(ParseResult, returncode, stderr_tail, timed_out, spec) → ErrorEnvelope|None
                   #     11 priority 阶梯；error_events + stderr_tail + (gemini) noise_log 三路 503/401/disconnect/invalid-session 检测
  auth.py          # M3：preflight_codex_auth — env + ~/.codex/auth.json 启发；
                   #     OPENAI_BASE_URL=api.openai.com 豁免直连官方
  server.py        # M3 重写：FastMCP 实例 + C7 smoke probe（条件注册）+ codex/gemini @mcp.tool +
                   #     stdio main 入口；GuDaStudio 字段名/顺序兼容（PROMPT/cd/SESSION_ID/...）

tests/
  test_errors.py        # M1
  test_parser.py        # M1
  test_paths.py         # M2
  test_argv.py          # M2
  test_runner_pure.py   # M2
  test_config.py        # M3：6 测 — defaults / truthy 变体 / log level mapping / frozen
  test_cap.py           # M3：7 测 — under/at/over limit / emoji / CJK / custom limit / empty
  test_classify.py      # M3：33 测 — 优先级阶梯全集 + stderr fallback + invalid-session/spawn
  test_auth.py          # M3：9 测 — env/file 组合矩阵 + 直连官方豁免 + envelope shape
  test_server.py        # M3：15 测 — C7 条件注册（4 矩阵）+ approval 配置（4 矩阵）+ envelope 构造
  test_fixtures.py      # M4：65 测（@pytest.mark.fixtures，默认 deselect）— 65 条 fixture
                        #     回放 Strategy B-mocked-spawn；layered 断言：legacy leadin 不复现
                        #     + envelope 形状契约 + 类别专属 EXPECTED_CODE + MISSING 桶非空
                        #     + GuDaStudio dict-shape signatures 不泄漏
  integration/
    test_smoke.py       # M2（@pytest.mark.integration）
```

**M2 落盘原文件清单（保留以便对照历史）：**

```
src/prism_router/
  __init__.py             # version
  errors.py        (89)   # §4.2 ErrorEnvelope / Diagnostics / make_error / 15 codes / 7 stages
  events.py       (143)   # EventSpec + GEMINI_SPEC + CODEX_SPEC + AND-group required fields
                          # + is_transient_codex_error (Reconnecting filter)
  parser.py       (149)   # StreamParser：白名单 → 必需字段校验 → noise_log(cap 100)
                          # + 首行 `{` fallback (CCG 6988f64) + 多消息 \n join (H2)
                          # + saw_terminal property（M2 新增，给 runner 钩子）
  backend.py       (29)   # Backend Protocol + SpawnConfig（M2 已落地）
  paths.py         (29)   # M2: normalize_cwd — Git Bash /c/... + normcase + 绝对路径
  backend_codex.py (43)   # M2: CodexBackend.build_args（exec --json -C ... [resume <sid>] -）
  backend_gemini.py(35)   # M2: GeminiBackend.build_args（-o stream-json [-r sid] [-y] ...）
  runner.py       (211)   # M2: asyncio spawn loop / stdin PROMPT / 行级 parser / 20-line stderr
                          # ring / turn.completed→POST_MESSAGE→TERM→GRACEFUL→KILL→FORCE_KILL
                          # / Windows CREATE_NO_WINDOW / RunTimeouts.from_env / Spawner 注入
  server.py        (15)   # main() 占位（M3 实现）

tests/
  test_errors.py        (104)   # envelope 形状 + 15 codes 全集 + 7 stages
  test_parser.py        (398)   # 21 测：augment-pollution、首行 fallback、AND-group、Reconnecting、
                                # codex item.completed→item.text、cap、空行、saw_terminal、首 init 优先
  test_paths.py          (69)   # M2: 8 测 — Git Bash drive / 反斜杠/正斜杠 collapse / case fold /
                                #              idempotent / 绝对化 / Path 入参 / POSIX passthrough
  test_argv.py          (157)   # M2: 18 测 — codex+gemini build_args 全矩阵；resume 顺序锁定；
                                #              `-` 末位；--prompt 永不出现；gemini 无 --cwd flag
  test_runner_pure.py   (301)   # M2: 9 测 — stdin write+close / 自然退出无 kill / 终结事件→TERM /
                                #              graceful→KILL escalation / 总超时→TERM 链 / stderr
                                #              ring buffer 取末 20 / cwd= kwarg / stdout/stderr 隔离 /
                                #              env 默认与覆盖
  integration/
    test_smoke.py        (60)   # M2: @pytest.mark.integration（默认 deselect）— 真实 codex/gemini
                                #     binary 短 prompt + 强制超时 kill 链验证
```

**M2 关键不变量（已编码进测试）：**
- `CodexBackend.build_args` 末位**永远** `"-"`；resume 子命令仅在 SESSION_ID 非空时插入；所有 flag 都在 `exec` 与 `resume` 之间（clap 父作用域）
- `GeminiBackend.build_args` **不**包含 `--cwd`（上游无此 flag）；runner 通过 `subprocess cwd=` 落实工作目录
- runner kill_chain 顺序：Windows = `[TerminateProcess, kill_alias]`；POSIX = `[terminate, kill]`；reason 字段区分 `post_terminal`、`overall_timeout`、`cancelled`
- stderr 与 stdout 完全隔离：parser 仅吃 stdout；stderr 全程后台 readline；ring buffer = 末 20 行
- `RunTimeouts.from_env` 接受 env 字典注入（测试用）；CODEX_TIMEOUT/POST_MESSAGE_DELAY/GRACEFUL_TIMEOUT/FORCE_KILL_DELAY 默认 7200/1.0/5.0/5.0
- `Spawner` 可注入：`run(..., spawner=fake)` 让 pure tests 不需要真实子进程

**§3.1 与上游实测的偏差（已落地）：**
- 计划写的是 `["gemini", "-o", "stream-json", "--cwd", str(cfg.cwd)]`，实测 `gemini-cli@main` `packages/cli/src/config/config.ts` 无 `--cwd` flag。改为不传 flag、用 `subprocess cwd=` kwarg。
- codex `--yolo` 是 `--dangerously-bypass-approvals-and-sandbox` 的短别名（确认于 `codex-rs/utils/cli/src/shared_options.rs`）。代码统一使用长名以保留语义清晰。

**关键不变量（已编码进测试，禁止破坏）：**

- `ErrorEnvelope` keys 必须严格 = `{code, stage, retryable, raw, stderr_tail}` 五元组
- 15 个 code 与 §4.2 audit-bucket map 一一对应（`cli_too_long` 复用于 codex+gemini Windows overflow）
- type 白名单 + 必需字段双重门：仅 type 不够（noise 会带合法 type）
- 前缀 `{` substring fallback 穿过 init 前的 pollution/noise，**遍历同一行内所有 `{` 位置**，且仅接受通过 init-event 必需字段校验的候选；首个正常 stream event 被接纳后即关闭，后续同模式 → noise（语义稳定性）
- 解析器**纯函数**：`feed_line(str)` 无副作用，无 IO，无网络，无 logging。M2 spawn loop 直接喂行即可。

**测试运行命令：**

```bash
uv venv --python 3.11               # 一次性
uv pip install -e ".[dev]"          # 一次性
.venv/Scripts/python -m pytest -q   # Windows
# 或 source .venv/bin/activate && pytest -q  (Unix)
```

## 2. 锁定的设计不变量（禁止再讨论）

来自 `docs/README.md` 决策时间线 + `design-rationale.md` §1/§6/§9。任何与此冲突的"灵感"必须有新证据才能反对。

| 不变量 | 出处 | 一句话 |
|---|---|---|
| 单 MCP server / 同步 tool / 无 progress / 无 start-poll-cancel | §1 | AI-AI 中途观测是陷阱（§2 token 经济 + 决策质量双否） |
| PROMPT 走 stdin（codex `-`，gemini 直管道）所有平台 | §3.3 + §9 V1+E1 | 消除 argv overflow 整类 bug |
| 整章 §8（daemon/前端/agentsview）= v2-deferred | §6.2 + 决策 11 | v1 = wrapper-only 验证失败率治理后才决定 v2 形态 |
| `stream_max_retries` 纯透传，不写 wrapper 代码 | §6.3 + §9 V5/V6 | ROI gate 原则；`feedback_zero_code_priority.md` |
| C2 stream-resume 不做 | §6.2 + §9 V2 | codex resume 不能救 mid-stream，仅起新 turn |
| C7 = backend `--version` smoke probe；失败则不注册 tool | §6.1 + §8.8 note | Claude Code 看到 native "no such tool" = 正确归因 |
| Gemini path = sha256(cwd) | §9 V4 | wrapper 只需路径规范化（H1），**不**实现自定 hash |
| envelope 三元组 = `{code, stage, retryable}` + raw + stderr_tail | §4.2 | 15 codes 覆盖全 16 audit 桶 |
| 解析器 type 白名单 + 必需字段双重门 | §4.4 | 仅 type 不够，noise 会伪造 |

**外部模型（codex/gemini）调用约束：** v1 实现里**禁止**调用 `mcp__codex__codex` / `mcp__gemini__gemini` —— 它们正是被替换对象，行为不可信。外部联网用 `mcp__grok-search` / `mcp__exa`；上游源码引用首选 `mcp__context7` 或 GitHub URL。

## 3. M2：Backend + spawn 循环（下一接力点）

**目标：** 把 M1 的纯解析器接到真实 codex/gemini 子进程上。所有 §6.1 P0 中"行为类"修复在此落地。

### 3.1 新文件

| 文件 | 作用 |
|---|---|
| `src/prism_router/paths.py` | Git Bash `/c/...` → `C:/...` 规范化 + `os.path.normcase` + `/` 统一 + 解析为绝对路径。20 LOC 内。 |
| `src/prism_router/runner.py` | asyncio spawn loop：`run(backend, prompt, cfg, *, timeout, post_msg_delay) → RunOutcome`。stdin 喂 prompt → 实时按行喂 `StreamParser` → `turn.completed` 触发 graceful terminate → stderr 末 20 行捕获 → 返回 `(parse_result, stderr_tail, kill_chain, returncode)`。 |
| `src/prism_router/backend_codex.py` | `CodexBackend.build_args(cfg) → list[str]`：基础 `["codex", "exec", "--json", "-C", str(cfg.cwd), "-"]` + sandbox/skip-git/yolo/profile/model 透传 + `resume <id>` 当 SESSION_ID 非空。**永不**带 `--prompt` 类参数。 |
| `src/prism_router/backend_gemini.py` | `GeminiBackend.build_args(cfg)`：`["gemini", "-o", "stream-json", "--cwd", str(cfg.cwd)]` + `--yolo` + `--resume <id>` 当 SESSION_ID 非空。永不带 `-p`。 |

### 3.2 §6.1 P0 中本里程碑落地的项

| P0 项 | 落点 |
|---|---|
| env 驱动 timeout 套件（CODEX_TIMEOUT / POST_MESSAGE_DELAY / GRACEFUL_TIMEOUT / FORCE_KILL_DELAY） | `runner.py` 读 env，传入 `asyncio.wait_for` 与 kill 链 |
| Codex 0.80 `--json` non-exit 修复（`turn.completed` 后 sleep 再 terminate） | `runner.py` parse 到 `turn.completed` 时启动 `POST_MESSAGE_DELAY` 后 SIGTERM |
| **PROMPT via stdin** | `runner.py` `subprocess.PIPE` + `transport.write(prompt.encode())` + `close()` |
| stderr_tail 末 20 行 | `runner.py` 后台 task 持续 readline stderr，环形缓冲 20 |
| Windows `CREATE_NO_WINDOW` | `runner.py` 平台分支 `creationflags=subprocess.CREATE_NO_WINDOW` |
| 路径规范化（H1 + Git Bash） | `paths.py` 单函数 `normalize_cwd(p) → Path`，server.py 入口处调用 |

### 3.3 测试策略

**单测（无 IO）：**
- `tests/test_paths.py`：`/c/Users/foo` → `C:/Users/foo`、`D:\foo\bar` 与 `D:/foo/bar` 经规范化得相同字符串、`os.path.normcase` 后大小写折叠
- `tests/test_argv.py`：`CodexBackend.build_args` 在各种 cfg 组合下产生预期 argv；同理 gemini
- `tests/test_runner_pure.py`：把 stdin/stdout 替换为内存对象，验证 stream→parser 流水管线 + kill 链顺序（用 `asyncio.create_subprocess_exec` 的 mock）

**集成（真实 CLI，local-only，不入 CI）：**
- `tests/integration/test_smoke.py`（标 `@pytest.mark.integration`）：
  - codex / gemini 各跑一条短 prompt（"reply LGTM"），断言 `agent_messages == "LGTM"` 且 `error is None`
  - 故意 `CODEX_TIMEOUT=1`，断言 `code:"timeout"` `stage:"shutdown"` 且 kill_chain 非空

### 3.4 已知陷阱

1. **codex stdin 协议**：VERIFIED §9 V1 — `codex exec` 用位置参数 `-` 表示从 stdin 读。`build_args` 末位必须 `"-"`，且 spawn 时 `stdin=PIPE` 写完立即 close。
2. **gemini stdin**：VERIFIED §9 E1 — 原生支持 `echo "..." | gemini`，无需特殊 flag。`-p`/`--prompt` 已 deprecated 且**会污染 stdout**（E2），不要用。
3. **`turn.completed` kill chain 是平台相关的**：等 `POST_MESSAGE_DELAY`（默认 1.0s）后 `process.terminate()`。POSIX 上这等于 SIGTERM，若 `await process.wait()` 在 `GRACEFUL_TIMEOUT`（5s）内未退则 `process.kill()`（SIGKILL）+ 等 `FORCE_KILL_DELAY`（5s）让 OS 回收。**Windows 上**：`asyncio` subprocess `terminate()` 直接调 `TerminateProcess`，`kill()` 仅是 hard-kill 别名 / fallback —— **不存在**优雅退出窗口；kill_chain 标签用 `TerminateProcess` / `kill_alias`，且不应假设子进程能清理资源。
4. **stderr 与 stdout 分离**：parser 只吃 stdout；stderr 全程后台 readline，**别**关 stderr pipe，否则进程可能阻塞。
5. **Windows console flash**：CCG `5a9e0e2`。`subprocess.CREATE_NO_WINDOW` 仅 Windows 可用，用 `if sys.platform == "win32"` 守。
6. **Path normalize 时机**：在 server.py tool 入口（用户传入 `cd` 时）就做，**不**等到 backend 内部再做 — 后者会让 SESSION_ID hash 不稳。
7. **不做 stream-resume 重试**：VERIFIED §9 V2 — `codex resume` 只能加载历史 + 起新 turn，**无法**救 mid-stream 中断。M2 runner 必须把断流分类为 `stream_disconnected` 然后返给上层，**严禁**自动 spawn `codex resume` 作为恢复手段。

## 4. M3：FastMCP 注册 + 分类 + cap

**目标：** server.py 暴露 `codex` / `gemini` 两个 tool；启动时 smoke probe；运行时把 runner 输出 + parser 结果 →（C1/C3 分类 →）envelope。

### 4.1 新文件

| 文件 | 作用 |
|---|---|
| `src/prism_router/config.py` | env 加载：`CODEX_TIMEOUT=7200`、`CODEX_POST_MESSAGE_DELAY=1.0`、`CODEX_GRACEFUL_TIMEOUT=5`、`CODEX_FORCE_KILL_DELAY=5`、`CODEX_REQUIRE_APPROVAL=false`、`PRISM_LOG_LEVEL=info`。dataclass + 一次性读。 |
| `src/prism_router/classify.py` | `classify(parse_result, returncode, stderr_tail) → ErrorEnvelope \| None`。把 `error_events` / 退出码 / stderr 模式翻译成 §4.2 code。 |
| `src/prism_router/auth.py` | C3 pre-flight：解析 codex 当前 provider/base_url/auth route（`~/.codex/auth.json` + env），检测 token/route 不一致 → 直接返 `auth_401` envelope，不 spawn。 |
| `src/prism_router/cap.py` | `cap_envelope(text, *, limit_chars=100_000) → (text, truncated)`。UTF-8 char-count，不是 byte。 |
| `src/prism_router/server.py`（重写）| FastMCP 实例 + smoke probe + 两个 `@mcp.tool` + main 入口（stdio 默认） |

### 4.2 §6.1 P0 中本里程碑落地的项

| P0 项 | 落点 |
|---|---|
| `error.code` 三元组分类（§4.2） | `classify.py`：parser 的 `error_events` → 503 / 401 / stream_disconnected / model_refused / codex_error / gemini_error；流结束后 `session_id is None` 且无终结事件 → `no_session` `stage:"stream"`；`noise_log` 中存在 JSON-shaped 行（首尾 `{}`）且含已白名单 `type` 但反复必需字段失败 → `parse_error` `stage:"parse"`（区别于纯日志噪声）；returncode 非零无事件 → `spawn_failed` / `codex_error` / `gemini_error`；超时 → `timeout` `stage:"shutdown"` |
| Gemini session_id `{` fallback（CCG 6988f64） | M1 `parser.py` 已实现（含 codex 第 2 轮 BLOCKER 修：fallback 开闸到首事件被接受 + 多 `{` 位置遍历 + init-event 校验），M3 验证端到端 |
| **G1: gemini 事件白名单 parser** | M1 已实现，M3 验证 augment-pollution fixture 不复现 |
| **C1: 503 检测 + 分类** | `classify.py`：codex error.message 含 "503" 或 "Service Unavailable" → `proxy_503` `stage:"stream"` `retryable:true`；gemini noise_log 中含 "503"/"504" 序列 → `proxy_503` |
| **C3: auth pre-flight** | 新增 `auth.py`：spawn 前解析 codex 当前 provider / base_url / auth route（读 `~/.codex/auth.json` + `OPENAI_API_KEY` / `OPENAI_BASE_URL` env 覆盖）；发现 token 缺失或 proxy/direct route mismatch → 直接返回 `auth_401` `stage:"pre_flight"` `retryable:false`，**不**启动 CLI。stream 中后续仍出现 401 时作为兜底 `auth_401` `stage:"stream"` 分类（与 C1 平级）。两层分类不互斥：pre-flight 抢先，兜底兜底。 |
| **C7: backend smoke probe** | `server.py` 启动时 `subprocess.run([cmd, "--version"], timeout=5)`；失败则**不调用** `@mcp.tool` 注册装饰器（条件注册）；不返 envelope，让 Claude Code 看 native "no such tool" |
| 100K UTF-8 hard cap + `truncated:true` + 默认剥离 `all_messages[*]` | `cap.py` + tool 返回前调用；`return_all_messages=False` 时 `all_messages` 字段 = None |

### 4.3 Tool 签名（与现有 GuDaStudio 包**字段名兼容**）

```python
@mcp.tool
async def codex(
    PROMPT: str,
    cd: Path,
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "read-only",
    SESSION_ID: str = "",
    skip_git_repo_check: bool = True,
    return_all_messages: bool = False,
    model: str = "",
    profile: str = "",
    image: list[Path] | None = None,
    yolo: bool = False,
) -> CodexResult: ...

@mcp.tool
async def gemini(
    PROMPT: str,
    cd: Path,
    SESSION_ID: str = "",
    return_all_messages: bool = False,
    model: str = "",
    sandbox: bool = False,
) -> GeminiResult: ...
```

`CodexResult` / `GeminiResult` 即 §4.2 `CodexResult` TypedDict（gemini 复用同 shape，仅字段集合更窄）。FastMCP v3 自动从类型生成 schema。命名（大写 `PROMPT` / `SESSION_ID`、字段顺序）必须与 audit 数据中观测到的一致 —— `mcp-audit/README.md` §3 已统计。`image: list[Path] | None = None` 而非 `[]`：默认可变参数是 Python 反模式。

### 4.4 测试策略

**单测：**
- `tests/test_classify.py`：构造 `ParseResult` + stderr_tail + returncode 各组合，断言映射到正确 code/stage/retryable
- `tests/test_cap.py`：UTF-8 字符计数边界、emoji/CJK 不被截断字节中间、`truncated` 旗标
- `tests/test_config.py`：env 默认 + 覆盖

**集成：**
- 启动 server，发 `tools/list` MCP request，断言 codex/gemini 两个 tool 在场（用 `fastmcp.client.Client`）
- 故意删 codex 二进制（或 PATH 改造），断言 smoke probe 失败 → 仅 gemini tool 注册

## 5. M4：fixtures.json 回归 + .mcp.json 装载

> ✅ 已完成（2026-05-03）。实现：`tests/test_fixtures.py`（Strategy B-mocked-spawn，
> 65 通过）+ `docs/implementation/m4-cutover.md`（备份/替换/重启/回滚/观察清单）。
> 真实流量观察期是用户活动，由用户在 cutover 后自驱。

**目标：** 65 条 fixture 全部回放，每条 `original_error` 不复现；改 `.mcp.json` 让真实流量走 prism-router；1–2 周后回收新 audit。

### 5.1 fixture 回放

`tests/test_fixtures.py`（local-only，标 `@pytest.mark.fixtures`）。**仅"旧错误字符串不复现 + enum 合法"是不够的**——会让"伪成功"和"分类错"漏网。分层断言：

```python
# 1) GuDaStudio wrapper 自有的 lead-in 字符串必须消失（结构性证据：新 wrapper
#    生效），而旧错误的 backend root cause 文本可保留在 raw / stderr_tail
LEGACY_LEADINS = (
    "[json decode error]",
    "Failed to retrieve `agent_messages`",
    "Failed to get `agent_messages`",
    "Failed to get `SESSION_ID`",
)
for s in LEGACY_LEADINS:
    assert s not in (result.get("agent_messages") or "")
    if result.get("error"):
        # 在 raw / stderr_tail 内允许（那是 backend 的诊断信息）；
        # 但 envelope 顶层 / agent_messages 必须无 GuDaStudio leadins
        pass

# 2) 类别专属断言：每类 fixture 期望特定 code
EXPECTED_CODE = {
    "codex-503":                 {"proxy_503"},
    "codex-401":                 {"auth_401"},
    "codex-stream-disconnected": {"stream_disconnected"},
    "codex-cli-too-long":        {"cli_too_long"},
    "gemini-stop-self":          {"model_refused"},
    "gemini-no-session-id":      {"no_session"},
    "gemini-winerror-206":       {"cli_too_long"},
    "gemini-augment-pollution":  None,  # 期望恢复 success=True；不强制 code
    "codex-oversize":            None,  # 期望 truncated=True 或 oversize_dropped
}

# 3) MISSING 桶（original_error is None / original_raw_kind == "MISSING"）：
#    必须二选一 —— success=True 且 agent_messages 非空，或 success=False 且
#    envelope 合法。空成功（success=True 但 agent_messages == ""）算回归。
if fixture["original_raw_kind"] == "MISSING":
    if result["success"]:
        assert result["agent_messages"], "empty success is a regression"
    else:
        assert result["error"]["code"] in ALL_ERROR_CODES
        assert result["error"]["stage"] in ALL_STAGES

# 4) 旧 envelope 形状 + leadin 全序列化扫描
serialised = json.dumps(result, ensure_ascii=False)  # no default=str — 非 JSON 值即回归
for sig in LEGACY_LEADINS + _LEGACY_DICT_SIGNATURES:
    assert sig not in serialised
```

**fixture 分布**（参考性，调试时排序优先级用）：

| count | category |
|---:|---|
| 8 | codex-no-result-block |
| 8 | gemini-augment-pollution |
| 5 | codex-bare-failure |
| 5 | codex-stream-disconnected |
| 5 | codex-503 |
| 5 | gemini-no-session-id |
| 5 | gemini-no-result-block |
| 5 | multi-turn-codex-fail |
| 5 | multi-turn-gemini-fail |
| 4 | codex-oversize |
| 3 | codex-401 |
| 3 | gemini-stop-self |
| 2 | codex-cli-too-long |
| 2 | gemini-winerror-206 |

### 5.2 `.mcp.json` 替换（**用户操作位**，不是自动）

当前 `.mcp.json`（`D:\projects\prism-router\.mcp.json` 第 14–33 行）：
```json
"codex": {"type":"stdio","command":"uvx","args":["--from","git+https://github.com/GuDaStudio/codexmcp.git","codexmcp"],"env":{}},
"gemini": {"type":"stdio","command":"uvx","args":["--from","git+https://github.com/GuDaStudio/geminimcp.git","geminimcp"],"env":{}}
```

替换为单条 prism-router 注册（暴露两 tool）：
```json
"prism": {"type":"stdio","command":"uv","args":["run","--directory","D:/projects/prism-router","prism-router"],"env":{},"alwaysLoad":true}
```

或 `pip install -e .` 后直接 `"command":"prism-router"`。

**重要：** 以 `docs/implementation/m4-cutover.md` 为权威操作手册；备份要落在
**repo 外**（如 `$HOME/.prism-router.mcp.json.backup`），避免 `.mcp.json` 中的
token/路径被 `git add .` 误带入。

### 5.3 真实流量回收

- 跑 1–2 周（与 audit 时长一致）
- 用 agentsview 重做一次 `calls.json` 抽取，对比失败率：目标从 ~50% → ≤ 30–40%（v1 治理目标，§6.1）
- 把残余失败按 §4.2 code 分桶，作为 v2 优先级输入

## 6. 不要做（v1 deferred / rejected）

| 项 | 状态 | 出处 |
|---|---|---|
| 整章 §8 daemon/REST/SSE/前端 SPA/agentsview probe | v2-deferred | §6.2 |
| C2 wrapper-side stream-resume | rejected | §9 V2 |
| `stream_max_retries` 自动写 `~/.codex/config.toml` | rejected | §6.3 + §9 V5/V6 |
| `--start-agentsview` 自启 flag | rejected | §8.4 |
| MCP progress notifications | rejected | §1 |
| start/poll/cancel 三 tool 模式 | rejected | §1 |
| 实时 SSE WebUI（CCG inline 模式） | rejected | §3.4 |
| `--lite` / `--parallel` / `--progress` | rejected | §3.4 |
| 前端 iframe / API proxy 集成 agentsview | rejected | §8.5 |

## 7. 接力检查表（下一会话进来后做的第一件事）

M1–M4 已 ship。下一会话不再启动新 milestone（已锁），而是辅助 cutover + 真实流量观察期。

1. ✅ 读 `CLAUDE.md`（自动）
2. ✅ 读本文件（你正在读）
3. ✅ `git status` —— M4 commit 后应仅有用户在 cutover 期间产生的 `.mcp.json` 改动（已 `.gitignore`），
   不应再有源码或测试改动；`*.stackdump` 已 gitignore，无需关心
4. ✅ `.venv/Scripts/python -m pytest -q` —— 必须 136 通过 / 1 skip(POSIX) / 68 deselect
5. ✅ `.venv/Scripts/python -m pytest -m fixtures -q` —— 必须 65 通过 / 140 deselect
6. ✅ 复核 / 协助执行 `docs/implementation/m4-cutover.md`；真实流量观察期是用户自驱活动

不需要重读 design-rationale 全文。它是归档，不是日常参考。回看仅在 §3/§4 提到具体小节时（如 §9 V1）才打开对应锚点。

## 8. 命名 / 路径 速查

```
D:\projects\prism-router\
├── pyproject.toml
├── .gitignore
├── .mcp.json                    # 用户的；M4 改这里
├── AGENTS.md / CLAUDE.md        # 项目约束
├── docs/
│   ├── README.md                # 顶层索引（决策时间线 + 阅读顺序）
│   ├── implementation/
│   │   └── v1-plan.md           # ← 本文件 ← 接力点
│   └── mcp-audit/
│       ├── README.md            # 失败分类（1032 calls）
│       ├── design-rationale.md  # 架构决策（§1–§9）
│       ├── calls.json           # 全量数据集
│       ├── fixtures.json        # 65 条回归 fixture
│       └── samples/             # 失败模式 verbatim 转储
├── src/prism_router/
│   ├── __init__.py
│   ├── errors.py                # M1 ✅
│   ├── events.py                # M1 ✅
│   ├── parser.py                # M1 ✅
│   ├── backend.py               # M1 占位 / M2 实现
│   ├── server.py                # M1 占位 / M3 实现
│   ├── paths.py                 # M2 新增
│   ├── runner.py                # M2 新增
│   ├── backend_codex.py         # M2 新增
│   ├── backend_gemini.py        # M2 新增
│   ├── classify.py              # M3 新增
│   ├── cap.py                   # M3 新增
│   ├── auth.py                  # M3 新增（C3 pre-flight）
│   └── config.py                # M3 新增
├── tests/
│   ├── __init__.py
│   ├── test_errors.py           # M1 ✅
│   ├── test_parser.py           # M1 ✅
│   ├── test_paths.py            # M2 新增
│   ├── test_argv.py             # M2 新增
│   ├── test_runner_pure.py      # M2 新增
│   ├── test_classify.py         # M3 新增
│   ├── test_cap.py              # M3 新增
│   ├── test_config.py           # M3 新增
│   ├── test_fixtures.py         # M4 新增
│   └── integration/             # local-only，不入 CI
└── .venv/                       # uv venv，已建
```
