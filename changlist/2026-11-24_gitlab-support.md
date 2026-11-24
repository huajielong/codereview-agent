# 2026-11-24 GitLab 支持

## 目标

在保留 GitHub 行为的前提下，让同一套 review pipeline 支持 GitLab Merge Request：

- CLI：`--provider gitlab`，`--repo group/project`，`--pr <MR IID>`。
- Webhook：`POST /webhook/gitlab`，项目/群组 webhook 可用。
- CI：`ci/gitlab-codereview.yml`（`include:project`）与复制版。
- 服务端/server 可同时接 GitHub 与 GitLab（`ProviderRouter`）。

## 主要改动

### 新增

- `src/codereview/scm.py`：`CodeHost` Protocol、repo 规范化/全限定、provider 检测、
  `ProviderRouter`。
- `src/codereview/gitlab/client.py`：REST v4 客户端；MR diff、compare、archive、
  notes/discussions、summary-last 发布、逐条 inline marker、截断检测。
- `src/codereview/gitlab/webhook.py`：legacy token / Standard Webhooks 验签、
  Merge Request 事件映射、去重键、bot 判定。
- `tests/`：93 个离线测试（FakeGitHub/FakeGitLab/FakeLLM、MockTransport、TestClient）。
- `ci/gitlab-codereview.yml`、`docs/gitlab-codereview.example.yml`、`.gitlab-ci.yml`。
- `docs/GITLAB.md`。
- `scripts/gitlab_bootstrap.py`：建私有项目/推送/CI 变量/deploy token（不打印 secret）。

### 修改

- `config.py`：`GITLAB_*`、`SCM_PROVIDER`、`GITLAB_ALLOW_TRUNCATED_DIFF`、
  provider-aware `require_webhook`、`repo_allowed`、redacted；`ENGINE_VERSION`
  bump 到 `2026-11-24.1`。
- `errors.py`：`SCMAPIError`、`GitLabAPIError`、`GitLabAuthError`。
- `publisher.py`：GitLab 错误不再走 GitHub 422 的“去掉 inline 重发”路径；marker
  文案改为 provider 中性。
- `factory.py` / `cli.py` / `scripts/run_review.py`：provider 选择与 repo 规范化。
- `server/app.py`：`/webhook/gitlab`；按 provider 独立启用路由；delivery 前缀。
- `scripts/check_production.py`：`--provider github|gitlab|both`。
- `.github/workflows/codereview.yml`：保持 CLI 默认 provider（github）。不能加
  `--provider github`：GitHub Actions 从 PR base SHA 检出 agent 代码，base 版本的
  CLI 不认识新参数，会在自审 PR 时直接退出 2。
- `README.md`、`CLAUDE.md`、`.env.example`、`.gitignore`、`pyproject.toml`、
  `MANIFEST.in`。

## 关键设计决定

1. **GitLab 发布不是原子的**：inline discussion 先发、summary note 最后发；每条
   inline 带独立 marker，重试只补缺失；summary 成功后才 `mark_posted`。
2. **repo 身份**：GitLab 用 `<host>/<path>`，GitHub 保持 `owner/repo`，共用 SQLite
   但不撞键，无需迁移。
3. **截断 fail closed**：`too_large`/`collapsed`/`changes_count` 不匹配/
   `compare_timeout` 默认 413；只有 `GITLAB_ALLOW_TRUNCATED_DIFF=true` 才继续，并在
   summary 标注。
4. **stale head fail closed**：发布前重新取 MR，head 变化/关闭/合并 → 409，不发。
5. **Webhook 不降级**：配置 signing secret 后只接受 HMAC，不接受 legacy token。
6. **token 分权**：CI clone 用 deploy token；评审用 `api` token。

## 安全加固（review 反馈）

- GitLab HTTP 客户端不再自动跟随 redirect；`PRIVATE-TOKEN` 只发同源，分页 `Link`
  必须同源；archive 跨域跳转时去掉 token 再跟随。
