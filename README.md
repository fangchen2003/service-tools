# NAI Gate —— NovelAI 公益分发网关

把你的 NovelAI Opus 订阅安全地分发给他人使用的**自建中转站**。

```
用户A(nai-xxx) ─┐
用户B(nai-yyy) ─┼─→  NAI Gate（你的服务器）        NovelAI
用户C(nai-zzz) ─┘    ├─ 鉴权：只认虚拟 Key          api.novelai.net
                     ├─ 限流：RPM / 并发 / 排队   ←─ 真实 Token 只存服务端
                     ├─ 配额：V5/Anlas/日tokens
                     ├─ 钳制：未开通 Anlas 的普通 Key 按免费条件调整
                     └─ 记账：每次调用入库，后台可视化
```

- 别人拿到的是你签发的**虚拟 Key**（`nai-xxxx`），你的真实 NovelAI Token 永远不出服务器
- 四层防护：每分钟请求数 → 单 Key 并发 → 全站并发 → 按日/按月配额
- 默认开启**安全钳制**：未开通 Anlas 的普通 Key 强制单张、最多 28 步、关闭 SMEA；V4.5 按像素面积 ≤1024×1024 判断免费尺寸，V5 按预设边界判断。开通 Anlas 的普通 Key 按预算限额使用
- 内置中文管理后台：发 Key、改配额、看用量日志、站点公告

---

## ⚠️ 先读我：风险与合规

**NovelAI 服务条款（5.3 / 9.1）明确禁止账号共享**，官方文档原话："Any request using your access token should be done by the owner of the account"，违规后果是**账号永久封禁或免费生成权限被暂停**。分发式使用属于灰色地带，请自行评估并采取以下缓解措施：

1. **用小号，不要用主号**：注册一个单独账号开 Opus 专门做公益站，封了不心疼；主号留着自己用
2. **控制总并发**：`GLOBAL_CONCURRENCY` 保持 1，模拟"单人在用"的行为特征（本项目默认值就是 1）
3. **不要挂公网大流量**：公益站小圈子分发即可，Writeup 里那种"人人可注册"的大站风险完全不同
4. Token 泄露 = 账号失守：服务器上锁好 `.env` 权限，后台密码用强密码

---

## 部署

### 方式一：Docker Compose（推荐）

```bash
git clone <本仓库> nai-gate && cd nai-gate
cp .env.example .env
vim .env          # 至少改 ADMIN_PASSWORD 和 NAI_TOKENS 两项
docker compose up -d --build
curl http://127.0.0.1:8000/healthz   # {"ok":true,...} 即成功
```

### 方式二：手动运行（Python ≥ 3.11）

```bash
pip install -r requirements.txt
export ADMIN_PASSWORD='你的后台密码'
export NAI_TOKENS='pst-你的持久Token'
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> 注意：限流器是进程内存实现，**必须单 worker 运行**（默认就是）。要上 HTTPS 请用 Nginx/Caddy 反代。

### 获取 NovelAI 持久 Token

1. 登录 [novelai.net](https://novelai.net)
2. 左侧边栏齿轮 → **User Settings** → **Account** 标签
3. 点击 **Get Persistent API Token**，复制（`pst-` 开头）
4. 填入 `.env` 的 `NAI_TOKENS=`（多个 token 用英文逗号分隔即组成令牌池，网关会轮询使用并在 429 时自动退避）

---

## 站长上手

1. 打开 `https://你的域名/admin`，用 `ADMIN_PASSWORD` 登录。默认要求 HTTPS；使用 HTTP 时，在 `.env` 设置 `ADMIN_COOKIE_SECURE=0`，运行 `docker compose up -d --force-recreate` 生效。HTTP 会明文传输登录信息。
2. **密钥管理 → 新建 Key**：先选图片模型范围（默认“仅 V4.5 及更低”；需要 V5 才选“所有当前支持模型”），再设置每日 V5、每日/月 Anlas、每日文本 tokens、RPM、有效天数
   - `允许消耗 Anlas`：默认关。关闭时，任何会花 Anlas 的请求直接被拒（402），双保险
   - `允许 img2img / 局部重绘`：默认关。该功能必烧 Anlas
3. 把生成的 `nai-xxxx` 发给用户，同时把你的站点地址发给他
4. **总览**页看全局用量与上游令牌池健康状态；**用量日志**页可按 Key 审计每一次调用（含被钳制的参数说明）
5. **设置**页可编辑首页公告（告诉用户怎么申请 Key、使用规则）

密钥管理支持“重新生成 Key”：旧 Key 失效，配置和历史用量保留，已受理任务继续处理。重置今日额度保留月度消费；删除 Key 保留历史用量。

## 用户接入方式

虚拟 Key 用法与官方一致，把 `Authorization: Bearer <token>` 换成虚拟 Key、API 地址换成你的站点即可。

