# 2026-11-09 修改记录

- 项目：`_labs/code_review`（codereview-agent）
- 主题：开启 AI review 全量能力，接线 dependency/history，修复 fail-loud 与 reasoning 长度失败
- 结果：136 个离线测试通过；GitHub Actions 真实运行；agent PR #1 与 workflow PR #4 已合并
- 说明：本目录不是 git 仓库；本清单按文件、运行记录和 PR 证据整理。代码提交历史见
  `huajielong/codereview-agent`，workflow 提交历史见 `huajielong/local-ai-agent-setup`。

## 摘要

1. `REVIEW_DEPENDENCY_ENABLED` 从“只有开关”变成真实注入：索引单仓库 Python import 图，
   在 prompt 里给出“改动文件 import 了谁 / 谁 import 了改动文件”。
2. `REVIEW_HISTORY_ENABLED` 补上写入端：发布成功后把 findings 落 `history.jsonl`，
   后续 PR 关键词检索并注入；按 `(repo, pr, file, line, issue)` 去重，最多 2000 条。
3. Actions 开启 `ensemble`、草稿 PR、空结果摘要、context、dependency、history；
   `REVIEW_POST_ON_PARTIAL` 保持 `false`。
4. 新增 `review_exit_code()`：`--post` 模式未发布时非零退出，堵住“check 绿但没 review”。
5. reasoning 模型 length 重试从 2x/32K 提升到 **4x / cap 64K**，Actions 配
   `OPENAI_TIMEOUT=300`，job timeout 40 分钟。
6. embedding/review/chunk 缓存 + Actions cache/artifact + `workflow_run` 伴生
   workflow，解决 PR cache 不跨 PR 可见的问题。
7. docs-only 检测放进 job 内部（不用 `paths-ignore`），required check 始终报告；
   per-PR concurrency 取消旧 run。
8. 补齐测试、文档和 `eval/reports/` 证据；`CLAUDE.md` 扩写为完整开发指南。

## 1. 评审能力

### 1.1 依赖图接线

**文件**

- `src/codereview/review/dependency.py`
- `src/codereview/review/pipeline.py`
- `tests/test_dependency.py`（新增，8 个用例）

**改动**

- `index_repo` 现在同时收集完整 import 名和顶层 import 名，并处理相对 import；
  新增 `_absolute_import_base` 解析 `from .x import y` 的基准包。
- 新增 `module_candidates`、`build_import_map`、`_resolve_module`、`dependents_of`。
- 新增 `render_changed_dependency_section`：输出
  `IMPACT CONTEXT (repository import relationships for changed files)` 段落，
  列出每个改动文件 imports / imported by。
- `pipeline._build_context` 在 `REVIEW_DEPENDENCY_ENABLED=true` 且改动路径包含
  `.py` 时调用；非 Python 仓库不会产生这一节。
- 工作区下载条件从“仅 embeddings”改为“embeddings **或** dependency”，两者共用
  同一份 tarball 工作区。

**验证**

- Probe PR #5 第一次运行 `dependency_chars=393`；第二次仍为 `393`，说明依赖节稳定注入。
- 证据：`eval/reports/github_actions_dependency_history.json`。

### 1.2 历史 finding

**文件**

- `src/codereview/review/history.py`
- `src/codereview/review/pipeline.py`
- `tests/test_history.py`（新增，5 个用例）

**改动**

- `HistoryStore` 新增 `_record_key`（repo/pr/file/line/issue 去重）、`add_many`、
  原子写入（临时文件 + replace）、`threading.Lock`、`max_records=2000`。
- `render_history_section` 明确要求“逐条对照当前 diff，不要重复已经不存在的问题”。
- `pipeline._record_history` 在以下条件同时满足时写入：
  `history_enabled`、非 dry-run、PR 号 > 0、`outcome.status == "completed"`。
- 记录字段：repo/pr/head_sha/mode/issue/file/line/severity/source_agent/created_at。

**验证**

- Probe PR #5 第一次 `history_chars=0`、`history_recorded=3`；
  第二次 `history_chars=477`、`history_recorded=4`。
- 证据：`eval/reports/github_actions_dependency_history.json`。

### 1.3 Context 构建

**文件**

- `src/codereview/review/pipeline.py`

**改动**

- `_build_context` 统一组装四类上下文：检索到的代码、依赖图、policy、历史 finding；
  每节独立 try/except，单节失败只追加 error，不阻断 review。
- 新增 `review_context_built` 日志，字段包括：
  `sections`、`context_chars`、`dependency_chars`、`history_chars`、`workspace_ready`。
- 索引/检索仍排除本次改动文件，避免把 PR 自己的代码当“既有模式”。
- 只有同时需要 embeddings/dependency 时才下载工作区；否则不拉 tarball。

