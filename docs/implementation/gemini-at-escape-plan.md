# gemini 臂 `@` 转义 —— 绕过上游 at-command 处理挂死

> **范围。** 只改 gemini 臂；codex 臂投递载荷逐字节不变（回归断言）。
> **取代。** 本文取代 2026-09-15「大载荷多轮 SESSION 分片投递」需求 —— 该方案的第一性假设已被实测证伪，见 §2。

## 1. 根因（上游 gemini-cli 客户端缺陷，与 prism 无关）

已发布包 `@google/gemini-cli/bundle/chunk-CUTHWG3H.js:66631` `parseAllAtCommands`
用下列正则整篇扫 prompt，把命中片段当作 `@<path>` 文件引用：

```js
/(?<!\\)@(?:(?:"[^"]*")|(?:\\.|[^ \t\n\r,;!?()\[\]{}.]|\.(?!$|[ \t\n\r])))+/g
//        ^^^^^^^^^^^^  第一分支 [^"]* 不排除换行
```

第一分支吃到 `"` 后会一路吞到下一个 `"`——**跨行**——再继续循环。在 package.json 式
diff（`"@scope/pkg": "workspace:*",`）里，一个 `@` 可吞掉数千字符十余行，产出一条伪
"path"；它随后进入 `resolveAtCommandPath` → `validatePath` 失败 → `tryExtractPath`
（按空白切成数百段逐段剥壳 + 正则 + 校验，并递归），外加上游自述的「每个 at-path 两次
递归 workspace glob」（PR #28581）。CPU 被打满，请求从未发出。

### 实测证据（gemini 0.59.0，Windows，裸管道 `printf … | cat - FILE | gemini -o stream-json`）

| 观测 | 结果 |
|---|---|
| node 进程 CPU（12 墙钟秒内） | 2.78s → 17.70s（+14.9 CPU-s，>1 核满载） |
| stdout | 恰好 140 B = `init` 帧，之后全静默 |
| `icon-system-crosscheck-pack.md` 255,765 B 原样 | HANG |
| 同上，全部 `@` → `A`（字节数不变） | PASS |
| 同上，全部 `@` → `\@` | **PASS**，out = 264,515 B |
| 同上，只转义唯一 1 处跨行逃逸点 | HANG（成本是累积的，非单一毒点） |
| **8,168 B** prompt（仅含一段逃逸区） | **HANG** ← 与载荷尺寸无关 |
| 255,719 B 均匀 ASCII filler（0 个 `@`） | PASS |
| 17 文件空仓当 workspace | 照样 HANG ← 与仓库规模无关 |
| 正则本身跑 87 KB 载荷 | 1 ms / 38 matches ← **不是回溯，不是 ReDoS** |

`g-half1.txt`(87,258 B, 45 个 at-path/共 677 字符) PASS；
`g-half2.txt`(87,140 B, 38 个 at-path/共 8,366 字符，最长单条 8,112) HANG。

### 上游状态（暂不补交，另行处理）

- issue **#28550**（Heap OOM in non-interactive runs，复现于 0.50.0/0.52.0）
  —— 2026-08-22 以 `not_planned` 关闭，标签含 `status/need-information`。
  其描述与本症状逐字吻合：*"runs that did not quite tip over instead consumed
  the full 1 hour job timeout producing no output at all"*。
- PR **#28581**（`fix(cli): skip diff hunk markers during @ processing`，`Fixes #28550`）
  —— 2026-08-13 关闭且 `merged_at: null`，**从未合并**。

## 2. 为什么分片方案作废

原需求假设「每轮 ≤45 KB 真实内容均安全」。**8,168 B 即可复现**，该假设不成立：任一
45 KB 分片只要含一处 `@…"` 结构就照样挂。分片把「一次挂」变成「某一轮挂」，却额外背上
多轮总预算、liveness egress 预算、重试与分片交互、N 上限回退、`AGENT_SIDE`
「前轮证据已过期」矛盾等一整套复杂度，且**不解决 bug**。

## 3. 方案

投递前对 gemini 载荷做一次字符串替换：`@` → `\@`。命中正则的 `(?<!\\)` 前瞻后，
上游整段 at-command 处理被跳过。

