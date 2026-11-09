# codereview-agent

从 DeepLearning.AI L6 抽取的、面向 GitHub 的生产级 PR 安全评审智能体。

L6 解决的是「General vs Security/Pattern 多专家」的实验问题；这个项目补的是上线必须的
三件事：**接入（GitHub App + webhook + 行级评论）、缓存（增量仓库索引 + embedding 缓存）、
真实数据验证（真实 PR 录制、shadow 运行、标注、P/R/F1）**。

## 架构

```
GitHub webhook ──> FastAPI /webhook (HMAC 校验, 去重)
                         │
                         ▼
                  SQLite jobs (pending/running/done)
                         │
                 ┌───────┴────────┐
                 ▼                ▼
          ReviewPipeline      worker x N
                 │
    ┌────────────┼─────────────┬──────────────┐
    ▼            ▼             ▼              ▼
 diff 解析/分块  上下文检索     规范/历史       LLM agents
 (L6 parser)   (增量索引)     (policy/history) (general/security/
    │            │             │              pattern/ensemble)
    └────────────┴──────┬──────┴──────────────┘
                        ▼
              结构化 findings (FILE/LINE)
                        │
                        ▼
         Publisher (行级评论 + 摘要 + 幂等标记)
                        │
                        ▼
              GitHub Review / dry-run
```

## 两种上线方式

- **GitHub Actions（无需服务器）**：把 `docs/github-actions-codereview.yml` 和
  `docs/github-actions-codereview-state.yml` 复制到目标仓库，配 3 个 LLM secrets
  （开启上下文再加 2 个 Cohere secrets），每个 PR 自动评审。
  默认开启：ensemble 双专家、草稿 PR、跨文件上下文、Python 依赖图、历史 finding 检索、
  空结果摘要、SQLite/history 跨 run 缓存、同 PR 新 push 取消旧 run、纯文档 PR 跳过 LLM。
  `push` 到 `main` 的直推也会做一次审计。配合分支保护（要求 `review` check +
  conversation resolution）实现「PR 不能秒合并、main 只能人工 merge」。
  步骤见 `docs/GITHUB-ACTIONS.md`。
- **Webhook 服务（集中式）**：Docker 部署 + GitHub App + 公网 HTTPS，适合多仓库/组织级。
  步骤见 `docs/PRODUCTION.md`。

## 快速开始

```bash
cd _labs/code_review
cp .env.example .env          # 填入模型与 GitHub 凭据
python -m pip install -r requirements.txt
python -m pip install -e .    # 让 python -m codereview.server 可用

# 136 个离线测试（不调用任何外部接口）
python -I -X utf8 -m pytest

# 上线前自检（不联网）：配置、凭据、目录、SQLite、policy/prompt
python -I -X utf8 scripts/check_production.py

# 只评审、不发布
python -I -X utf8 scripts/run_review.py --repo owner/repo --pr 42

# 确认 shadow 结果后再发布
python -I -X utf8 scripts/run_review.py --repo owner/repo --pr 42 --post
```

启动 webhook 服务：

```bash
python -m codereview.server
# 没安装包时：
python -I -X utf8 scripts/serve.py
# 或
docker compose up -d --build
```

国内网络如果拉不到 Docker Hub：

```bash
docker pull docker.m.daocloud.io/library/python:3.11-slim
docker tag docker.m.daocloud.io/library/python:3.11-slim python:3.11-slim
docker build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
  -t codereview-agent:0.1.0 .

# 或 compose 带着镜像参数：
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ docker compose up -d --build
```

## GitHub App 配置

生产环境用 GitHub App，不要用个人 PAT。

| 项目 | 值 |
|---|---|
| Repository permissions | Contents: Read；Pull requests: Read & write；Metadata: Read |
| Subscribe to events | Pull request |
| Webhook URL | `https://<host>/webhook` |
| Webhook secret | 与 `GITHUB_WEBHOOK_SECRET` 一致 |
| Private key | 挂载到容器，路径写入 `GITHUB_PRIVATE_KEY_PATH` |

