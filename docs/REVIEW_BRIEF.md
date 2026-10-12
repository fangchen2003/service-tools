# 审查交接说明（给安全审查 / 对抗测试用）

> 这份文档是给“另一个审查者”（人或 AI）看的：说明系统是什么、信任边界在哪、哪些行为是**有意设计**的、哪些问题已经修过，避免重复劳动，并把精力集中到真正可能出问题的地方。
> **请勿对真实部署发起攻击流量，也不要使用真实的 NovelAI / Discord 凭据。** 所有验证请在本地、用假上游完成（见文末“如何本地复现”）。

## 1. 系统是什么

NAI Gate 是一个 FastAPI 网关：站长用**一个**付费的 NovelAI 账号，向约 10 位受邀成员分发各自独立的虚拟 Key（`nai-…`）。网关负责鉴权、额度、限流、排队，再把请求转发给 NovelAI。部署形态：单台 2GB VPS，Docker Compose，前面是 Caddy（HTTPS），网关只监听 `127.0.0.1:3003`。

```
成员客户端 ──HTTPS──▶ Caddy ──▶ NAI Gate (127.0.0.1:3003) ──▶ NovelAI
                                 ▲   │
Discord 用户 /register ─▶ Bot ───┘   └─▶ SQLite (data/nai_gate.db)
          (容器内网 + 桥接密钥)
```

## 2. 代码地图（重点文件）

| 文件 | 内容 |
| --- | --- |
| `app/main.py` | 所有成员接口（生图、文本、语音、工具）、`authenticate`、落地页、`/public/status`、后台循环（维护、清理） |
| `app/admin.py` | 后台 API：会话 Cookie、Origin 校验、登录与改密码、成员 / 生成记录 / 运行时开关 |
| `app/registration.py` `registration_routes.py` `ops.py` | Discord 自助领取、封禁、名额、身份组同步、桥接接口 |
| `app/features.py` | 功能开关（全局 + 按 Key） |
| `app/policy.py` | 费用估算、免费档钳制、引用图 / Vibe 校验 |
| `app/state.py` `nai.py` `concurrency.py` | 限流、排队、无效 Key 拦截、上游并发与 429 冷却 |
| `app/audit.py` `alerts.py` | 生成记录（提示词 + 缩略图）、Discord 告警 / 公告 |
| `app/static/index.html` | 后台界面（单文件） |
| `app/static/landing.html` | 成员落地页（严格 CSP） |
| `integration/discord_bot.py` | 机器人（斜杠命令，通过桥接接口调网关） |
| `tests/` | pytest；用假上游，见 FakeState 与 `httpx.ASGITransport` 的写法 |

`git log c385bc1..HEAD` 是 fork 相对原项目新增的全部改动，**这部分经过的审视最少**。

## 3. 攻击者模型

1. 匿名互联网访客（没有 Key）。
2. 持有有效低权限 Key 的恶意成员。
3. 开放注册后混进来的恶意 Discord 用户（先到先得、名额有限、账号需注册满 N 天）。
4. 拿到机器人 Token 或桥接密钥的人。
5. 站长的误操作 / 不安全的默认值。
6. 想白嫖 Anlas（付费点数）、超额使用 V5、绕过功能开关、读别人数据、或拖垮共享队列的成员。

## 4. 信任边界与假设

- **网关端口只绑本机**；Caddy 以 host 网络运行并**覆盖** `X-Forwarded-For`（已实测：伪造该头无效）。容器内设置了 `FORWARDED_ALLOW_IPS=*`，其前提正是“只有本机反向代理能连上”。如果你发现端口能从公网直连，那是严重问题。
- **桥接接口**（`/self-register/*`，`callback` 除外）仅供机器人在容器内网调用，Caddy 对公网返回 404；认证为共享密钥（≥32 字符）+ 发起者必须在 `ADMIN_DISCORD_IDS` 名单内（名单为空则管理类操作一律拒绝）。
- **后台**：会话 Cookie 绑定密码摘要；所有写操作校验 `Origin`；登录有 IP 限流；改密码会使全部会话失效。
- **成员鉴权**：Key 通过 `Authorization: Bearer`；无效 Key 按 IP 计数，超阈值后该 IP 只拦“无效 Key”，有效 Key 不受影响。
- **功能开关**：全局开关 + 每个 Key 的 `features` 列表（NULL 表示沿用旧行为）；管理员 Key 绕过。
- 单个 SQLite 连接（aiosqlite）被所有请求共享；这是已知的规模假设（十几个人），不是疏漏。

## 5. 有意的设计决定（不要当成漏洞报）