**① SillyTavern（酒馆）/ OpenAI 兼容客户端 —— 文本续写**

- API 类型：`自定义(OpenAI 兼容) / Custom (OpenAI-compatible)`
- Endpoint：`http://你的站点:8000/v1`
- Key：`nai-xxxx`
- 模型：`llama-3-erato-v1` / `kayra-v1` / `nai-glm-4-6` 等（`GET /v1/models` 可查）

**② 原生 NovelAI 客户端（支持自定义 API 地址的工具，如 ComfyUI 的 NAI 节点等）**

图片工具现支持 `POST /ai/upscale` 和 `POST /ai/augment-image`（也支持 `/nai/ai/` 前缀）。
两者沿用 Key 的 `allow_img2img` 和全局图片编辑开关；涉及 Anlas 时，还需 Key 与上游 Token 均允许付费，并通过日/月/全站限额。

- 放大：传入 Base64 `image`；可选 `width`、`height` 须与原图一致。支持 `nai-diffusion-5-curated`、`declared_blur_sigma=0` 和 `scale=2`。输入像素不超过 1048576 / 1747627 / 2446678 / 3145728 时，分别记 1 / 2 / 3 / 4 Anlas。
- 导演工具：`req_type` 支持 `bg-removal`、`lineart`、`sketch`、`colorize`、`emotion`、`declutter`、`declutter-keep-bubbles`。上色与表情接受 `prompt` 和整数 `defry`（0–5）；表情提示词采用官方 `表情;;补充提示词` 格式。
- 输入仅接受静态 PNG/JPEG/WebP，最多 3145728 像素；服务端解码核验尺寸。导演工具的小图会像官网一样先放大到 Normal 尺寸。Opus 下普通导演工具在 1048576 像素以内免费；大图按 28 步 V3 公式估算，去背景按该付费基价的 3 倍加 5（常规尺寸 65 Anlas）。结果可能是单张图片或 ZIP；去背景返回三张。
- 图片工具按 Opus 规则估算费用，成功取得有效结果后记账。失败或超时返回错误，此时官方扣费状态可能未知。

- 图片：`POST http://你的站点:8000/ai/generate-image`
- 图片流式预览：`POST http://你的站点:8000/ai/generate-image-stream`，返回 SSE，按完整最终图片结算
- Vibe 编码：`POST http://你的站点:8000/ai/encode-vibe`，支持 V4/V4.5，成功编码记 2 Anlas；V3 直接使用原图，V5 不支持 Vibe
- 文本流式：`POST http://你的站点:8000/ai/generate-stream`
- 语音：`POST http://你的站点:8000/ai/generate-voice`
- 使用官方请求格式，将 base URL 改成站点地址；支持范围和参数限制见上文。

> **NAI Launcher v3**：第三方地址填写站点根地址，如 `https://your-domain.example`。支持 Bearer 登录、multipart 生图、GET 标签建议和 SSE 预览。用户视图显示当前 Key 的权限与额度；`GET /queue-status` 显示汇总排队状态。

订阅接口在 `naiGate` 字段中返回本地 V5 日配额，省略表示官方电量的可选 `usage` 字段。

V4.5 精准参考每张参考图、每张输出额外记 5 Anlas；V4/V4.5 Vibe 超过 4 张的部分，每张参考图、每张输出额外记 2 Anlas。编码单独计费；精准参考与 Vibe 二选一。

图片请求串行执行并结算。流式请求排队时断连会取消；发送上游后，即使客户端断连也继续处理结果。

**③ curl 示例**

```bash
# 生图（V4.5 免费尺寸示例）
curl -X POST http://站点:8000/ai/generate-image \
  -H "Authorization: Bearer nai-xxxx" -H "Content-Type: application/json" \
  -d '{"input":"1girl, best quality","model":"nai-diffusion-4-5-full","action":"generate",
       "parameters":{"width":832,"height":1216,"steps":28,"n_samples":1,"sampler":"k_euler_ancestral"}}'
# 返回 zip（内含 png），与官方一致

# 查询自己的剩余额度
curl http://站点:8000/v1/me -H "Authorization: Bearer nai-xxxx"
```

---

## 2026-08 V5 发布后的额度现实（重要）

