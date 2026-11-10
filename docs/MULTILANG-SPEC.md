# 多语言 Context / Dependency 规格（SDD）

## 1. 目标

让 review agent 在**多语言混合仓库**里：

1. 把改动文件的语言识别出来，并作为检索过滤条件；
2. 对支持的语言注入跨文件 context（语义检索）；
3. 对支持的语言注入依赖影响（Python import、JS/TS import、C/C++ `#include`）；
4. 在 prompt 里明确列出本次改动涉及的语言；
5. 用可复现的 P/R/F1 评测证明每一阶段没有退化。

## 2. 非目标

- 不做完整编译/类型检查；不替代 clang/tsc/go build。
- 不解析跨语言 API 调用（TS → Python HTTP、C++ → Python binding、protobuf/gRPC）。
- 不追求 100% 精确的 C/C++ 预处理；宏/条件编译/生成头文件按启发式处理。
- 不把外部包（node_modules、vendor、third_party、系统头）建成图。

## 3. 阶段与验收

### Phase 1：语言注册表 + C/C++ 索引 + 语言过滤

范围：

- 新增 `context/languages.py`：扩展名 → 语言、`languages_for_paths()`、`render_language_section()`。
- `context/chunker.py` 支持 C/C++ 扩展名（window chunk）：
  `.c .h .cc .cpp .cxx .hpp .hh .hxx`。
- `ContextRetriever` 接受 `languages`，优先检索同语言 chunk；同语言不足时回退全语言。
- `pipeline._build_context` 注入 `CHANGED LANGUAGES` 摘要，并把改动语言传给检索。
- `pipeline._build_context` 的 `dependency_ready` 不再只判断 `.py`，改用语言注册表（Phase 2 接依赖实现）。

验收：

- 单元测试覆盖语言映射、C/C++ 分块、语言过滤、回退、语言摘要。
- 全量 pytest 通过。
- 检索评测：多语言 fixtures 上 file-level recall 不低于改动前；precision 不下降。
- 日志新增 `languages` 字段，能看到本次改动语言。

### Phase 2：依赖 dispatcher（Python / C/C++ / JS / TS）

范围：

- `review/dependency.py` 变为 dispatcher：
  - Python：保留现有 AST import 图；
  - C/C++：解析 `#include "..."`，相对当前文件 + 仓库根解析，构建 includes / included_by；
  - JS/TS：解析 `import ... from "..."`、`export ... from "..."`、`require("...")`、动态 `import("...")`，只处理相对路径，解析 `.ts/.tsx/.js/.jsx` 和 `/index.*`；
  - 每种语言一个 section，混合 PR 可同时注入多节。
- 每个改动文件向上找最近的 workspace 根（`pyproject.toml`、`package.json`、`CMakeLists.txt`），用于本地解析；外部包忽略。
- `render_changed_dependency_section` 保持现有签名，内部按语言分发。

验收：

- C/C++：本地头文件反向依赖、相对 include、系统 `<...>` 忽略。
- JS/TS：相对 import/require、index 解析、裸包名忽略。
- 混合 PR：Python + C++ + TS 同时出现时三节都注入。
- 全量 pytest 通过。
- 依赖图评测：fixtures 上 file-level precision/recall/F1 达到 0.9+；报告数字。

### Phase 3：Go / Java / Rust + build metadata

范围：

- Go `import` 块 + `go.mod` module path；
- Java/Kotlin `import` + package/source root；
- Rust `mod`/`use crate::`；
- 可选读取 `compile_commands.json` / `tsconfig.json` / `go.mod` 的路径信息。

验收：

- 每种语言至少 3 个 fixture；依赖图 P/R/F1 报告；
- 没有 build metadata 时明确降级为“仅相对路径解析”。

## 4. TDD 流程

每个 Phase 按同一顺序：

1. 写失败测试（先不实现）。
2. 跑测试确认失败原因正确。
3. 最小实现让测试通过。
4. 跑全量 `python -I -X utf8 -m pytest -q`。
5. 跑检索评测，记录 JSON + Markdown。
6. 再进入下一 Phase。

测试文件：

- `tests/test_languages.py`（Phase 1）
- `tests/test_context.py`（Phase 1 检索过滤 / C/C++ 分块）
- `tests/test_dependency.py`（Phase 2 C/C++ / JS / dispatcher）
- `tests/test_pipeline.py`（语言摘要注入）

## 5. 评测

### 5.1 检索 P/R/F1（确定性，不调用 LLM）

fixtures：每个 PR 给出：

- 改动文件列表；
- 期望相关文件集合 `E`（人工标注的“应该被检索到”的文件）；
- 仓库快照。

对 top-k context 的 chunk 取 `file_path` 去重得到 `R`：

- Precision = |R ∩ E| / |R|
- Recall = |R ∩ E| / |E|
- F1 = 2PR / (P + R)

报告 macro 平均 + 每个 fixture。该评测可重复、无 LLM 方差。

### 5.2 Review P/R/F1（LLM，现有 `scripts/evaluate.py`）

多语言数据集：

- 每个语言至少 2 个 PR；
- expected issues 带 `issue` + 可选 `file/line`；
- 跑 `--runs 3 --match keyword`，报告 mean ± std；
- `--match hybrid` 只作为附加，不替代 keyword。

指标沿用 `evaluation/metrics.py`：

- Issue P/R/F1（贪心一对一）
- Line P/R/F1
- Actionable rate

### 5.3 验收口径

- Phase 1/2 先保证**不退化**：keyword F1 不低于改动前基线；
- 依赖图准确率/召回率在 fixture 上报告绝对值，不以单次 LLM 运行宣称线上收益；
- 任何“提升”必须同时给 keyword-only 和样本量。

## 6. 风险

- C/C++ 宏/条件编译导致 include 图不完整；报告“启发式”限制。
- JS/TS 路径别名（`@/`）没有 tsconfig 时无法解析；Phase 2 只做相对路径。
- 多语言检索可能引入跨语言噪声；用语言过滤 + 回退控制，不保证零噪声。
- 多语言 prompt 变大；需要 per-language char budget，Phase 2 再加。
- 评测 fixtures 是合成/小样本，不能当线上 SLA。

## 7. 当前状态（2026-11-10）

- Phase 1：已实现。语言注册表、C/C++ window chunks、语言族检索
  （C/C++、JS/TS）、`CHANGED LANGUAGES` 注入；新增
  `tests/test_languages.py` 并扩展 `test_context.py` / `test_pipeline.py`。
- Phase 2：已实现。Python / C/C++ `#include` / JS/TS relative import
  dispatcher；nearest workspace root；扩展 `tests/test_dependency.py`。
- 全量测试：**150 passed**。
- 确定性依赖图评测：7 cases / 15 edges，P/R/F1 **1.0000**（合成 fixture）。
- 确定性检索评测（lexical-hash proxy，top-6）：language-filtered
  P 0.3846 / R 1.0000 / F1 0.5556；unfiltered P 0.3590 / R 0.9333 /
  F1 0.5185。
- Review-level LLM（7 个合成 PR，security，3 runs，keyword）：
  issue P 0.9697 / R 1.0000 / F1 0.9841；line F1 1.0000；
  actionable 0.8810；errors 0。
- 基线（context/dependency 关闭）：issue P/R/F1 相同（0.9697 / 1.0000 /
  0.9841）；line F1 0.8515；actionable 0.8571。这个合成集上 issue-level
  已经到顶，测到的收益主要是行级定位/可评论率（line F1 +14.85pp，
  actionable +2.39pp）。
- Phase 3：Go/Java/Rust 和 build metadata 仍未实现。
