# 调用血缘（lineage）记录契约 · `v = 1`

prism 每次 `codex` / `gemini` 工具调用，往本机 jsonl 追加最多三行（`start` / `child` / `end`），
记下「哪个 Claude Code 父会话 → 哪个子会话」。读者是 agent-os（需求件 `prism-lineage-v2.1.md`），
本文件是两边的契约。实现：`src/prism_router/lineage.py`。

记录是纯旁路：MCP 信封、`agent_messages`、工具定义、progress 通知一律不变，写失败不影响调用。
**仅 Windows**：prism 的调用路径本身只在 Windows 上可用（`docs/bugs/non-windows-dispatch-unsupported.md`），
其它平台不写记录。

## 位置与开关

- 目录：`%USERPROFILE%\.prism\lineage\`，环境变量 `PRISM_LINEAGE_DIR` 可改。目录不存在则创建。
- 文件：`YYYY-MM.jsonl`，月份取**该行** `ts` 的 UTC 月份，所以同一调用的三行可能分在相邻两个月的文件里。
- `PRISM_LINEAGE`：不设置时开启；设置后只有 `1` / `true` / `yes` / `on`（不分大小写、忽略首尾空白）为开，其它值（含 `0`）为关——与 prism 其它开关用同一个解析函数 `config._bool_env`。
- 这些值在 prism 进程启动时读一次。改了之后需要重启 prism（例如 `/mcp` 重连）才生效。
- prism 从不清理这些文件，按月人工删除。

## 写入方式

- 每行一次 `WriteFile`，句柄只申请 `FILE_APPEND_DATA`、不申请 `FILE_WRITE_DATA`。微软文档保证：对本地文件，这样的句柄写入不会覆盖已有数据，且每次写都落在文件末尾（《File Access Rights Constants》《WriteFile》）；各次写之间互不交错是下表实测的结论。多个 prism 进程不加锁，共写同一个月文件。
- **不用 `os.open(O_APPEND)`**：Windows CRT 把它实现成「先 seek 到文件尾，再写」两步（UCRT `_write` 的实现；CPython 为此开的 GH-86772 至今未关），跨进程会丢行、撕行（见下表）。
- 每行是 ASCII 的 JSON（非 ASCII 字符写成 `\uXXXX`），以 `\n` 结尾，键顺序固定。单行不超过 4096 字节。
  - 实现不限制任何字段的长度。按正常输入估算——两个路径都在 MAX_PATH 260 字符以内（按最坏的纯中文算）、会话 id 为 UUID、父进程名是 prism 启动链上的常见名字（如 `claude.exe`、`python.exe`、`prism-router.exe`）——一行最多约 3.7 KB。
  - 超过 4096 字节的行只会来自上述正常输入之外的情况（例如超出 MAX_PATH 的路径、超长的非 UUID `SESSION_ID`、异常长的父进程名），按约定一律视为异常：这一行不写，记一条 WARNING 日志。
- 不读回、不改名、不重试、不 fsync。任何写失败都吞掉，只记 DEBUG 日志，调用照常进行。
- 写入在事件循环上同步执行，生产写入路径实测单行中位 0.71 ms、p99 1.19 ms（3000 次）。目录必须在本地卷上：网络盘既不保证原子追加，断连时还可能卡住 prism。

并发追加实测（Windows 10 专业版 19045.6466、NTFS、Python 3.11.3，2026-10-08）。所有写入进程都先在一道闸门前就绪，再同时放行；「交替」一栏是文件里相邻两行来自不同进程的次数，用来证明写入确实是并发的。测试套件里的
`test_two_concurrent_writers_of_1000_lines_each_lose_and_tear_nothing`、`test_killing_one_writer_tears_at_most_its_own_last_line`
用同一个写入函数、同样的闸门复现了这个结论：

| 写法 | 进程 × 行 | 行长 | 读回结果 | 交替 | 坏行 |
|---|---|---|---|---|---|
| `FILE_APPEND_DATA`（生产写入函数 `_append`） | 2 × 3000 | 228–1130 B | 6000/6000，每个进程的行都完整有序 | 5496 | 0 |
| 同上 | 4 × 3000 | 228–1130 B | 12000/12000，每个进程的行都完整有序 | 11995 | 0 |
| 同上 | 2 × 3000 | 3928–4030 B | 6000/6000，每个进程的行都完整有序 | 5406 | 0 |
| 同上，3 轮：一个进程写 3000 行；另一个进程的循环上限设为 10^7 行，文件里它的行标记（按原始字节计数）达到 300 个后被 `TerminateProcess` 杀掉 | 1 × 3000 + 1 个被杀进程 | 228–1130 B | 存活进程 3000/3000 有序；被杀进程写出的 315 / 318 / 322 行是从头开始的连续序号 | 590–601 | 0 |
| `os.open(O_APPEND)`（CRT，对照组） | 2 × 3000 | — | 5475 行，两个进程都丢行 | 4235 | 469 |
| 同上 | 4 × 3000 | — | 10082 行，四个进程都丢行 | 8175 | 1778 |

## 每次调用的三行

| `event` | 什么时候写 |
|---|---|
| `start` | 工具函数一进入就写，早于一切后端工作（prompt_file 读取、认证预检、并发排队、拉起 worker） |
| `child` | 主进程第一次得知子会话 id 的那一刻：通常是收到 worker 发来的 `thread_id` 里程碑（codex 的 `thread.started`、gemini 的 `init` 被解析时发出）；里程碑漏收时，是解析出该次派发结果帧的时候，早于之后任何可被取消的等待。每次调用最多一行 |
| `end` | 信封交还 MCP 之前写；调用异常退出时，写完 `end` 再把原异常原样抛出 |

特殊情况：

- 后端没起来：只有 `start` 和 `end`，`child_session_id` 为 null。
- prism 进程被强杀：没有 `end` 行，`child` 也可能没有。
- 宿主关闭 prism 的 stdin：在途调用不会被取消。调用会执行到后端结束，`end` 按后端的真实结果写，给宿主的响应被丢弃；宿主若在这之前强杀 prism，就没有 `end` 行。原因是 prism 依赖的 FastMCP 3.2.4 覆盖了 mcp 的 `Server.run()`，没有保留上游「传输关闭即取消在途处理器」这一步。
- 里程碑漏收：结果帧可能先于里程碑到达，此时由结果帧上报会话 id（见上表）。另外，`end` 时信封里有 `SESSION_ID` 而 `child` 行还没写，也会先补写 `child` 再写 `end`。
- gemini 内部单次重试：`child` 行记第一个观测到的子会话 id；`end` 优先取信封里非空的 `SESSION_ID`，否则取最后观测到的 id（例如第二次派发在后端起来之前就失败了）。新开会话的调用里，如果 `child` 和 `end` 的 id 不同，这两个会话都是这次调用产生的。
- 调用结束（写了 `end`）之后才到达的子会话 id 一律忽略，`child` 行永远不会出现在 `end` 之后。

## 字段

三行都带下面这些公共字段，每一行单独就能解析：

| 字段 | 类型 | 含义 |
|---|---|---|
| `v` | int | 契约版本，目前是 `1` |
| `event` | str | `start` / `child` / `end` |
| `ts` | str | 写这一行时的 UTC 时间，格式 `YYYY-MM-DDTHH:MM:SS.mmmZ` |
| `call_id` | str | 每次工具调用一个 uuid4，三行靠它配对 |
| `backend` | str | `codex` / `gemini` |
| `cwd` | str | 调用参数 `cd` 经 `paths.normalize_cwd` 处理后的结果（其中的 `os.path.normcase` 在 Windows 上转小写、把 `/` 换成 `\`） |
| `project_dir` | str \| null | prism 启动时的 `CLAUDE_PROJECT_DIR` |
| `prism_pid` | int | 写记录的进程 `os.getpid()`。在 venv 下这是真正的解释器进程，不是 `prism-router.exe` 蹦床，也不是 venv 启动器 |
| `prism_version` | str | 包版本号（目前 `0.1.0`，不随提交变化） |
| `parent_session_id_env` | str \| null | prism 启动时的 `CLAUDE_CODE_SESSION_ID`，**只是被拉起那一刻的值**：据 Claude Code 官方环境变量文档，stdio MCP 子进程保留拉起时的 id，`/clear` 之后不会更新 |
| `parent_pid` | int | 沿父进程链往上找到的第一个 `claude` / `claude.exe`（不分大小写）祖先；找不到时是直接父进程（venv 下就是 venv 启动器 `python.exe`） |
| `parent_pid_start` | str \| null | 该进程的操作系统创建时刻（`GetProcessTimes`，UTC，截断到毫秒）。读不到，或者沿途有进程的创建时刻读不到、因而无法排除 pid 复用时，为 null |
| `parent_exe` | str \| null | 该进程的进程名 |
| `requested_session_id` | str \| null | 调用方传入的 `SESSION_ID`，为空时记 null |
| `child_session_id` | str \| null | `start` 行恒为 null；`child` 行是第一个观测到的子会话 id；`end` 行是最终已知 id——优先取信封里的 `SESSION_ID`，信封为 null 时取最后观测到的 id（例如被看门狗杀掉之前已经建立的会话） |
| `resumed` | bool \| null | `child_session_id` 为 null 时为 null，否则等于 `child_session_id == requested_session_id`。新开会话为 false；codex 对未知 id 悄悄另起线程时也为 false |

`parent_*` 三个字段在 prism 启动时算一次，之后一直缓存。往上走父链时，每一跳都拿候选祖先的创建时刻，和最近一个读得到创建时刻的后代比较：祖先反而更年轻，说明原来的祖先已死、pid 被别人复用了，就在这里停止。沿途只要有一个进程的创建时刻读不到，这一检查就不完整，之后找到的祖先一律把 `parent_pid_start` 记为 null，读者据此判为「不确定」。

`end` 行额外多三个字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `outcome` | str | 见下表 |
| `error_code` | str \| null | 信封里的 `error.code`；没有信封时为 null |
| `duration_ms` | int | 从写 `start` 到写 `end` 的毫秒数 |

## `outcome` 怎么取值

| 情形 | `outcome` | `error_code` |
|---|---|---|
| 信封 `success: true` | `success` | null |
| 信封错误码是 `timeout`、`pre_terminal_idle`、`post_terminal_grace` 之一（prism 的三个计时看门狗） | `timeout` | 原错误码 |
| 其它信封错误，包括后端起来之前就失败的（如 `prompt_file_error`） | `error` | 原错误码 |
| 工具调用抛出 `CancelledError`。记录不区分来源（工具拿不到来源）；实测来源是 MCP 取消通知，宿主关闭 stdin 不会产生它（见上文「特殊情况」） | `cancelled` | null |
| prism 自身代码抛出的普通异常（没有信封） | `error` | null |
| `CancelledError` 以外、又不属于 `Exception` 的 `BaseException`，如 `KeyboardInterrupt` / `SystemExit` / `GeneratorExit`（通常是解释器正在退出，还来得及写一行；`GeneratorExit` 也可能来自协程被关闭） | `killed` | null |
| prism 进程被强杀 | 没有 `end` 行 | — |

## 版本语义

同一个 `v` 之内，只会新增字段，既有字段的含义和取值方式不变；读者应忽略不认识的字段。任何改变含义或删除字段的改动都会升 `v`。

## 读者约定

以下约定是契约的一部分，由 agent-os 实现：

- 任意位置的坏行（截断、不是 JSON、缺 `v` 或 `call_id`）都跳过并计数，不能只检查最后一行。
- 按 `call_id` 配对。三行可能跨月份文件，读的时候要把相邻两个月合并。
- 只有 `start` 没有 `end` 的调用，24 小时后记为「未完成」。有 `end`、且 `end` 行的 `child_session_id` 为 null 的，记为「后端未起」，意思是整个调用都没拿到子会话 id，不一定是后端进程没起来。判断时不能只看有没有 `child` 行：`child` 行可能因单次写失败而缺失（写失败不重试），这时 `end` 行里仍带着 id。
- 判断父会话以 `parent_pid` + `parent_pid_start` 在调用时刻对应的会话 id 为准。下列情况这条父子边都标「不确定」：对不上 `parent_session_id_env`、`parent_exe` 不是 claude、`parent_pid_start` 为 null。
- `claude agents --json` 里的 `startedAt` 在 2 个实测进程样本中比 `parent_pid_start` 晚 1.3–1.4 s，对账时不能做精确相等。这只是样本观察，不代表该字段的完整语义或偏差范围。
- 记录的单位是一次调用，不是一个子会话：同一个 `child_session_id` 可能被多次调用续用，也可能出现在不同父会话名下。

## 示例

下面三行由生产代码生成，后端是替身（子会话 id 是虚构的），是一次续用成功的调用：

```
{"v":1,"event":"start","ts":"2026-10-07T16:42:38.536Z","call_id":"1bdf31e9-f637-4854-abca-d4b2f92e5415","backend":"codex","cwd":"d:\\projects\\prism-router","project_dir":"D:\\projects\\prism-router","prism_pid":50336,"prism_version":"0.1.0","parent_session_id_env":"41e511b4-711a-4f4d-88a6-08d6073bc0f0","parent_pid":38380,"parent_pid_start":"2026-10-07T13:26:21.004Z","parent_exe":"claude.exe","requested_session_id":"019dc5a2-7f3e-7c10-9b2d-3e5f0a4c8d21","child_session_id":null,"resumed":null}
{"v":1,"event":"child","ts":"2026-10-07T16:42:38.937Z","call_id":"1bdf31e9-f637-4854-abca-d4b2f92e5415","backend":"codex","cwd":"d:\\projects\\prism-router","project_dir":"D:\\projects\\prism-router","prism_pid":50336,"prism_version":"0.1.0","parent_session_id_env":"41e511b4-711a-4f4d-88a6-08d6073bc0f0","parent_pid":38380,"parent_pid_start":"2026-10-07T13:26:21.004Z","parent_exe":"claude.exe","requested_session_id":"019dc5a2-7f3e-7c10-9b2d-3e5f0a4c8d21","child_session_id":"019dc5a2-7f3e-7c10-9b2d-3e5f0a4c8d21","resumed":true}
{"v":1,"event":"end","ts":"2026-10-07T16:42:39.540Z","call_id":"1bdf31e9-f637-4854-abca-d4b2f92e5415","backend":"codex","cwd":"d:\\projects\\prism-router","project_dir":"D:\\projects\\prism-router","prism_pid":50336,"prism_version":"0.1.0","parent_session_id_env":"41e511b4-711a-4f4d-88a6-08d6073bc0f0","parent_pid":38380,"parent_pid_start":"2026-10-07T13:26:21.004Z","parent_exe":"claude.exe","requested_session_id":"019dc5a2-7f3e-7c10-9b2d-3e5f0a4c8d21","child_session_id":"019dc5a2-7f3e-7c10-9b2d-3e5f0a4c8d21","resumed":true,"outcome":"success","error_code":null,"duration_ms":1000}
```
