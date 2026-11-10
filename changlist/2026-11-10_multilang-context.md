# 2026-11-10 修改记录

- 项目：`_labs/code_review`（codereview-agent）
- 主题：多语言 Context / Dependency（Phase 1 + Phase 2）
- 分支：`feat/multilang-context`
- 结果：150 个离线测试通过；依赖图 edge-level P/R/F1 = 1.0；review-level
  issue F1 = 0.9841（合成集，3 runs）
- 说明：本目录不是 git 仓库；代码提交在 `huajielong/codereview-agent` 的
  `feat/multilang-context` PR。

## 摘要

1. 新增语言注册表，统一 chunker / retriever / dependency 的语言识别。
2. chunker 支持 C/C++ 扩展名；非 Python 仍用 window chunk。
3. 检索支持语言族过滤：C/C++ 一组，JS/TS 一组；混合 PR 每个语言族预留 slot，
   不足时回退全语言。
4. `CHANGED LANGUAGES` 注入 prompt。
5. dependency 从 Python-only 变成 dispatcher：
   Python import、C/C++ `#include "..."`、JS/TS relative import/require。
6. 新增 `find_workspace_root`，按最近的 `pyproject.toml` / `package.json` /
   `CMakeLists.txt` 等定位本地 workspace。
7. 新增 SDD 规格、确定性依赖/检索评测、多语言 review 数据集与报告。

## 1. Phase 1：语言注册表 + C/C++ 索引 + 语言过滤

**新增/修改**

- `src/codereview/context/languages.py`（新增）
  - `EXTENSION_LANGUAGES`、`language_for`、`languages_for_paths`
  - `render_language_section`：`CHANGED LANGUAGES: python (2 files), ...`
  - `LANGUAGE_GROUPS` / `expand_languages` / `retrieval_groups`：
    C/C++ 与 JS/TS 作为检索族
- `src/codereview/context/chunker.py`
  - 支持 `.c .h .cc .cpp .cxx .hpp .hh .hxx`
  - 从 `languages.py` 取 `SUPPORTED_EXTENSIONS` / `language_for`
- `src/codereview/context/store.py`
  - `MemoryVectorStore.query` / `ChromaVectorStore.query` 增加 `languages`
  - Chroma 优先用 metadata `where`，失败时回退并 post-filter
- `src/codereview/context/retriever.py`
  - `retrieve(..., languages=...)`
  - 语言族过滤；混合 PR 每族预留 slot；不足时回退全语言
- `src/codereview/review/pipeline.py`
  - `languages_for_paths(paths)` + `render_language_section(paths)`
  - 检索传入 `languages`
  - `dependency_ready` 改用语言注册表
  - `review_context_built` 日志增加 `languages`

**测试**

- `tests/test_languages.py`（新增）
- `tests/test_context.py`：C/C++ 索引/分块、语言过滤、回退
- `tests/test_pipeline.py`：`CHANGED LANGUAGES` 注入

## 2. Phase 2：依赖 dispatcher

**新增/修改**

- `src/codereview/review/dependency.py`
  - `render_changed_dependency_section` 变为 dispatcher
  - Python：原 `index_repo` / `build_import_map` / `dependents_of`
  - C/C++：`index_cpp_includes`、`render_cpp_dependency_section`
    - 只解析 `#include "..."`，忽略系统 `<...>`
    - 相对当前文件、workspace root、`include/`/`src/`/`inc/` 前缀解析
  - JS/TS：`index_js_imports`、`render_js_dependency_section`
    - `import` / `export ... from` / `require` / 动态 `import()`
    - 只处理相对路径；忽略裸包名
    - 解析 `.ts/.tsx/.js/.jsx/.mjs/.cjs` 和 `/index.*`
  - `find_workspace_root`：最近的 build/package manifest
  - `DEPENDENCY_LANGUAGES`：python / c / cpp / javascript / typescript