代价：模型看到 `\@`。`ui.escapePastedAtSymbols` **在非交互路径不生效**——
`gemini-CQYKWDIK.js:11216` 与 `:11722`（均在 `nonInteractiveCli.ts` 段内）传的是硬编码
字面量 `escapePastedAtSymbols: false`，不读 settings；实测投 `\@idle-logger/protocol`
模型原样回显反斜杠。故以一行说明兜底，已验证无损：转义版 package.json diff 提问
「列出全部 scoped 依赖名」，模型 6 个全部命中、零幻觉（并找回 grep 漏掉的 2 个）。

### 3.1 被否决的替代方案：解析感知的选择性转义

为保住调用方的 `@path` 展开，曾评估「仅转义 token 在 `cd` 下不存在的 `@`」。**不安全，已实测否决。**

决定挂不挂的是**上游正则实际吞了多远**，不是我们切出的 token 是否存在。构造
`@marker.txt": "…"`（`marker.txt` 真实存在、按该规则被放行，其余 `@` 全部转义）的
8,191 B 载荷投给 gemini：**HANG（100 s 超时，stdout 仅 140 B init 帧）**。即漏转义一处
即 bug 复发，与「修 bug 第一位」冲突。

要做到安全，必须把上游正则复刻到 wrapper 侧，逐**匹配**校验（不跨行 + 能解析 + 保留条数
封顶）——引入版本耦合，且上游一改正则就会静默劣化。成本已不属于「顺手保留」。

被牺牲的是**快捷方式而非能力**：gemini 即便不带 `-y` 也声明了 `read_file` / `glob` /
`grep_search` / `list_directory`（见 `server.py` gemini 工具 docstring），调用方要让模型读
文件，直接在 PROMPT 里要求即可；大证据本就应走 `prompt_file`。

## 4. 实现规格

**新增** `src/prism_router/at_escape.py`（纯函数，无 IO）：

```python
def escape_at(prompt: str) -> tuple[str, int]:
    """把每个 `@` 替换为 `\\@`，返回 (转义后文本, 替换计数)。"""
```

**接入点** `server.py::_invoke_gemini`，在 `prompt_file` 拼接之后、`inject_agent_side`
之前：

```python
prompt, n = escape_at(prompt) if at_escape else (prompt, 0)
if n:
    prompt = f"{AT_ESCAPE_NOTE}\n\n{prompt}"
prompt = inject_agent_side(prompt)
```

约束：

1. **gemini 专属。** `_invoke_codex` 不接入；codex 载荷保持逐字节不变。
2. **空操作保真。** `n == 0`（载荷不含 `@`）时输出与现状逐字节相同，说明行也不加。
   **不按尺寸门控** —— 8 KB 即可复现，尺寸不是变量。
3. **receipt 覆盖域不变。** 转义发生在 `load_prompt_file` 之后，`prompt_file_receipt`
   仍是**文件原始字节**的 sha256（`errors.py:122-130` 契约），无需改动。
4. **说明行不得含字面 `@`**（否则自身成为匹配起点）。用英文单词表述，ASCII 单行：
   `[prism-router] Backslash-at sequences below are transport escapes inserted by the
   wrapper; read each as a plain at-sign. They are not part of the original content.`
5. **旋钮。** `ServerConfig` 增 `gemini_at_escape: bool = True`
   （env `PRISM_GEMINI_AT_ESCAPE`），按 `gemini_require_approval` 同样方式经
   `build_server` 透传。置 0 精确恢复现状，作回退口。
6. `AGENT_SIDE` / `ORCHESTRATOR_SIDE` 实测 `@` 计数均为 0，不受影响、无需特判。
7. **可观测性走日志，不进信封。** `n > 0` 时 `_LOG.info` 记一条转义计数，便于事后判断
   「某人的 `@path` 为何不工作」。**不要放进 `diagnostics`**：`_envelope` 的
   `has_extra` 门只认 `recovered_from_rollout` / `internal_retries`，新键要么在正常成功
   时根本不出现（最该看见时看不见），要么就得放宽该门 —— 后者会让每个含 `@` 的 gemini
   调用都开始携带 diagnostics 块，改变默认信封形状（`server.py` 该处注释已专门警告）。

## 5. 测试

沿用现有风格补充：

