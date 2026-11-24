# GitLab 支持

GitHub 与 GitLab 共用同一套 review pipeline、LLM、缓存和发布语义；平台差异集中在
`src/codereview/gitlab/`、`src/codereview/scm.py` 和 `server/app.py` 的路由层。

两种接入方式：

1. **GitLab CI（推荐，零服务器）**：目标仓库的 `.gitlab-ci.yml` 引入
   `ci/gitlab-codereview.yml`，每个 MR pipeline 调一次 CLI。
2. **Webhook 服务**：把 `POST /webhook/gitlab` 配到项目/群组 webhook，服务用
   PAT 访问目标仓库，适合多仓库集中管理或 fork MR。

---

## 1. 凭证

| 凭证 | scope / 角色 | 评论身份 | 用途 |
|---|---|---|---|
| Personal Access Token (PAT) | `api` + `write_repository` | 用户本人 | 跨仓库 CLI / CI / webhook；最省事 |
| Project Access Token | `api`，Developer+ | `project_<id>_bot_<hash>` | 单项目 CI，评论像机器人 |
| Group Access Token | `api`，Maintainer+ | `group_<id>_bot_<hash>` | group 下多仓库一次覆盖 |
| Deploy Token | `read_repository` / `read_registry` | — | **只能读**；不能发评论，仅用于 CI 克隆 agent 仓库 |
| CI_JOB_TOKEN | 受限 API | — | **不能发 MR notes**，不要用于评审 |

推荐拆成两个变量：

- `CODEREVIEW_GITLAB_TOKEN`：评审用 PAT / project token（`api`），只发评论。
- `CODEREVIEW_AGENT_CLONE_USER` + `CODEREVIEW_AGENT_CLONE_TOKEN`：agent 项目的
  deploy token（`read_repository`），只用于 CI 克隆私有 agent 仓库。

`GITLAB_TOKEN` 是服务端/CLI 读取的变量名；CI 模板会把
`CODEREVIEW_GITLAB_TOKEN` 映射过去。Token 只从环境变量或临时文件读，不写日志、
不写入仓库。

CI/CD 变量用 **masked、非 protected**：protected 变量在 feature branch 的 MR
pipeline 里不可见。masked 值需满足 GitLab 的单行/Base64 字符集约束；URL、模型名
这类非密钥变量不要 masked。

---

## 2. CLI

```bash
# dry-run（默认）
python -I -X utf8 scripts/run_review.py \
  --provider gitlab --repo group/project --pr 42

# 发布
python -I -X utf8 scripts/run_review.py \
  --provider gitlab --repo group/project --pr 42 --post

# 等价入口
python -I -X utf8 -m codereview.cli \
  --provider gitlab --repo group/project --pr 42 --post
```

`--repo` 可以是 `group/project`、`group/sub/project`、`gitlab.com/group/project`
或完整 HTTPS URL；内部统一存成 `gitlab.com/group/project`。`--pr` 是 **MR IID**
（项目内编号，不是全局 id）。`--installation-id` 是 GitHub 专用，传了会提示并忽略。

push 审计：

```bash
python -I -X utf8 -m codereview.cli \
  --provider gitlab --repo group/project --push \
  --before <sha> --after <sha> --branch main \
  --push-title "<commit message>" --fail-on-severity high
```

`--before` 全 0 表示新分支，直接 skipped；push 模式永远 dry-run。

---

## 3. GitLab CI

### 3.1 include（同 namespace / 同 group 推荐）

```yaml
include:
  - project: huajielong/codereview-agent
    ref: main
    file: /ci/gitlab-codereview.yml
```

`include:project` 在 pipeline 创建时按触发用户权限解析：触发用户必须能访问
`huajielong/codereview-agent`（私有项目至少 Reporter）。外部仓库无法访问时，
用 `docs/gitlab-codereview.example.yml` 复制版，或改用 webhook 服务。

### 3.2 目标仓库需要配置的变量

| 变量 | 说明 |
|---|---|
| `CODEREVIEW_GITLAB_TOKEN` | 评审 token（`api`），必填 |
| `CODEREVIEW_AGENT_CLONE_TOKEN` | agent 项目 deploy token（`read_repository`）；不填则回退用 `CODEREVIEW_GITLAB_TOKEN` |
| `CODEREVIEW_AGENT_CLONE_USER` | deploy token 用户名；用 PAT 时填 `oauth2` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | LLM |
| `COHERE_API_KEY` / `COHERE_EMBED_MODEL` | 可选 embedding |
| `CODEREVIEW_MODE` | `security`（默认）/ `general` / `pattern` / `ensemble` |
| `CODEREVIEW_CONTEXT_ENABLED` / `..._DEPENDENCY_ENABLED` / `..._HISTORY_ENABLED` | 默认 true |
| `CODEREVIEW_PYTHON_IMAGE` | 默认 `python:3.11`；可换国内镜像 |
| `CODEREVIEW_AGENT_REF` | 默认 `main`；生产建议 pin tag/branch（clone 用 `--branch`，裸 commit SHA 不适用） |