- `scripts/gitlab_bootstrap.py` 关闭 redirect 跟随，3xx 直接报错。
- `/webhook` 与 `/webhook/gitlab` 验签前按客户端 IP 限流
  （`WEBHOOK_RATE_LIMIT`/`WEBHOOK_RATE_WINDOW`），并限制 body
  （`WEBHOOK_MAX_BYTES`）；超限 429/413。
- `GITLAB_BASE_URL` / `GITLAB_API_URL` 强制 `https://`（localhost 明文例外）。
- GitLab CI clone **必须**用 `read_repository` deploy token，模板不再回退到
  `api` 评审 token；clone 走 credential helper，token 不进 URL/`.git/config`。
- GitLab CI cache key 含 `$CI_COMMIT_REF_SLUG`、`$CI_COMMIT_REF_PROTECTED` 和
  `$CI_MERGE_REQUEST_IID`，按分支/MR 隔离，避免 `Main`/`main` 这类 slug 碰撞；
  agent ref 默认 `v0.1.0`，`main`/`master` 需显式
  `CODEREVIEW_ALLOW_MUTABLE_AGENT_REF=true`。
- 文档建议 CI 评审 token 用 **project access token**；CI 变量对能修改
  `.gitlab-ci.yml` 的人可见，需限制 push/run pipeline 权限。这是 CI 模式接受的
  风险，webhook 模式不把 token 暴露给 MR job。

### 后续修复（PR #7 review）

- `GitLabClient.create_review`：inline 只有 400/422（位置失效）跳过；401/403/409/
  429/5xx 直接中止，不再带着缺失的 inline 继续写 summary。写 summary 前再次
  取 MR，head 推进/关闭/合并则 409。
- GitLab CI cache key 增加 `$CI_COMMIT_REF_PROTECTED` 与 `$CI_MERGE_REQUEST_IID`，
  修复 `$CI_COMMIT_REF_SLUG` 大小写归一/截断导致的 key 碰撞。
- `scripts/gitlab_bootstrap.py --set-review-token` 要求单独的
  `--review-token`/`--review-token-file`；只有显式
  `--allow-pat-as-review-token` 才允许写入 bootstrap PAT。新增
  `--protected-variables`（仅适合保护分支/tag 的 push 审计）。
- 文档明确 CI 模式 token 暴露是接受的风险，并给出 project access token、权限
  收敛、webhook 替代方案。

## 验证

```bash
python -m compileall -q src scripts tests
python -I -X utf8 -m pytest -q          # 93 passed
python -m pip wheel . --no-deps -w dist
python -I -X utf8 scripts/check_production.py --provider gitlab
```

真机（2026-11-24，GitLab.com）：

- agent 项目 `huajielong/codereview-agent`（private）：`main` +
  `feat/gitlab-support` 已推送；MR !1 的 `verify` pipeline 成功。
- demo 项目 `huajielong/codereview-demo`（private）：MR !1 的 `codereview`
  job 成功；summary note（1 high，`app.py:6`）+ inline discussion（`app.py:6`）；
  重试同一 job 后 summary=1、inline=1；`main` 的 `codereview-push-audit` 成功。
- `/webhook/gitlab` 本地 uvicorn + 签名 curl：正确签名 202，错误签名 401，
  配 signing secret 后 legacy token 401；job 行的 repo/pr/installation_id 正确。
- `feat/gitlab-support` 已推到 GitHub `origin`。

待办/未覆盖：

- 真机 webhook 需要 GitLab 能访问的公网 HTTPS；目前用 TestClient + 本地 curl 覆盖。
- stale head 409、truncated 413、半途崩溃只补缺失有单测，未做真机故障注入。
- agent MR 合并后，把 demo 的 `ref`/`CODEREVIEW_AGENT_REF` 从
  `feat/gitlab-support` 换成 `main`。
- fork MR 的 CI 模式不支持；用 webhook 服务。

## 限制

- GitLab 行内评论只支持新增行；旧侧/上下文行落 summary。
- 没有 GitLab commit status / approval 回写；CI 用 job 退出码，webhook 用 MR note。
- GitLab.com Free 私有项目受 compute minutes/storage 配额限制。
- `ci/gitlab-codereview.yml` 用 `resource_group` 按项目串行化；同项目多 MR 会排队。