### 1.4 Ensemble / 草稿 / 空摘要

**文件**

- `docs/github-actions-codereview.yml`

**改动**

- `REVIEW_MODE=ensemble`：security + pattern 两个 agent。
- `REVIEW_DRAFTS=true`：job 不再过滤 `draft == false`；触发类型包含
  `ready_for_review`。
- `REVIEW_POST_EMPTY_SUMMARY=true`：0 finding 也发一条
  “No security issues found in the changed lines.”，保证 required check 有可见结果。
- `REVIEW_POST_ON_PARTIAL=false`：任一 chunk/agent 失败，整次不发布。
- 移除 CLI 的 `--mode security`，模式统一由环境变量控制。

## 2. 失败语义与模型可靠性

### 2.1 `review_exit_code()`

**文件**

- `src/codereview/cli.py`
- `scripts/run_review.py`
- `tests/test_cli.py`（新增，5 个用例）

**改动**

- `failed` → 1。
- `--post` 且 `completed_with_errors` 且 `posted=false` → 2。
- 其他 → 0。
- `scripts/run_review.py` 现在也调用 `review_exit_code(outcome, post=args.post)`，
  不再只区分 `failed`。

**背景**

真实事故：一次 ensemble 运行中 pattern agent 返回 `finish_reason=length`，
pipeline fail closed 未发布，旧 CLI 仍返回 0，导致 required check 绿着却没有 review。
`review_exit_code()` 让这种情况变成红色 check。

### 2.2 Reasoning length 重试

**文件**

- `src/codereview/llm/client.py`
- `docs/github-actions-codereview.yml`

**改动**

- 当 `content` 为空且 `finish_reason=length` 时，预算从 2x/32K 提升到
  `min(65536, max(base+1, base*4))`，即 **4x、cap 64K**。
- Actions 设置 `OPENAI_MAX_TOKENS=16384`、`OPENAI_TIMEOUT=300`；
  job `timeout-minutes: 40`。
- 重试仍计入 token 统计；失败仍 fail closed，不自动降级到另一个 agent。

**验证**

- Probe PR #6（run `42631353930`）：2x/32K 失败；4x/64K + 300s 成功发布
  review `5827083959`，耗时 `428,400 ms`。
- 证据：`eval/reports/github_actions_length_retry.json`。

## 3. 缓存与并发

### 3.1 缓存层

**文件**

- `src/codereview/config.py`（`ENGINE_VERSION` + fingerprint）
- `src/codereview/context/embedding_cache.py`（沿用）
- `src/codereview/context/index.py`（沿用）
- `src/codereview/review/pipeline.py`（review cache key）

**改动/确认**

- **Review cache** key 覆盖：
  `repo/pr/head_sha/mode/config fingerprint/prompt fingerprint/policy fingerprint`。
- **Embedding cache** 按 `model + input_type + text hash` 存 SQLite（float32 BLOB）。
- **Chunk cache** 按 `repo + path + blob_sha + CHUNKER_VERSION` 缓存 AST 分块。
- **Repo index fast path**：`repo_index_state(repo, ref)` 命中且 Chroma collection
  存在时跳过建索引；否则从缓存向量重建 Chroma。
- `ENGINE_VERSION = "2026-11-09.1"` 加入 `Settings.fingerprint()`；行为变更会使旧
  review cache 失效。

### 3.2 GitHub Actions 缓存

**文件**

- `docs/github-actions-codereview.yml`
- `docs/github-actions-codereview-state.yml`（新增）

**改动**

- PR job 用 `actions/cache/restore` / `actions/cache/save` 保存
  `data/codereview.db*`、`data/history.jsonl`。
- 每次 run 上传同名 artifact；`codereview-state.yml` 在默认分支用
  `workflow_run` 下载并校验后，保存 main-scoped cache。
- PR cache 只对当前 PR 可见，兄弟 PR 看不到；main-scoped cache 才能跨 PR 复用。
- state workflow 只接受同仓库 PR（`head_repository.full_name == github.repository`），
  校验 SQLite 表 `embeddings` / `chunk_cache` / `posted_reviews` 和每行 history JSON。
- state workflow 使用 `concurrency: group: codereview-state, cancel-in-progress: false`
  串行保存；仍是 last-writer-wins（已写入文档）。

**验证**

- PR #4 run `42625939925`：6 files / 16 chunks，**5 个文件复用**，只新 embed 2 个 chunk
  （435 ms）；artifact 237,160 bytes。
- Probe PR #5 第二次：9 files / 23 chunks，**8/9 文件复用**，只新 embed 1 个 chunk。

### 3.3 并发

- PR job：`concurrency.group = codereview-pr-<PR number>`，
  `cancel-in-progress: true`；同一 PR 连续 push 取消旧 run。
- state workflow：独立 group，`cancel-in-progress: false`，避免写一半被砍。