job 默认在 `test` stage；如果目标仓库使用自定义 stages，在包含模板后覆盖
`codereview.stage` / `codereview-push-audit.stage`，或确保 `stages` 里有 `test`。

模板会自动设置 `GITLAB_TOKEN`、`GITLAB_BASE_URL=$CI_SERVER_URL`、状态目录
`.codereview-state/`、`REVIEW_DRY_RUN=false`、`REVIEW_POST_ON_PARTIAL=false`、
`REVIEW_ALLOWED_REPOS=$CI_MERGE_REQUEST_PROJECT_PATH`。

### 3.3 行为与限制

- 只跑 **same-project 的 merge_request_event**：fork MR 的 pipeline 在 fork 项目
  运行，拿不到目标项目变量；模板用 `CI_PROJECT_PATH == CI_MERGE_REQUEST_PROJECT_PATH`
  失败关闭。fork 场景用 webhook 服务。
- `GIT_STRATEGY: none`：不 checkout 目标仓库，diff 走 GitLab API；agent 仓库单独
  clone 到 `.codereview-agent`。
- 不传 `--head-sha`：由 `get_pull` 取当前 head，发布时再做一次 stale check，
  避免 merged-results pipeline 的 SHA 语义差异。
- 状态缓存 `.codereview-state/` + `resource_group: codereview-$CI_PROJECT_ID`
  串行化，避免 SQLite/Chroma 并发。代价是同一项目不同 MR 的 pipeline 会排队；
  这是为了跨 MR 复用 embedding/review cache。
- `codereview` job 失败 → pipeline 失败。项目设置 **Merge requests → Merge checks
  → Pipelines must succeed** 即可作为合并门禁。GitLab Free 没有 GitHub 那种
  “按 job 名 required check”；需要更细的外部状态检查是 Ultimate 功能。
- `codereview-push-audit` 是可选的默认分支 push 审计 job，发现 high 时退出 1。
- GitLab.com Free 私有项目约 400 compute minutes/月、10 GiB 存储；`chromadb` 等
  依赖安装较重，建议保留 pip 缓存、必要时把 `REVIEW_CONTEXT_ENABLED=false`，或
  预构建镜像并覆盖 `CODEREVIEW_PYTHON_IMAGE`。

### 3.4 复制版

`docs/gitlab-codereview.example.yml` 与 `ci/gitlab-codereview.yml` 内容一致，只是
不依赖 `include:project`。复制到目标仓库的 `.gitlab-ci.yml` 后仍需
`CODEREVIEW_AGENT_CLONE_TOKEN`（或 PAT）才能 clone 私有 agent 仓库。

---

## 4. Webhook 服务

```bash
python -m codereview.server        # 或 python -I -X utf8 scripts/serve.py
docker compose up -d --build
```

### 4.1 配置

```env
GITLAB_BASE_URL=https://gitlab.com
GITLAB_TOKEN=glpat-...
GITLAB_WEBHOOK_SIGNING_SECRET=whsec_...   # 推荐
# 或（仅当没有 signing secret 时）
GITLAB_WEBHOOK_SECRET=change-me
```

- 项目/群组 webhook URL：`https://<公网主机>/webhook/gitlab`
- 事件：只勾选 **Merge request events**（push 审计走 CI；服务当前不处理 Push Hook）
- GitLab 必须能访问该 HTTPS 地址；localhost 不行。开发时可用隧道。
- 群组 webhook 可用一个 PAT/group token 覆盖群组内多个项目；PAT 需要对这些项目有
  访问权（至少 Developer 才能发评论）。

### 4.2 验签

两种方式，**signing secret 优先且不降级**：

- `GITLAB_WEBHOOK_SIGNING_SECRET`（推荐）：校验 `webhook-signature`
  （`v1,<base64>`，HMAC-SHA256，message=`{webhook-id}.{webhook-timestamp}.{body}`，
  key 为去掉 `whsec_` 后 base64 解码；时间戳容差 300s）。配置了它之后，
  `X-Gitlab-Token` 不再被接受。
- `GITLAB_WEBHOOK_SECRET`（legacy）：`X-Gitlab-Token` 明文常量时间比较。仅用于
  兼容旧配置；生产建议换成 signing token。

去重键优先级：`webhook-id` → `Idempotency-Key` → 请求体哈希，内部加 `gitlab:`
前缀。**不使用 `X-Gitlab-Event-UUID`**：递归 webhook 会共享该值，会误去重。

### 4.3 事件映射

只处理 `X-Gitlab-Event: Merge Request Hook` 且 `object_kind=merge_request`：

| GitLab action | 内部 action | 是否评审 |
|---|---|---|
| `open` | `opened` | 是 |
| `reopen` | `reopened` | 是 |
| `update` + `oldrev` 非空 | `synchronize` | 是 |
| `update` + draft true→false | `ready_for_review` | 是 |
| `update`（只改 label/title） | `update` | 否 |
| `approved` / `unapproved` / `merge` / `close` | 原样 | 否 |

