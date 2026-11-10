1. 在回答用户的具体问题前，**必须尽一切可能“检索”代码或文件**，即此时不以准确性、仅以全面性作为此时唯一首要考量，穷举一切可能性找到可能与用户有关的代码或文件。在这一步中，**必须使用 `mcp__fast-context__fast_context_search`**（Windsurf Devstral 驱动的 AI 语义代码搜索）获取完整、全面的项目上下文。该工具支持中英双语查询，返回 相关文件路径 + 行号范围 + 建议的 grep 关键词。

   **关键：** 不要依赖内部知识库或假设。
   1.1  **首选工具：** 任何需要理解代码上下文、探索性搜索、或自然语言定位代码的场景，必须将 `mcp__fast-context__fast_context_search` 作为**第一选择**。
   1.2  **工具分工：** 语义搜索 / 不确定位置 → `fast_context_search`；精确关键词（已知标识符跨库查全部出现）→ Grep；已知路径看内容 → Read；按文件名模式 → Glob。不要一开始就 grep/find，先用自然语言搞清 "Where / What / How"。
   1.3  **编辑前强制动作：** 在计划编辑任何文件前，必须调用 `fast_context_search` 定位涉及的符号、类或函数，确保拥有当前磁盘状态的完整上下文。尽可能在一次调用中覆盖所有相关符号。
   1.4  **参数调优：** `tree_depth=1, max_turns=1` 快速粗查（小项目/初步定位）；默认 `tree_depth=3, max_turns=3` 平衡精度与速度；`max_turns=5` 深度追踪复杂调用链；payload/size 报错时下调 `tree_depth`，结果过浅时上调。`project_path` 指定项目根目录（默认当前工作目录）。
   1.5  **迭代：** 如果检索到的上下文不足，重复搜索直到获得全貌。

2. **文件读取规则**（与 prism-router MCP Bundle Protocol 配套）：

   2.1  **bundle 内的文件不要重读：** 若 PROMPT 中出现 `## Existing files` / `## Diff` / `## New files` 三类 header，且其下用 `### <path/to/file>` 标注了具体文件，这些文件的内容已**完整**在你的 context 中。重读它们不会获得任何新信息，只会消耗 context budget 并增加 self-compaction 风险。

   2.2  **bundle 之外按需读取，但走快路径：** Windows 上频繁启动 `powershell.exe Get-Content` 通常比 `cmd /c type` 慢一个数量级（`cmd /c type` 约 30-50ms；PowerShell 冷启动常见可到数百 ms）。多文件 review 任务里 100+ 次读取可累积成明显的纯进程开销，且每次结果都会回流 context。

   - 优先使用 `cmd /c type <file>`（~30-50ms，无 PowerShell 模块加载）
   - 一次 shell 调用尽量批量化，并给每个文件加边界（`cmd /c "echo ==== a.py ==== & type a.py & echo ==== b.py ==== & type b.py"`）
   - 仅在需要 PowerShell 专属功能（pipeline、对象操作）或精确处理非 ASCII/编码问题时才用 `Get-Content`

   2.3  **短 review 默认不用 `update_plan`：** 若任务只是一次性输出 VERDICT / issues / patch，不需要规划展开。只有当 review 明确要求多阶段产出、跨多个子问题持续跟踪，或用户点名要求 TODO/plan 时，才调用 `update_plan`。