## 4. GitHub Actions 与治理

### 4.1 `codereview.yml`

**文件**

- `docs/github-actions-codereview.yml`

**关键配置**

| 配置 | 值 |
|---|---|
| `REVIEW_MODE` | `ensemble` |
| `REVIEW_DRAFTS` | `true` |
| `REVIEW_CONTEXT_ENABLED` | `true` |
| `REVIEW_DEPENDENCY_ENABLED` | `true` |
| `REVIEW_HISTORY_ENABLED` | `true` |
| `REVIEW_POST_EMPTY_SUMMARY` | `true` |
| `REVIEW_POST_ON_PARTIAL` | `false` |
| `OPENAI_MAX_TOKENS` | `16384` |
| `OPENAI_TIMEOUT` | `300` |
| `LOG_LEVEL` | `INFO` |
| job timeout | `40` 分钟 |
| concurrency | per-PR，cancel-in-progress |
| cache | `data/codereview.db*`、`data/history.jsonl` |
| artifact | `codereview-state-<run_id>-<attempt>`，保留 3 天 |

- docs-only 检测在 job 内完成；只跳过 LLM 步骤，required check 仍然成功。
- `gh api` 失败时 **fail open**，跑全量 review，不因检测失败跳过。
- 只把 `.md/.mdx/.rst` 和图片当文档；`docs/build.sh` 等代码文件不会被误判。
- `push` 到 `main` 的直推仍跑 `push-audit`；merge commit 跳过。

### 4.2 `codereview-state.yml`

**文件**

- `docs/github-actions-codereview-state.yml`（新增）

**作用**

- `workflow_run` 监听 `AI code review` 完成。
- 只在 `workflow_run.event == pull_request`、conclusion success、同仓库 PR 时运行。
- 下载 PR run 的 state artifact；校验 SQLite 表和 history JSON；保存
  `codereview-state-<repo>-review-<run_id>-<attempt>` 的 main-scoped cache。
- 只有合并到默认分支后才生效；首次生效在下一次成功 PR review 之后。

### 4.3 分支保护

**证据**

- `eval/reports/github_actions_branch_protection.json`

**配置**

- 必须 PR；required status check `review`，strict/up-to-date。
- required conversation resolution：AI 行级评论必须逐条 resolve。
- `enforce_admins: true`；禁止 force push / 删除。
- required approvals 0（个人仓库，人工点 merge）；有第二人时可改 1。

## 5. 配置 / CLI / 脚本

### 5.1 `ENGINE_VERSION`

- `src/codereview/config.py` 新增 `ENGINE_VERSION = "2026-11-09.1"`。
- 参与 `Settings.fingerprint()`；修改评审行为后必须 bump，否则旧 review cache 继续命中。

### 5.2 结构化日志

- `src/codereview/cli.py`：只有设置了 `LOG_LEVEL` 才启用结构化日志；
  默认保持 stdout 为单个 JSON，方便脚本解析。
- Actions 设置 `LOG_LEVEL=INFO`，因此能看到 `review_context_built`、
  `history_recorded`、`llm_length_retry` 等事件。

### 5.3 `run_review.py`

- `scripts/run_review.py` 改为调用 `review_exit_code()`，与 `codereview.cli` 一致。
- `--post` 模式未发布（`completed_with_errors` + `posted=false`）退出 2，
  可作为本地 fail-loud gate。

### 5.4 `CLAUDE.md`

- 从 53 行扩写为 227 行的开发指南：
  常用命令、单测试、pipeline 顺序、模块职责、SQLite 表/缓存层、失败语义、
  GitHub Actions 双 workflow、评测链路、必须注意的 14 条边界。
- 修正旧文档里“长度重试只加倍一次”的描述为 4x/64K；
  补充 `review_exit_code`、`ENGINE_VERSION`、`build/lib` 是旧产物等注意事项。

## 6. 测试

**新增/修改**

- `tests/test_dependency.py`：8 个用例，覆盖 import 索引、相对 import、
  `dependents_of`、prompt 注入、非 Python 跳过。
- `tests/test_history.py`：5 个用例，覆盖去重、上限、原子写、检索、渲染。
- `tests/test_cli.py`：5 个用例，覆盖 `review_exit_code` 的 1/2/0 分支。
- `tests/test_pipeline.py`：新增依赖注入/关闭、草稿、history 记录/干跑/注入、
  空摘要等用例。
- `tests/test_llm_client.py`：length 重试期望从 2x 改为 4x。
- `tests/test_config.py`：engine version 参与 fingerprint。

**结果**

```text
python -I -X utf8 -m pytest -q
136 passed, 1 warning
```

单文件/单测试命令已在 `CLAUDE.md` 写明并验证。

## 7. 文档与证据

**文档更新**