官方在 V5 发布时调整了政策（[官方博客](https://blog.novelai.net/subscription-updates-usage-limits-subscription-anlas-policy-adjustments-88a208d5d9c5)）：

| 资源 | 规则 |
|---|---|
| **V4.5 及更老模型** | Opus 下「单张 / 纯文生图 / ≤28 步 / 像素面积 ≤1024×1024 / 无 SMEA 及付费附加功能」→ **0 Anlas 不限量**，支持自定义长宽比 |
| **V5 (nai-diffusion-5)** | **不再有无限免费档**。Opus 获得独立的「V5 周额度」：约 1800 张/周，服务端按 ~0.5%/小时（约 190 张/天）自动恢复；**账户内所有人共享**。实测三个 Normal 预设（竖屏 832×1216 / 横屏 1216×832 / 方形 1024×1024）及更小尺寸、≤28 步、单张 → 走周额度**不扣 Anlas**；额度外或更大尺寸按 Anlas 计费，实测 Small≈11A、Normal≈26A、Large≈39A（约为 V4 价格的 1.3 倍）。网关按**预设判定**（逐维 ≤ 任一预设），面积达标但非预设的自定义尺寸（如 896×1152）按额度外计费——宁紧勿松，防止真实 Anlas 被意外扣掉 |
| **Anlas** | 每月账单日**回满**到 10000（不叠加、不是每天恢复）；31 天后取消订阅时余额清零 |

符合周额度条件的 V5 请求会按需查询所用账号的官方余量。正常正余量缓存 5 分钟，低余量缓存 1 分钟；官方明确标记耗尽（`usage.isNegative=true`）后，按 Anlas 估算并检查付费权限和预算。查询失败则拒绝本次生成，至少 1 分钟后可重试；遇到官方 429 至少等待 5 分钟。

后台展示缓存状态，低余量告警阈值默认 20%，可设为 1～100%。告警持续至查询确认恢复。其他客户端的使用可能使缓存过时；本站费用为估算值，全站及各 Key 日限为本地配额。

## 推荐配额（2~3 人规模）

按「每月回满 10000、自己留 ~2500」计算，可送出约 7500 Anlas/月：

| 配额项 | 3 人规模（每人） | 2 人规模（每人） | 说明 |
|---|---|---|---|
| 每日 V5 | 50 | 60 | 走官方周额度，不扣 Anlas |
| **每日 Anlas** | **80~100** | **120~150** | ≈3~4 张 V5 Normal 或 2~3 张 Large/天 |
| 每月 Anlas | 2300~2500 | 3400~4000 | 日限×30 应 ≤ 月限 |
| 全站月预算 | 7500 | 8000 | 后台「设置」里改 |
| 全站每日 V5 | 150 | 150 | 官方恢复速率 ~190/天 |

> 记住恒等式：**日限只是节奏控制，真正守恒的是月度**——`每日Anlas × 30 ≤ 每月Anlas ≤ 你愿意送的份额`。V5 很贵（Normal 26A），预算主要被 V5 吃掉；让朋友们日常用 V4.5 免费档 + 每天几张 V5，是最可持续的分法。

## 防爆费机制（重点）

V4.5 免费生图的条件是**同时满足**：单张（`n_samples=1`）、纯文生图（无底图/蒙版/ControlNet/角色参考）、`steps ≤ 28`、像素面积 ≤ 1024×1024、不开 SMEA。**尺寸不必等于标准预设**：例如 896×1152，官方实测为 0 Anlas，网关保留原尺寸并按免费记账。V5 仍按上文的预设边界和官方额度状态单独判断。

| 防线 | 行为 |
|---|---|
| 安全钳制（默认开） | 仅作用于未开通 Anlas 的普通 Key：`n_samples` 强制 1、`steps` 默认最多 28、关闭 SMEA；超出免费尺寸条件时吸附到 Normal 预设 |
| `allow_anlas` 权限 | 即便钳制被管理员关闭，普通 Key 没开通付费权限时，估算消耗 > 0 的请求一律 402 |
| 每日 Anlas 上限 | 开通 Anlas 的 Key 可设**每天最多花多少 Anlas**（0=不限），隔天自动恢复 |
| 月度 Anlas 配额 | 每把 Key 独立月度 Anlas 上限，按（社区逆向公式的）**估算值**预检+记账，超了 402 |
| 全站月度预算 | 普通 Key 共享的预算上限（默认 10000，后台可改），约束每月 Anlas 估算消耗 |

**关于"适当供给 Anlas"**：勾选 `允许消耗 Anlas` 的 Key **不再受免费档钳制**（否则永远花不出去），可以出大图、多张、img2img、ControlNet，但每一单都会被三道闸约束：`每日 Anlas` → `每月 Anlas` → `全站月度预算`。Opus 每月续期会把上月花掉的 Anlas 返还（上限 10000），所以建议：全站预算 ≈ 你愿意送出去的量，并留 20~30% 余量（估算公式与官方有偏差）。免费 Key 与 Anlas Key 可以混发，普通用户走免费档，信任的用户给 Anlas 额度。

文本生成对订阅用户不限量（只受单账号速率限制），网关按 **tokens/天** 限额，流式输出到达上限会**硬切断**并返回 `[DONE]`。

## 环境变量

见 [.env.example](.env.example)，全部有注释。常用的几项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_PASSWORD` | 无（后台锁定） | 管理后台密码 |
| `ADMIN_COOKIE_SECURE` | `1` | 后台 Cookie 仅用于 HTTPS；明确需要 HTTP 时设为 `0`，重新创建容器生效 |
| `CORS_ORIGINS` | `*` | 允许跨域的来源，以英文逗号分隔；空值关闭跨域中间件，不允许跨域 Cookie |
| `NAI_TOKENS` | 无 | 上游 Token，逗号分隔多个组成令牌池 |
| `GLOBAL_CONCURRENCY` | 1 | 全站并发（对上游的保护，**别调大**） |
| `KEY_CONCURRENCY` | 1 | 单 Key 并发 |
| `SAFE_CLAMP` | 1 | 安全钳制开关（对开通 Anlas 的 Key 不生效） |
| `ALLOW_IMG2IMG` | 0 | img2img 总开关（还需 Key 勾选 allow_img2img） |
| `GLOBAL_MONTHLY_ANLAS` | 10000 | 全站月度 Anlas 预算（后台可改） |
| `GLOBAL_DAILY_V5` | 150 | 全站每日免费 V5 张数上限；官方余量在生成时按需查询 |
| `KEY_INACTIVITY_DELETE_DAYS` | 3 | 超过天数未使用的 Key 自动永久删除；`0` 关闭 |
| `DEFAULT_*` | 见配置示例 | 新 Key 默认配额、RPM 和有效期，后台可逐 Key 覆盖 |
| `TZ` | Asia/Shanghai | 配额按天重置的时区 |

## 日常运维

- **上游 Token 失效**：总览页令牌池变红（401）→ 去官网重新生成 Token → 改 `.env` → `docker compose up -d` 重启
- **上游 429**：对应 Token 按 `Retry-After` 冷却（缺省 20 秒）；图片请求返回错误并触发全站图片冷却，不自动重试。其他普通请求最多换 Token 重试一次
- **数据备份**：所有状态在 `data/nai_gate.db`（SQLite，WAL 模式），定期拷走即可
- **审计**：用量日志保留每次调用的 Key、类型、模型、状态、消耗和钳制说明

## 本地开发与测试

```bash
pip install -r requirements.txt pytest pytest-asyncio tzdata
python -m pytest tests/ -q                 # 策略、鉴权、账本、流式与图片工具回归测试

# 起一个假上游做全链路联调（不访问真实 NovelAI）
python -m uvicorn tests.mock_nai:app --port 9999 &
NAI_TOKENS=fake NAI_IMAGE_HOST=http://127.0.0.1:9999 \
NAI_TEXT_HOST=http://127.0.0.1:9999 NAI_TEXT_HOST_LEGACY=http://127.0.0.1:9999 \
ADMIN_PASSWORD=test python -m uvicorn app.main:app --port 8000
```

测试使用假上游和临时数据库，不消耗真实 NovelAI 额度。`tzdata` 为 Windows 提供时区数据；生产 Linux 镜像使用系统时区数据库。

## FAQ

**Q：Key 能创建多少个？会互相抢额度吗？**
A：数量不限，后台随时创建/禁用/删除。普通 Key 各有独立配额，同时受全站并发（默认 1）、全站月度 Anlas 预算和全站每日 V5 上限约束；指定 Key 可豁免全站 V5 上限。

**Q：限额是怎么实现的？**
A：RPM 用 60 秒滑动窗口（内存）预检；每日 V5/每日 Anlas/每日文本 tokens 按 `(Key, 日期)` 记在 SQLite，按配置时区计算当日用量；文本是流式逐 token 计数、到上限硬切断；并发用「单 Key + 全站」两层信号量排队。**只有上游成功返回才记账**，失败请求不扣配额。

**Q：会影响我自己的使用吗？**
A：这正是架构要解决的问题——**强烈建议用小号**，物理隔离，主号零影响。如果坚持用主号 Token，把 `GLOBAL_CONCURRENCY` 设为 1 能保证不会和用户并发冲突，但配额共享、风险共担。

**Q：用户会用我的号看我的故事/图片吗？**
A：虚拟 Key 无法读取账号的云端故事或其他用户的图片，网关不开放 `/user/objects` 等云端存储接口。生成请求和结果由网关处理，服务器可接触明文内容；用量日志记录调用信息，不保存生成正文或图片。

**Q：为什么不直接把官方 Token 给用户？**
A：官方 Token = 整个账号（包括余额、云存储、改密码前的所有权限），且无法限额。虚拟 Key 只能走生成接口，且配额、期限、功能权限全由你控制。

**Q：Opus 的文本不是无限吗，为什么还要限 tokens？**
A：无限指的是不限总量，但单账号有速率限制，且你的服务器带宽也有限。按 tokens 限额是为了防止单个用户把全站并发占满。