服务会为每个 installation 缓存 access token，并在 401 时自动刷新。PAT 只作为单仓库本地
调试的降级方案（`GITHUB_TOKEN`）。

## 评审模式

| `REVIEW_MODE` | 调用/分块 | 用途 |
|---|---|---|
| `security`（默认） | 1 | 安全专项，线上默认；L6 中性价比最高 |
| `general` | 1 | 通用评审，兼容 L6 基线 |
| `pattern` | 1 | 只看代码库模式一致性 |
| `ensemble` | 2 | Security + Pattern 共识优先；成本翻倍 |

`REVIEW_DRY_RUN=true` 时只计算、存库、返回 payload，不发布。**上线前必须先跑 shadow**。

GitHub Actions 默认用 `ensemble`：security 与 pattern 两个 agent 各跑一次，两者都命中的
finding 合并保留，另外每个 agent 只保留一条独有的 high/medium finding。成本是单 agent 的
两倍；本仓库还没有 ensemble 对单 agent 的对照指标，不要把它当成已验证的准确率提升。

`REVIEW_DEPENDENCY_ENABLED=true` 会索引工作区里的 `*.py`（最多 2000 个文件、单文件 1 MB），
把「改动文件 import 了谁 / 谁 import 了改动文件」注入 prompt；非 Python 仓库不会产生这一节。

大 diff 会分块；只要有一个 chunk 的 LLM 调用失败，默认**整次不发布**
（`REVIEW_POST_ON_PARTIAL=false`），避免“漏掉的那一块看起来没问题”。每次运行的
run_id / repo / PR / head_sha / 状态 / finding 数会追加到 `data/audit.jsonl`。

## 自定义 Prompt

启动时自动加载 `REVIEW_PROMPT_DIR`（默认 `prompts/`）：

- 全局：`prompts/security.md`、`prompts/pattern.md`、`prompts/general.md`
- 单仓库：`prompts/repos/<owner>__<repo>/security.md`

模板必须保留占位符（`{title}` `{diff}` `{task_section}` `{context_section}`，general
还要 `{instructions}`），字面花括号写成 `{{` / `}}`。格式说明见 `prompts/README.md`。
无效模板默认跳过并告警；`PromptRegistry.load_directory(..., strict=True)` 可改为启动失败。

## 缓存

三层，全部落在 `data/`：

1. **Review cache**：`(repo, pr, head_sha, mode, config, prompt, policy)` 命中后不再调用 LLM。
2. **Embedding cache**：按 `model + input_type + text hash` 存 SQLite（float32 BLOB）。
   未变更的代码块跨 PR、跨进程都不会重复 embedding。
3. **Chunk / repo index cache**：按 `repo + path + blob_sha + chunker版本` 缓存 AST 分块；
   `repo_index_state(repo, ref)` 命中且 Chroma collection 存在时直接跳过建索引。

仓库源码通过 GitHub tarball 下载到 `CACHE_DIR/repos/<repo>/<ref>/`，只保留最近
`REPO_CACHE_KEEP` 个 ref。解压只允许普通文件/目录，拒绝绝对路径、`..`、符号链接和超限大小。

GitHub Actions 里 SQLite（review/embedding 缓存）与 `data/history.jsonl` 通过
`actions/cache/restore` / `actions/cache/save` 保留；Chroma 每次重建，但未变更的
chunk 直接命中 SQLite 里的 embedding，不会重复调用 Cohere。缓存 key 含 `run_id` +
`run_attempt`，`restore-keys` 取最近一份。

GitHub 的 PR cache 只对同一个 PR 可见，所以 workflow 还会把状态作为 artifact 上传；
`codereview-state.yml` 在默认分支用 `workflow_run` 把它转存成 main 作用域的 cache，
后续 PR 才能复用 embedding 和历史。这个伴随 workflow 只有在默认分支上才会生效。

