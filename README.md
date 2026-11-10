# codereview-agent

从 DeepLearning.AI L6 实验抽取的 GitHub PR 安全评审服务。它拉取 PR diff，按
security / pattern / general / ensemble 模式调用 LLM，生成带 `file:line` 的
findings，并通过 GitHub Review 回写。仓库同时包含 webhook 服务、GitHub Actions
自审 workflow、仓库上下文检索、依赖图和缓存。相对 L6 notebook，这里补上了 GitHub
接入、评论回写、缓存和可重复的多语言评测。

当前状态：内部试点项目，未声明许可证。

## 快速开始

在仓库根目录执行：

```bash
cp .env.example .env
# 至少填写 OPENAI_*；使用上下文检索时再配置 embedding
python -m pip install -r requirements.txt
python -m pip install -e .

# 离线自检：配置、凭据、目录、SQLite、policy/prompt
python -I -X utf8 scripts/check_production.py

# 只评审、不发布
python -I -X utf8 scripts/run_review.py --repo owner/repo --pr 42

# 确认结果后再发布
python -I -X utf8 scripts/run_review.py --repo owner/repo --pr 42 --post
```

`scripts/check_production.py` 不联网；缺少必填配置时会报错。`--post` 才会写回
GitHub。

本仓库不包含 `tests/`，直接运行 `pytest` 不会收集到测试。测试套件保留在实验
目录，未随本仓库发布。

## 架构

```text
GitHub webhook ──> FastAPI /webhook（HMAC、delivery 去重）
                         │
                         ▼
                  SQLite jobs（pending/running/done）
                         │
                         ▼
                    worker x N
                         │
                         ▼
                   ReviewPipeline
                         │
        ┌────────────────┴─────────────────┐
        ▼                                  ▼
  上下文构建（可选）                   diff 解析/分块
  - 仓库索引 + 向量检索
  - Python / C/C++ / JS/TS 依赖图
  - policy / 历史 finding
        │                                  │
        └──────────────┬───────────────────┘
                       ▼
              拼接 prompt ──> LLM agents
                         ├─ general
                         ├─ security
                         ├─ pattern
                         └─ ensemble（security + pattern）
                       │
                       ▼
              结构化 findings（FILE/LINE）
                       │
                       ▼
       Publisher（行级评论 + 摘要 + 幂等 marker）
                       │
                       ▼
              GitHub Review / dry-run
```

上下文检索、依赖图、policy 和历史 finding 都只是 LLM 的输入，不直接产生
findings。review cache 命中时，`ReviewPipeline` 直接返回缓存结果，不调用 LLM。
`ensemble` 会分别调用 security 与 pattern，再合并结果。

## 部署

### Webhook 服务

```bash
python -m codereview.server
# 未安装包时：
python -I -X utf8 scripts/serve.py
# 或
docker compose up -d --build
```

生产环境建议使用 GitHub App，而不是个人 PAT：

| 项目 | 值 |
|---|---|
| Repository permissions | Contents: Read；Pull requests: Read & write；Metadata: Read |
| Subscribe to events | Pull request |
| Webhook URL | `https://<host>/webhook` |
| Webhook secret | 与 `GITHUB_WEBHOOK_SECRET` 一致 |
| Private key | 挂载到容器，路径写入 `GITHUB_PRIVATE_KEY_PATH` |

PAT 仅作为单仓库本地调试的降级方案（`GITHUB_TOKEN`）。

### GitHub Actions 自审

仓库内的 `.github/workflows/codereview.yml` 只用于本仓库的 PR 自审：它从 PR
的 base SHA 检出 reviewer 代码，通过 GitHub API 拉取 diff，配置
`OPENAI_*` / `COHERE_*` secrets 后发布 review。自审使用 `security` 模式；
`push` 到 `main` 时另有 `push-audit`。它不是一个可直接复制到任意目标仓库的
通用模板；目标仓库需要自行提供 workflow，并指定本仓库作为 agent 源。

## 评审模式

| `REVIEW_MODE` | 调用/分块 | 用途 |
|---|---|---|
| `security`（默认） | 1 | 安全专项 |
| `general` | 1 | 通用评审 |
| `pattern` | 1 | 代码库模式一致性 |
| `ensemble` | 2 | Security + Pattern，合并结果；成本翻倍 |

`REVIEW_DRY_RUN=true` 时只计算、存库、返回 payload，不发布。上线前应先跑
shadow。

## 上下文与依赖

`REVIEW_DEPENDENCY_ENABLED=true` 会索引工作区（最多 2000 个文件、单文件 1 MB），
按语言注入「改动文件依赖了谁 / 谁依赖了改动文件」：

- Python：`import` / `from ... import`；
- C/C++：本地 `#include "..."`（系统 `<...>` 忽略）；
- JS/TS：相对 `import` / `export ... from` / `require` / 动态 `import()`。

外部包、`node_modules`、`vendor`、`third_party` 和系统头文件不建图；不支持的语言
不会产生这一节。`REVIEW_CONTEXT_ENABLED=true` 时，检索结果同样作为 LLM 输入，
不是 findings 来源。

## 缓存

三层，全部落在 `data/`：

1. **Review cache**：`(repo, pr, head_sha, mode, config, prompt, policy)` 命中后
   不再调用 LLM。
2. **Embedding cache**：按 `model + input_type + text hash` 存 SQLite
   （float32 BLOB），未变更的代码块跨 PR、跨进程都不会重复 embedding。
3. **Chunk / repo index cache**：按 `repo + path + blob_sha + chunker 版本` 缓存
   AST 分块；`repo_index_state(repo, ref)` 命中且 Chroma collection 存在时跳过
   建索引。