`head_sha` 只取 `object_attributes.last_commit.id`（payload 没有 `diff_refs`）；
为空则跳过，等下一次事件。draft、bot（`user.bot` 或 `project_*_bot_*` 用户名）、
`REVIEW_ALLOWED_REPOS`、`REVIEW_SKIP_BOTS` 与 GitHub 共用同一套 `should_review`。

### 4.4 双平台

同一个服务可同时接 GitHub 和 GitLab：配置好两边的 secret + 写凭证后，
`build_pipeline` 会构造 `ProviderRouter`，按 `repo` 的 host 前缀路由。
未配置对应 secret 的路由返回 404，不会用空 secret 验签。

---

## 5. 评审与发布语义（与 GitHub 的差异）

- GitLab 没有 GitHub Review 那种“摘要 + N 条行内评论”的原子对象。发布顺序是
  **先逐条 inline discussion，最后发 summary note**：
  - 每条 inline 带 `<!-- codereview-inline:<fingerprint>:<index> -->`；
  - summary 带现有 `<!-- codereview:<fingerprint> -->`；
  - 重试时先扫描 discussions，跳过已发布的 inline，只补缺失，再发 summary；
  - summary 成功后才写 DB `posted_reviews`。
  这样中途崩溃不会重复评论，也不会永久缺 summary。
- summary 是普通 MR note，不是 GitLab “review/approval”；GitLab 的 Approve API
  没有接入。
- 行内评论只支持**新增行**（`position[new_line]`）；旧侧/上下文行 finding 落 summary
  表格。新文件 `old_path=new_path`；重命名从 `/diffs` 元数据取 `old_path`。
- 单条 discussion 位置失效（400/422）会记日志并跳过，summary 仍包含该 finding。
- 发布前重新取 MR：若 head 已推进或 MR 已关闭/合并，返回 409，**什么都不发**；
  下一次 webhook/CI 会评审新 head。这是 fail-closed，避免把旧 findings 锚到新 head。
- diff 完整性：
  - 主路径 `GET .../diffs?unidiff=true`，能暴露 `too_large`/`collapsed`；
  - 兜底 `GET .../raw_diffs`（旧实例），用 `changes_count` 交叉校验文件数；
  - `too_large`/`collapsed`/`changes_count` 不匹配/`compare_timeout` 默认
    **413 fail closed**，不发布半份评审；
  - `GITLAB_ALLOW_TRUNCATED_DIFF=true` 时才继续，并在 summary 顶部标注
    “GitLab 截断了 diff，部分文件未审”。
  - MR 刚创建时 diff 异步填充：`get_pull_diff` 重试 5 次×2s，仍为空则 409。
- 不支持 GitLab commit status / external status；CI 用 job 退出码，webhook 模式
  目前也没有状态回写。

---

## 6. 自建 GitLab

```env
GITLAB_BASE_URL=https://gitlab.example.com
# 可选；默认 <base>/api/v4
GITLAB_API_URL=https://gitlab.example.com/api/v4
```

- `GITLAB_BASE_URL` 用于 repo 身份（`gitlab.example.com/group/project`）和默认
  API 地址；`GITLAB_API_URL` 用于带路径前缀的实例。
- CI 模板用 `CI_SERVER_URL` 自动适配；`include:project` 在同实例内工作。
- 自签证书需要在 runner/服务端信任；webhook 的 SSL verification 按需配置。

---

## 7. 排错

| 现象 | 检查 |
|---|---|
| `401 Unauthorized` | token scope 是否 `api`；是否过期；项目是否可见 |
| `403` 发评论失败 | token 角色至少 Developer；MR 是否允许讨论 |
| `400 Note position is invalid` | 行不在 diff 新增行；文件被重命名/删除；head 已变化；看 summary 是否已包含该 finding |
| `409 stale head` | 评审期间 MR 又推了 commit；下一次 pipeline/webhook 会重评 |
| `409 MR diff is not ready` | MR 刚创建，diff 尚未生成；重跑 pipeline 或等 webhook 重试 |
| `413 truncated` | 大 MR 超过 GitLab diff 限制；拆小 MR，或显式设 `GITLAB_ALLOW_TRUNCATED_DIFF=true` |
| `429` | GitLab API 限流；客户端按 `Retry-After`/`RateLimit-Reset` 退避 |
| CI 拿不到变量 | 变量是否 masked 且 **非 protected**；是否 fork MR；是否在正确项目/群组 |
| `include:project` 失败 | 触发用户对 agent 项目是否有 Reporter 权限 |
| webhook 一直 401 | signing secret 是否配了但请求只带 legacy token（不会降级）；时间戳是否偏差 >300s |
| webhook 被 GitLab 自动禁用 | 连续失败过多；在项目 webhook 页面重新启用并查服务日志 |
| pipeline 排队 | `resource_group` 按项目串行；这是为避免 SQLite/Chroma 并发 |

日志不会打印 token；`Settings.redacted()` 会把 GitLab 三个 secret 打成 `***`。
