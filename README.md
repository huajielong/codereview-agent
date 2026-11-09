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

- **GitHub Actions（无需服务器）**：把 `docs/github-actions-codereview.yml` 复制到目标仓库，
  配 3 个 LLM secrets，每个 PR 自动评审。步骤见 `docs/GITHUB-ACTIONS.md`。
- **Webhook 服务（集中式）**：Docker 部署 + GitHub App + 公网 HTTPS，适合多仓库/组织级。
  步骤见 `docs/PRODUCTION.md`。

## 快速开始

```bash
cd _labs/code_review
cp .env.example .env          # 填入模型与 GitHub 凭据
python -m pip install -r requirements.txt
python -m pip install -e .    # 让 python -m codereview.server 可用

# 109 个离线测试（不调用任何外部接口）
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

- `pytest`：109 个离线测试。
- **真实发布验证（webhook）**：临时私有仓库/PR 上发出 2 条 review、3 条 inline comment；
  第三次运行 `cache_hit=true`，没有重复 review。证据见 `eval/reports/real_posting_summary.json`。
- **GitHub Actions 端到端**：临时仓库 + 真实仓库 `local-ai-agent-setup` 均验证成功；
  真实仓库 PR 自动出现 1 条 review + 2 条 inline comment。证据见
  `eval/reports/github_actions_smoke.json`、`eval/reports/github_actions_local_repo_smoke.json`。
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
- 跨仓库依赖图已实现但默认不接入 pipeline（单仓库场景不需要）。
- 历史 review 检索是关键词重叠，不是语义检索。
- 大 diff 分块在 L6 fixture 上是 **-2.71pp F1**（只有 1/15 被拆），真实大 PR 仍需验证。
- 没有 GitHub App manifest 自动注册；需要手动创建 App。
- 真实发布测试用的临时私有仓库 `huajielong/codereview-agent-smoke` 已在验证后删除
  （HTTP 204，复查 404）。
- 没有多进程/多副本共享索引锁；同一 repo+ref 的并发建索引请把 `REVIEW_CONCURRENCY` 设为 1。
- 所有指标都是 LLM 输出，存在单次运行方差；不要用一次运行的数字做上线承诺。