GitHub Actions 只缓存 `data/codereview.db*` 和 `data/history.jsonl`；Chroma 每次
重建，但未变更的 chunk 会复用 SQLite 里的 embedding。缓存 key 含 `run_id` +
`run_attempt`，`restore-keys` 取最近一份。GitHub 的 PR cache 作用域是当前
PR/分支，不能保证跨 PR 复用；仓库没有 artifact 或 `codereview-state.yml`
转存 workflow。

历史 finding 检索（`REVIEW_HISTORY_ENABLED=true`）会在成功发布后把 findings
追加到 `REVIEW_HISTORY_PATH`，按 `(repo, pr, file, line, issue)` 去重，最多保留
2000 条；后续运行用标题 + diff 做关键词检索，把相似历史 finding 注入 prompt。
同一数据目录下的 webhook 服务共享历史；GitHub Actions 的缓存按 PR/分支作用域，
历史通常只在该 PR 的后续 run 中可见，不会自动跨 PR 累积。

## 行级评论与幂等

- 只有落在 diff 新增行（`valid_line_map`）上的 finding 才会变成 inline comment；
  其余进入 review 摘要，仍带 `file:line`。
- 每条 review body 带隐藏标记 `<!-- codereview:<fingerprint> -->`，fingerprint
  覆盖 findings + 评审配置。发布前同时检查 SQLite 和 GitHub 已有 review，避免
  重复评论。
- GitHub 返回 422（行号已失效）时自动降级为「只发摘要」，不会整次失败。
- 大 diff 分块；任一 chunk 的 LLM 调用失败时，默认整次不发布
  （`REVIEW_POST_ON_PARTIAL=false`），避免漏掉的那一块看起来没问题。

## 评测

仓库提交的是多语言评测数据集和 Markdown 摘要；JSON 报告由脚本本地生成并已加入
`.gitignore`：

```bash
python -I -X utf8 scripts/seed_multilang_eval.py
python -I -X utf8 scripts/evaluate_multilang.py

REVIEW_CONTEXT_ENABLED=true REVIEW_DEPENDENCY_ENABLED=true \
python -I -X utf8 scripts/evaluate.py \
  --dataset eval/datasets/multilang.jsonl --runs 3 --match keyword \
  --review-mode security \
  --out eval/reports/multilang_review.json \
  --markdown eval/reports/multilang_review.md
```

已提交的摘要（`eval/reports/*.md`）显示：

- 依赖图 edge-level：7 个 case / 15 条边，P/R/F1 = 1.0000；
- review-level：7 个合成 PR、3 runs、keyword，issue P 0.9697 / R 1.0000 /
  F1 0.9841，line F1 1.0000，actionable 0.8810。

这些是合成 fixture 上的回归指标，不是真实仓库 benchmark。L6 与真实 PR 的早期
评测记录保留在实验目录，未随本仓库发布。

## 文档与目录

- `docs/MULTILANG-SPEC.md`：多语言 Context / Dependency 的规格与评测口径。
- `CLAUDE.md`：开发约定、命令和模块说明。
- `prompts/README.md`：prompt 覆盖文件的格式。
- `changlist/2026-11-10_multilang-context.md`：多语言改动的变更记录。

```text
src/codereview/
  github/       App JWT、REST client、webhook 校验
  diff/         unified diff 解析与分块
  context/      AST/窗口分块、embedding 缓存、增量索引、Chroma
  llm/          客户端、prompt 注册表、general/security/pattern/ensemble
  review/       pipeline、policy、history、publisher、workspace
  storage/      SQLite（runs/jobs/cache/labels/snapshots）
  evaluation/   dataset、metrics、runner
  server/       FastAPI、worker、Prometheus
scripts/        run_review / record_pr / evaluate / shadow_report / seed_from_l6
```

## 自定义 Prompt

启动时读取 `REVIEW_PROMPT_DIR`（默认 `prompts/`）。目录不存在或文件缺失时使用
`src/codereview/llm/prompts.py` 里的内置 prompt。可选覆盖文件：

- 全局：`prompts/security.md`、`prompts/pattern.md`、`prompts/general.md`
- 单仓库：`prompts/repos/<owner>__<repo>/security.md`

模板必须保留占位符（`{title}` `{diff}` `{task_section}` `{context_section}`，
general 还要 `{instructions}`），字面花括号写成 `{{` / `}}`。格式说明见
`prompts/README.md`。

## 已知边界

- 上下文分块对 Python 用 AST；其他语言是整文件/窗口，不做语法理解。
- 依赖图是单仓库内的 Python / C/C++ / JS/TS 图：C/C++ 只解析本地
  `#include "..."`，不处理宏/条件编译/生成头文件；JS/TS 只解析相对
  import/require，不处理 `tsconfig` 路径别名。跨语言 API/protobuf/gRPC 不在
  范围内。
- 历史 review 检索是关键词重叠，不是语义检索；历史从空开始，需要几次 PR 才有
  内容。历史 finding 可能把模型引向重复旧问题，prompt 要求逐条对照当前 diff，
  但没有评测证明它不会增加误报。
- ensemble 没有与单 agent 的对照评测；任一 agent 出错都会按 partial failure
  整次不发布。
- GitHub Actions 的缓存按 PR/分支作用域，不同 PR 之间不保证复用；同一 PR 的并发
  run 由 `concurrency` 取消，不会并行写同一份状态。
- 没有 GitHub App manifest 自动注册；需要手动创建 App。
- 没有多进程/多副本共享索引锁；同一 repo+ref 的并发建索引请把
  `REVIEW_CONCURRENCY` 设为 1。
- 所有指标都是 LLM 输出，存在单次运行方差；不要用一次运行的数字做上线承诺。

许可证：未声明。