- 新成员默认只开通“文生图”；语音合成**仅管理员 Key** 可用（按 Anlas 计费但未接入额度统计）。
- 开放注册先到先得，靠名额上限、账号年龄、Discord 授权（真人）、到期自动释放名额来约束；注册默认关闭，需站长明确开放。
- 被站长“停用”的成员（`enabled=0`）不会被自动释放、清空或闲置清理；永久禁止用 `/ban`（封禁表）。
- 生成记录默认关闭；开启后会在首页、`/help`、领 Key 私信、公告频道向成员披露。流式请求只记提示词（拿不到最终图），缩略图只来自非流式请求。
- `/public/status` 是匿名可读的，只含非敏感字段，并缓存 8 秒。
- 机器人身份组授予 / 摘除失败不会阻塞注册；摘除会进入待办表并重试。

## 6. 已经发现并修复的问题（请勿重复报告）

后台与会话：跨站写入防护（Origin）、公告沙箱化、会话随密码变化而失效、`secret_key` 原子写入、非 ASCII 密码、示例密码拒绝登录、后台可改密码。
注册与桥接：桥接对公网屏蔽、密钥长度与失败计数、管理员白名单、封禁表、停用成员不被释放、身份组摘除重试且不在锁内等 Discord、默认关闭。
额度与上游：文本接口模型白名单、语音仅管理员、`use_string` 缺失导致的聊天 400、通过 generate-image 带参考图需要 `vibe` 权限、`strength` 校验、图片冷却在被拒时退还、文本额度竞态。
可用性：单 Key 占满标签补全队列、无效 Key 拦截误伤同出口 IP 的有效成员、限流字典与日志无限增长、缩略图解压炸弹上限。

## 7. 已知限制（欢迎质疑，但这些是已评估过的）

- 没有按 Key 的公平排队：上游图片槽是全局串行的，一个活跃成员可能挤占别人等待时间（受每 Key 最小间隔约束）。
- 桥接共享密钥一旦泄露，攻击者可对任意 `discord_id` 调用 `/quota`、`/resetkey`（返回新 Key 明文）；管理类操作还需要名单内的 `actor_id`，但 `actor_id` 由调用方自报。缓解：桥接不对公网开放、密钥 ≥32 字符、失败计数。
- 机器人角色在 Discord 里的位置与权限由站长手动管理；代码假设它至少拥有“管理身份组”并位于被管理的身份组之上。
- 上游 Token 可在后台添加 / 替换 / 删除，保存在 `data/upstream_tokens.json`（权限 600，不进数据库）；`NAI_TOKENS` 只用于首次初始化。

## 8. 你可以重点怀疑的地方

1. `registration.py` 中 `finish` 的并发：名额检查、建 Key、写登记、发私信是否有竞态或回滚遗漏？
2. 所有 `/ai/*`、`/nai/ai/*`、`/user/*`、`/v1/*` 路由是否都做了鉴权 + 功能开关 + 额度 + 限流？别名路径有无漏网？
3. `policy.py` 的免费档钳制：`parameters` 之外的字段、嵌套重复键、`n_samples`、尺寸 / 步数边界、引用图字段是否会让请求按“免费”计价却在上游花 Anlas？
4. 流式路径（`generate-image-stream`、文本流）在取消 / 断开时槽位和预算是否一定释放？
5. 后台与落地页的任何 `innerHTML` 路径：成员名、Discord 显示名、提示词都是用户可控的。
6. `audit.py` 缩略图解码在后台任务里：并发多个大图时的内存 / CPU。
7. 告警 / 公告 / 机器人回复里是否可能被用户内容注入 `@everyone` 或 Markdown。

## 9. 如何本地复现（不接触真实服务）

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-asyncio pytest-timeout
.venv/bin/python -m pytest -q -p no:cacheprovider --timeout=30          # 基线，应全部通过

# 起一个隔离实例（假 NovelAI Token，不会发出真实生成请求）
mkdir -p /tmp/review-data
ADMIN_PASSWORD='Review-pass-123456' NAI_TOKENS=pst-fakefakefake ADMIN_COOKIE_SECURE=0 DATA_DIR=/tmp/review-data \
  .venv/bin/uvicorn app.main:app --port 3099
```

写测试时参考 `tests/test_generation_integration.py` 的 `FakeState`、`tests/test_self_register.py` 的 Discord 假服务（`httpx.MockTransport`）。

## 10. 期望的反馈格式

按严重程度（CRITICAL / HIGH / MEDIUM / LOW）列出：标题、文件与行号、**可操作的攻击或故障场景与前置条件**、证据（你运行了什么）、最小修复建议。区分“已复现”和“仅推测”。