历史 finding 检索（`REVIEW_HISTORY_ENABLED=true`）会在每次成功发布后把 findings 追加到
`REVIEW_HISTORY_PATH`，按 `(repo, pr, file, line, issue)` 去重，最多保留 2000 条；
后续 PR 用标题 + diff 做关键词检索，把相似的历史 finding 注入 prompt。

## 行级评论与幂等

- 只有落在 diff 新增行（`valid_line_map`）上的 finding 才会变成 inline comment；
  其余进入 review 摘要，仍带 `file:line`。
- 每条 review body 带隐藏标记 `<!-- codereview:<fingerprint> -->`，fingerprint 覆盖
  findings + 评审配置。发布前同时检查 SQLite 和 GitHub 已有 review，避免重复评论。
- GitHub 返回 422（行号已失效）时自动降级为「只发摘要」，不会整次失败。

## 真实数据验证

### 1. 录制真实 PR

```bash
python -I -X utf8 scripts/record_pr.py \
  --repo apache/superset --pr 43111 \
  --out eval/datasets/real.jsonl \
  --label-template eval/labels/superset-43111.json
```

`--label-template` 会导出一个带 diff/文件列表的标注模板；把人工确认的问题写进
`expected` 后，用 `--labels` 重新录制即可。也可以用 `--include-review-comments` 把
GitHub 上的人工 review 评论存进 `meta`，`--labels-from-comments` 可把它们当弱标签
（不是安全 ground truth，仍需人工筛）。

### 2. Shadow 运行与标注

```bash
# 服务以 REVIEW_DRY_RUN=true 跑一段时间
python -I -X utf8 scripts/shadow_report.py export --out eval/datasets/shadow.jsonl
# 人工给每条记录补 expected
python -I -X utf8 scripts/shadow_report.py import-labels \
  --file eval/datasets/shadow.jsonl --dataset-out eval/datasets/shadow_labeled.jsonl
```

### 3. 评估

```bash
python -I -X utf8 scripts/evaluate.py \
  --dataset eval/datasets/shadow_labeled.jsonl \
  --runs 3 --match keyword
```

指标：

- **Issue P/R/F1**：expected 问题 vs 模型 findings，贪心一对一匹配。
- **Line P/R/F1**：expected `(file, line)` vs finding `(file, line)`。
- **Actionable rate**：findings 中能落到 diff 新增行的比例（决定能不能发 inline comment）。
- **Bootstrap 95% CI**：按 PR 重采样；`--runs N` 同时报告跨次运行标准差。

`--match hybrid` 会额外用 embedding 做语义匹配。**报告收益时必须同时给 keyword-only 口径**：
L6 已经出现过「hybrid 增益、keyword 反降」的情况。

### 已跑出的结果（诚实披露）

15 个 L6 合成 PR（30 个 expected issue），`deepseek-flash`，单次运行：

| 配置 | P | R | F1 | actionable | errors |
|---|---|---|---|---|---|
| security，无上下文，keyword | 0.3056 | 0.3667 | 0.3333 | 0.8778 | 0 |
| security，有上下文，keyword | 0.3077 | 0.4000 | 0.3478 | 0.7833 | 0 |
| security，有上下文，hybrid | 0.7667 | 0.7667 | 0.7667 | 0.8111 | 1* |

\* hybrid 那次 `l6:pr_003` 出现 `finish_reason=length` 的空响应；已加入「长度截断后
加倍 budget 重试一次」的保护。hybrid 的高分主要来自语义匹配口径，不能当作线上收益。

已录制 3 个真实 PR（`psf/requests#6963`、`encode/httpx#3564`、`apache/superset#43111`），
并从 GitHub 抓取了**真实人工 review 评论**作为 expected（`eval/datasets/real_human_labeled.jsonl`）：

- Superset：10 条 reviewer 级 inline 评论（已排除 bot、作者回复、tests/）。
- requests：0 条 —— reviewer 明确说 CVE 已在 main 修复，这个 PR 只是补回归测试。
- httpx：0 条 —— 无 review 评论。