- `README.md`：新增 dependency/history/ensemble/state workflow/fail-loud 说明。
- `docs/GITHUB-ACTIONS.md`：新增全量开关、缓存、state workflow、docs-only、分支保护、`OPENAI_TIMEOUT=300`。
- `docs/PRODUCTION.md`：补充失败语义与 4x/64K 重试。
- `docs/VALIDATION.md`：新增第 8 节，记录 PR #4、Probe #5/#6 的真实运行数据。
- `CLAUDE.md`：扩写为完整开发指南。

**新增/更新证据**

- `eval/reports/github_actions_enable_all.json`：PR #4 三次运行、可靠性缺口、probe #6。
- `eval/reports/github_actions_dependency_history.json`：Probe #5 的 dependency/history 数据。
- `eval/reports/github_actions_length_retry.json`：Probe #6 的 4x/64K 数据。
- `eval/reports/github_actions_context_pr.json`：PR #3 的 context 运行。
- `eval/reports/github_actions_branch_protection.json`：分支保护配置。
- 早期今天的 `eval/reports/real_*`、`github_actions_smoke.json` 等保留。

## 8. 运行与合并记录

### 8.1 关键 run

| 对象 | run | 结果 |
|---|---|---|
| PR #2（trigger test） | `42595184572` | success；review `5824049232`，2 条 inline |
| PR #3（context） | `42618504088` | success；5 files / 14 chunks；2 条 medium |
| PR #4（enable-all 首跑） | `42623715991` | ensemble 2 calls；24,802 prompt / 66,268 completion tokens；446,016 ms；review `5826443972` |
| PR #4（第二次） | `42625939925` | 6 files / 16 chunks；5 文件复用、2 新 embed；review `5826623406` |
| PR #4（length 失败） | `42627781030` | `completed_with_errors`，`posted=false`；旧 CLI 仍返回 0 |
| Probe #5（dependency/history） | `42626525096` / `42626908411` | dependency 393；history 0 → 477；8/9 文件复用 |
| Probe #6（length retry） | `42631353930` | 4x/64K + 300s；review `5827083959` |
| PR #4（timeout only） | `42632854731` | check pass 6m31s；仍 `completed_with_errors`、未发布（旧 agent 2x/32K） |

### 8.2 合并状态

- `huajielong/codereview-agent#1`：已合并，merge commit `2b61c2e`（2026-11-09T14:34:33Z）。
- `huajielong/local-ai-agent-setup#4`：已合并，merge commit `84470f3`（2026-11-09T14:43:54Z）。
- 合并后 `codereview-state.yml` 已在 `main` 生效。

### 8.3 遗留事实

PR #4 最后 head `852d841` 的 `review` check 是 **绿但未审**：它跑的还是旧 agent
（2x/32K），pattern agent 返回 `finish_reason=length`，pipeline fail closed 未发布；
旧 CLI 返回 0，所以 check 没有变红。agent PR 合并后新代码才在 main 生效，
所以还没有一次“main + 新 agent + 真 PR”的完整运行；下一个 PR 是首次验证。

## 9. 兼容性 / 迁移注意

1. **`ENGINE_VERSION` 变更会失效旧 review cache**：这是预期行为；不要为了复用缓存回退版本。
2. **退出码新增 2**：本地脚本/CI 如果只判断 0/1，需要把 2 当失败处理。
3. **`OPENAI_TIMEOUT=300`**：不要调回 120s；64K 重试可能耗时数分钟。
4. **state workflow 只在默认分支生效**：合并后第一次成功 PR review 才会写 main-scoped cache。
5. **Actions 跑 agent 仓库默认分支**：本地 `_labs/code_review` 改完不会自动上线；
   需要推 `huajielong/codereview-agent`。
6. **`REVIEW_POST_ON_PARTIAL=false`**：需要接受部分结果时才改 true；默认 fail closed。
7. **`scripts/run_review.py` 现在可能退出 2**：本地自动化要按新语义处理。

## 10. 已知边界 / 后续

- dependency 只支持单仓库 Python import 图；L6 的跨仓库 module→repo 图仍未接入。
- history 是关键词重叠检索，从空开始，最多 2000 条；可能把模型引向旧问题，暂无评测证明不会增加误报。
- state cache 是 best-effort、last-writer-wins；并发 PR 可能互相覆盖。
- ensemble 没有与单 agent 的对照评测；任一 agent 失败仍 fail closed。
- reasoning length 失败模式仍存在；4x/64K 只是提高成功率，没有自动降级。
- 真实人工标签 n=3；L6 是 15 个合成 PR。不要用单次运行数字做上线承诺。
- 下一步：合并后开一个新 PR 验证 main + 新 agent 的完整链路；补多仓库依赖图、
  blinded 人工标签和 ensemble 对照评测。
