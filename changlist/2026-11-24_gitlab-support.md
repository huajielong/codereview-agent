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
- `tests/`：78 个离线测试（FakeGitHub/FakeGitLab/FakeLLM、MockTransport、TestClient）。
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
- `.github/workflows/codereview.yml`：CLI 显式 `--provider github`。
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

## 验证

```bash
python -m compileall -q src scripts tests
python -I -X utf8 -m pytest -q          # 78 passed
python -m pip wheel . --no-deps -w dist
python -I -X utf8 scripts/check_production.py --provider gitlab
```

GitLab 真机待办（需要 PAT / demo 项目）：

- agent 仓库 GitLab pipeline 绿。
- demo MR：summary + inline、重跑幂等、半途崩溃只补缺失、stale head 409、
  截断 413、错 token 时 job 红。
- `/webhook/gitlab`：签名/legacy token、重复 delivery、label-only update 跳过。
- fork MR 的 CI 模式不支持；用 webhook 服务。

## 限制

- GitLab 行内评论只支持新增行；旧侧/上下文行落 summary。
- 没有 GitLab commit status / approval 回写；CI 用 job 退出码，webhook 用 MR note。
- GitLab.com Free 私有项目受 compute minutes/storage 配额限制。
- `ci/gitlab-codereview.yml` 用 `resource_group` 按项目串行化；同项目多 MR 会排队。