用真实 shadow 输出回放 + hybrid 匹配（`eval/reports/real_human_review_hybrid.json`，n=3）：

| 指标 | 值 |
|---|---|
| Issue Precision | 0.6667 |
| Issue Recall | 0.2000 |
| Issue F1 | 0.3077 |
| Actionable rate | 0.3333 |

Superset 上模型命中了 10 条人工意见中的 2 条（cache-replay / chartless 两个方向），漏掉 8 条
关于 sentinel、组合 clause、交叉过滤、`isExtra` 绕过、500 vs 403 等更细的意见；requests 的
1 条“CVE 没修”是误报，因为 reviewer 说已在 main 修复。**n=3，且 human 评论对应的是 review
历史而非最终 diff 的逐行状态，不能当独立 benchmark**；但方向很清楚：真实 PR 上 recall 主要
受限于细粒度安全审查，precision 受“修复已存在于别处”影响。

作者复核的 pilot 标签（`eval/reports/real_pilot_keyword.json`）得到 P 0.3333 / R 1.0 /
F1 0.5，只作对照；人工 review 标签的结果更可信。独立人工标注仍需按
`docs/PRODUCTION.md` 的 shadow 流程积累 20–50 个 PR。

缓存验证（`eval/reports/index_cache_stats.json`）：L6 的 25 个文件 → 124 个 chunk，
整个 3 轮评估（45 次 review run）只建了 **1 次**索引；chunk cache 命中 25 个文件，
embedding cache 共 194 条。跨 PR 没有重复解析或重复 embedding。

## 目录

```
src/codereview/
  github/       App JWT、REST client、webhook 校验
  diff/         unified diff 解析与分块（L6 抽取）
  context/      AST/窗口分块、embedding 缓存、增量索引、Chroma
  llm/          客户端、prompt 注册表、general/security/pattern/ensemble
  review/       pipeline、policy、history、publisher、workspace
  storage/      SQLite（runs/jobs/cache/labels/snapshots）
  evaluation/   dataset、metrics、runner
  server/       FastAPI、worker、Prometheus
scripts/        run_review / record_pr / evaluate / shadow_report / seed_from_l6
```

## 文档

- `docs/GITHUB-ACTIONS.md`：免服务器方案：每个 PR 自动跑、只用 Actions Secrets。
- `docs/github-actions-codereview.yml`：可直接复制到目标仓库的 workflow。
- `docs/PRODUCTION.md`：GitHub App 权限/事件、shadow 上线、运维与排错。
- `docs/L6-EXTRACTION.md`：L6 模块到生产模块的映射。
- `docs/VALIDATION.md`：测试、缓存、合成/真实数据、服务冒烟的全部验证记录。

## 交付检查

- `pytest`：136 个离线测试。
- **真实发布验证（webhook）**：临时私有仓库/PR 上发出 2 条 review、3 条 inline comment；
  第三次运行 `cache_hit=true`，没有重复 review。证据见 `eval/reports/real_posting_summary.json`。
- **GitHub Actions 端到端**：临时仓库 + 真实仓库 `local-ai-agent-setup` 均验证成功；
  真实仓库 PR 自动出现 1 条 review + 2 条 inline comment。证据见
  `eval/reports/github_actions_smoke.json`、`eval/reports/github_actions_local_repo_smoke.json`。
- **push audit**：第一次运行抓到 workflow 里的真实 shell 命令注入并失败；修复后第二次
  push audit 成功。证据见 `eval/reports/github_actions_push_audit.json`。
- **main 分支保护已启用**：必须 PR、必须通过 `review` check、必须解决 AI 评论、
  enforce admins、禁止 force push/删除。证据见
  `eval/reports/github_actions_branch_protection.json`。
- **上下文检索已实测**：PR #3 开启 Cohere 后 `review` check success，索引 5 files /
  14 chunks；AI 对 Cohere 数据外发和检索内容 prompt injection 提出 2 条 medium 评论。
  证据见 `eval/reports/github_actions_context_pr.json`。
