# 多 AI 协作流程（站长 10/11 定）

Antigravity（Gemini）负责挑战和修复，Claude（Opus）负责审查和守门。站长只看结论。

## 分支

- `main`、`security-fixes` 受保护：不能直接 push，只能通过 PR 合并；禁止强推和删除。
- Antigravity 每次只做一个主题，在 `audit/<topic>` 或 `challenge/<topic>` 分支上工作，推到 `fork`，然后开 PR 到 `security-fixes`。
- 每个分支改动 100～300 行。超过就拆分。
- Claude 自己的修复也走 `fix/<topic>` 分支 + PR，规则一样。

## 每次提交的三件套

1. **测试**：先证明问题存在（修复前失败），再证明修复有效（修复后通过）。
2. **实现**：核心改动。
3. **Review Brief**：写在 commit message 或 PR 描述里，说明改了什么、可能的副作用、安全考量。

## Claude 合并前必查（守门规则）

1. 全量测试 100% 通过，耗时不能明显变长。
2. 不破坏已有接口、额度算法、前端行为。评估周（10-11～10-17）里，任何改变额度或处罚结果的改动都只合并、不打开，或等评估结束。
3. **仓库是公开的**：
   - 不能提交成员 Discord ID、用户名、IP 段、提示词、Key，也不能写「实锤 / 违规」这类指名的结论。
   - 带成员身份的报告只放本地 `exports/`（已在 .gitignore 里）。`docs/AUDIT_*` 被 .gitignore 拦住。
4. 不在别人的工作目录里用 `git add -A`：只 add 自己改过的文件。
5. 涉及上游请求（请求头、TLS、频率）或封号风险的改动，合并前要站长点头。
6. 通过：Claude 合并 PR、部署、在状态页记录（若有用户可见影响）。打回：在 PR 留评论列出原因，Antigravity 按评论修改后再推。

## 只读数据

- 服务器每天 05:10 生成一份脱敏快照，保留 7 份：`/opt/backups/ro/owl-ro-YYYYMMDD.db.gz`。
  - 已去掉：Key 原文（换成短哈希）、盐、提示词原文、头像、原图。
- 本机执行 `deploy/ops/pull-ro-snapshot.sh`，会拉到 `~/dev1/owl-data/latest.db`（只读）。分析只用这份，不登录生产服务器。
- 巡检用的测试 Key 是 `test-sentinel（Antigravity 巡检）`，标了 is_test，不计入统计和小号识别，额度手动定为每天 20 张。Key 文件在 `~/.config/owl/sentinel.key`，不进仓库。
  - 优先探测不出图的接口（`/healthz`、`/status.json`、`/public/live`）。
  - 出图探测每天最多几张：每张都消耗共享账号的额度，也算进全站每小时上限。