- `src/codereview/review/pipeline.py`
  - `dependency_ready` 支持上述语言
  - 混合 PR 会同时注入多语言 section

**测试**

- `tests/test_dependency.py`：C/C++ 双向 include、JS/TS 双向 import、
  mixed dispatcher、workspace root
- `tests/test_pipeline.py`：C++ + TS 混合 PR 的依赖注入

## 3. 测试结果

```text
python -I -X utf8 -m pytest -q
150 passed, 1 warning
```

## 4. 评测

### 4.1 依赖图 edge-level（确定性）

`scripts/evaluate_multilang.py`、`eval/reports/multilang_dependency.md`：

- 7 cases / 15 edges
- **Precision 1.0000 / Recall 1.0000 / F1 1.0000**
- 合成 fixture；不是真实仓库准确率

### 4.2 检索 file-level（lexical-hash proxy，top-6）

同一报告：

| 模式 | Precision | Recall | F1 |
|---|---|---|---|
| language-filtered | 0.3846 | 1.0000 | 0.5556 |
| unfiltered | 0.3590 | 0.9333 | 0.5185 |

precision 低是因为 fixture 只有 11 个文件、expected 只有 1–5 个；该指标只用于
比较 filtered/unfiltered，不是线上准确率。k=3 时 filtered F1 0.6471、
unfiltered 0.7059，说明指标对 k 敏感。

### 4.3 Review-level（7 个合成 PR，3 runs，keyword，deepseek-flash）

| 配置 | Issue P | Issue R | Issue F1 | Line F1 | Actionable |
|---|---|---|---|---|---|
| context + dependency | 0.9697 ± 0.0429 | 1.0000 | 0.9841 ± 0.0224 | 1.0000 | 0.8810 ± 0.0337 |
| baseline（两者关闭） | 0.9697 ± 0.0429 | 1.0000 | 0.9841 ± 0.0224 | 0.8515 ± 0.0275 | 0.8571 ± 0.0673 |

结论：合成集上 issue-level 已到顶；测到的收益主要是行级定位/可评论率
（line F1 +14.85pp，actionable +2.39pp）。一次 run 在 `multilang#5`（C++ buffer）
多报一条，导致 precision 0.9697。报告：

- `eval/reports/multilang_review.md`
- `eval/reports/multilang_review_baseline.md`
- `eval/reports/multilang_review_1run.md`（单次对照）

JSON 报告由评测脚本本地生成，已加入 `.gitignore`；仓库只提交 `.md` 摘要。

### 4.4 数据集

- `eval/datasets/multilang.jsonl`：7 个 PR（Python / TS / C++ / mixed）
- `eval/workspaces/multilang/`：对应 workspace
- `scripts/seed_multilang_eval.py`：可重生成
- `scripts/evaluate_multilang.py`：依赖/检索确定性评测

## 5. 文档

- `docs/MULTILANG-SPEC.md`（新增）：SDD 规格、阶段、TDD 流程、评测口径、风险
- `README.md`：架构图、dependency 支持范围、已知边界
- `CLAUDE.md`：pipeline/依赖说明、边界、测试计数 150

## 6. 限制

- C/C++ 只解析本地 `#include "..."`；不处理宏、条件编译、生成头文件。
- JS/TS 只解析相对 import/require；不处理 `tsconfig` 路径别名。
- 跨语言 API / protobuf / gRPC 依赖不在范围内。
- Go/Java/Rust 尚未接入（Phase 3）。
- 依赖图 1.0、检索/Review 指标都是小样本合成 fixture，不能当线上 SLA。
- 检索 precision 低是 fixture 规模造成的；不要单独引用这个数字。

## 7. 下一步

1. Phase 3：Go/Java/Rust + build metadata。
2. 跨语言 API 依赖（OpenAPI / protobuf / gRPC）单独设计。
3. 扩大真实多语言 PR 数据集，做 blinded 标注后再报告 P/R/F1。