- **Actions 全量开关**：PR #4 的 ensemble 运行 success（2 calls / 446 s / 15 chunks），
  第二次运行复用 5 个文件、只新 embed 2 个 chunk；状态 artifact 237 KB。AI 对
  docs-only 检测提的 2 条 medium 评论已修复。证据见
  `eval/reports/github_actions_enable_all.json`。
- **依赖图与历史 finding 实测**：probe PR #5 用 agent feature branch 运行，
  `dependency_chars=393`、`history_chars=477`，第二次运行 8/9 文件复用、只新 embed
  1 个 chunk；历史记录从 3 条增长到 4 条。证据见
  `eval/reports/github_actions_dependency_history.json`。
- **发现并修复 silent no-review**：一次 ensemble 运行中 pattern agent 返回
  `finish_reason=length`，pipeline fail closed 未发布，旧 CLI 仍返回 0，导致 required
  check 绿着却没有 review。agent PR 的 `review_exit_code()` 现在在 `--post` 模式未发布
  时返回非零；测试增至 136。
- **长度重试实测**：2x/32K 仍返回空；probe #6 用 4x/64K + `OPENAI_TIMEOUT=300`
  成功发布 review `5827083959`（2 条 finding 都是 probe 自己的 feature-branch ref）。
  证据见 `eval/reports/github_actions_length_retry.json`。
- 真实 uvicorn 进程冒烟：`/healthz` 200 → 签名 webhook 202 → job `done` →
  `/readyz` 200 → `/metrics` 有计数 → graceful shutdown。
- `python -m pip wheel . --no-deps -w dist`：wheel 构建通过；解包到临时 target 后
  `codereview`、CLI、`create_app` 均可导入。
- **Docker 镜像已构建并运行**：`codereview-agent:0.1.0`（894 MB）；容器 `/healthz` 200、
  `/readyz` 200、签名 webhook 202、worker 处理、`/metrics` 正常；测试容器已删除。
- `docker-compose.yml`：YAML 解析通过。
- `.github/workflows/ci.yml`：compile + pytest + wheel。
- `scripts/check_production.py`：上线前离线自检。

## 已知边界

- 上下文分块对 Python 用 AST；其他语言是整文件/窗口，不做语法理解。
- `REVIEW_DEPENDENCY_ENABLED` 接的是**单仓库内**的 Python import 图；L6 的跨仓库
  module→repo 图仍保留但未接入 pipeline。
- 历史 review 检索是关键词重叠，不是语义检索；历史从空开始，需要几次 PR 才有内容。
  历史 finding 可能把模型引向重复旧问题，prompt 要求逐条对照当前 diff，但没有评测能证明
  它不会增加误报。
- ensemble 目前没有与单 agent 的对照评测，只有 L6 的 security/pattern 实验数据；
  任一 agent 出错都会按 partial failure 整次不发布（fail closed），所以 ensemble 的
  “无结果”概率比单 agent 高。CLI 在 `--post` 模式下遇到“未发布”的
  `completed_with_errors` 会返回非零，避免 required check 绿着但没有 review。
- GitHub Actions 的缓存是“最后写入者获胜”：多个 PR 并发时，各自基于同一份 SQLite
  快照运行，后保存的会覆盖先保存的新 embedding/history。只影响性能和历史的完整度，
  不影响 review 的正确性。
- 大 diff 分块在 L6 fixture 上是 **-2.71pp F1**（只有 1/15 被拆），真实大 PR 仍需验证。
- 没有 GitHub App manifest 自动注册；需要手动创建 App。
- 真实发布测试用的临时私有仓库 `huajielong/codereview-agent-smoke` 已在验证后删除
  （HTTP 204，复查 404）。
- 没有多进程/多副本共享索引锁；同一 repo+ref 的并发建索引请把 `REVIEW_CONCURRENCY` 设为 1。
- 所有指标都是 LLM 输出，存在单次运行方差；不要用一次运行的数字做上线承诺。