- `tests/test_at_escape.py`（新）：计数正确；无 `@` 输入恒等返回；已含 `\@` 的输入
  变为 `\\@`（前瞻仍拦截，行为正确，需断言固定）；不改变除 `@` 外任何字节。
- `tests/test_server.py`：gemini 载荷已转义且带说明行；**codex 载荷未转义**（回归断言）；
  无 `@` 载荷与现状逐字节等价；说明行当且仅当 `n>0` 时出现；`prompt_file_receipt`
  仍等于全文件 sha256。
- 旋钮关闭时 gemini 载荷与现状逐字节等价。

按 `pytest -m ""` 跑全部 marker（默认排除 integration/fixtures）。

## 6. 验收（本机实测）

1. `.git/acf/icon-system-crosscheck-pack.md`（255,765 B）经 `mcp__prism__gemini`
   `prompt_file` **一次调用**返回成功信封，`receipt.sha256 ==
   a2ecb6c7a65b9589822946dcaed58ae0b5872a765bb3ba04b776128420b8c623`。
2. `.git/acf/g-half2.txt`（87,140 B，此前必挂）通过；`g-half1.txt` 仍通过。
3. 小载荷（无 `@`）行为与改动前逐字节一致。

验收 1 已用**磁盘上预转义的等价副本**经真实 prism 路径预跑通过（2026-09-15）。实现后应
复现同形信封，据此判断「对」长什么样：

```json
{"success":true,"SESSION_ID":"…","truncated":false,"error":null,"diagnostics":null,
 "agent_messages":"PACK_OK\n### A. primitives §2.5 (promo-prepare) rules r0 .. r5, verbatim\n94",
 "prompt_file_receipt":{"bytes":255909,"…":"…"}}
```

要点：255 KB 转义载荷全程无截断（`truncated:false`）；模型准确读出首标题，并给出**去重后**
的 `### ` 子标题数 94（文件内共 97 行），证明全量内容确实被处理而非截断或跳读。单次调用
耗时 **>120 s**，验收时别误判为挂死。
注意预跑的 receipt 是**预转义副本**的哈希（255,909 B）；实现后转义在内存中发生，receipt
必须回到**原始文件**的 `a2ecb6c7…` / 255,765 B（§4 约束 3）。

## 7. 残留风险

- **gemini 的 `@path` 展开快捷方式对 prism 调用方失效。** prism 公开契约
  （`ORCHESTRATOR_SIDE` §2/§5）从未宣传该能力，一律要求内联或用 `prompt_file`。
  失去的是快捷方式而非能力：`read_file`/`glob`/`grep_search`/`list_directory`
  在默认无 `-y` 下仍被声明，调用方可直接要求模型读取。保留该快捷方式的方案已在
  §3.1 实测否决。以旋钮 + 工具 docstring 说明兜底。
- 上游若修复，本转换变为无用但无害，置旋钮为 0 即可。
- **触发条件的边界尚未完全摸清。** `@` 是必要条件（所有实测挂死载荷在转义/替换后均转
  PASS），但**成本模型不完整**：按同一形状合成的 16,176 B 载荷产生了更大的跨行逃逸匹配
  （16,082 字符）却**通过**，而 g-half2 的 8,112 字符匹配挂死。故巨型逃逸匹配**不是充分
  条件**，无法排除存在别的挂死形状、其原因不是 `@`、因而本方案救不了。
- **因此下条的优先级高于原先判断：** 挂死发生在 `first_codex_output` 之前，gemini 臂无任何
  看门狗（`dispatcher.py:464-466` 对非 codex 硬关 `pre_terminal_idle`；且该看门狗仅在
  `first_codex_output` 里程碑上装载，init 帧之后的静默无人管），唯一天花板是 7200 s
  `overall_timeout`。既然触发边界未摸清，就更需要一个**不依赖「我们认得出触发条件」**的
  兜底 —— 另行立项，但不宜长期搁置。
- 向上游补交 issue 暂缓：目前唯一可复现的载荷均含用户私有仓库内容，而合成复现尚未成功；
  #28550 恰以 `status/need-information` 结案，补一份不可公开、无最小复现的报告大概率重蹈
  覆辙。前置条件是先造出不含私有内容的确定性复现（可从 g-half2 的逃逸段逐步替换为合成内容
  做二分）。该课题与本实施完全解耦。
