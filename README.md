# NAI Gate

一个自托管的 NovelAI 生成网关。站长在服务端配置上游 Token，给用户发放独立的虚拟 Key，并通过管理面板控制模型权限、用量和请求频率。用户不需要接触上游 Token。

支持 NovelAI 图片与文本接口，以及用于文本聊天的 OpenAI 兼容接口。图片生成结果直接返回给请求方，服务不会保存新生成的图片。

> 共享账号仍有被上游限流或封禁的风险；本项目的限额和冷却只能降低风险，不能保证账号安全。请自行确认使用方式符合上游要求，并妥善保管 `.env` 和数据库。

## 快速部署

需要 Docker Compose。在项目目录执行：

```bash
cp .env.example .env
# 编辑 .env，至少设置强 ADMIN_PASSWORD 和真实 NAI_TOKENS
# 检查 docker-compose.yml 的 ports；公网部署建议设为 "127.0.0.1:3003:8000"
docker compose up -d --build
curl http://127.0.0.1:3003/healthz
```

上面的检查很重要：如果 [docker-compose.yml](docker-compose.yml) 里仍是 `8000:8000`，服务会直接暴露在所有网卡上，示例健康检查端口也需相应改成 `8000`。建议先改为本机绑定，再用 Nginx/Caddy 配置 HTTPS 反向代理。若 `3003` 被占用，可换一个可用的主机端口。管理后台默认只接受 HTTPS Cookie。

健康检查返回 `{"ok":true,"upstream":true}` 表示服务已启动且配置了上游 Token。打开 `https://你的域名/admin` 登录管理面板。

## 站长怎么用

1. 在“密钥管理”创建虚拟 Key，选择“仅 V4.5 及更低”或“所有当前支持模型（含 V5）”，按需开放 Anlas、设置配额。
2. 把站点根地址和生成的 `nai-...` Key 发给用户。
3. 在“总览”查看全站和上游用量；可单独停用/启用上游 Key，修改其免费 V5 日限额及图片并发（每把 1～4，默认 1）。在“设置”可调整排队超时、图片任务间隔和上游 429 冷却。用量日志支持按 Key 筛选。
4. 在“总览 → Anlas 消耗核对”查询官方余额，与本站记录的消耗比较；首次查询保存当前余额。

首次配置多把上游 Token，可用英文逗号写入 `NAI_TOKENS`。`NAI_TOKEN_ALLOW_ANLAS=1,0` 按顺序指定哪些账号允许消耗 Anlas；`NAI_TOKEN_V5_DAILY_LIMITS=0,150` 设置初始免费 V5 日限额，`0` 表示不限。

“设置 → 上游账号”支持添加、更换、移除 Token，以及调整 Anlas 权限。更换或移除前先停用该账号，并等待任务和额度预留结束。更换 Token 会保留该账号的用量、限额和并发设置；余额核对会从新凭据重新建立比较记录。保存只检查输入格式，不会向 NovelAI 发起验证或生成请求。

首次使用后台管理账号后，完整账号列表保存在数据目录的 `upstream_tokens.json`，重启时优先于 `NAI_TOKENS`；删除全部账号后也不会重新启用环境变量中的旧 Token。原始 Token 不写入数据库，也不会通过管理接口回显。该文件包含凭据，使用受限文件权限；应与 `.env` 一样妥善保管，备份数据目录时一并保护。

“设置”按预算、生成、上游账号、Key 默认、自动清理、高级、公告、生效配置分组。后台保存的服务配置优先于环境变量并持久保留；数值限制、功能开关、并发和文本限制对后续请求生效，并发降低不会中断已在执行的任务。Key 默认模板仅影响之后由管理员创建的 Key，不修改已有 Key 或 Discord 自助注册配额。自动清理可先查看候选名单，保存不会立即执行清理；开启后按既有启动及每小时检查执行，`0` 天关闭。“生效配置”显示实际值及来源。后台登录相关设置仍按原有配置管理。

## 用户怎么接入

所有生成请求使用 `Authorization: Bearer nai-...`。支持自定义 NovelAI API 地址的客户端填写站点根地址，例如 `https://你的域名`；OpenAI 兼容文本客户端填写 `https://你的域名/v1`。

常用接口：

| 用途 | 接口 |
| --- | --- |
| 图片生成 / 流式预览 | `POST /ai/generate-image` / `POST /ai/generate-image-stream` |
| NovelAI 文本 / 语音 | `POST /ai/generate-stream` / `POST /ai/generate-voice` |
| OpenAI 兼容文本 | `GET /v1/models`、`POST /v1/chat/completions` |
| 查询当前 Key 的额度 | `GET /v1/me` |

图片生成、放大、导演工具和 Vibe 编码支持 JSON 或 multipart（`request` JSON 加图片附件）。两种格式使用相同的权限与额度检查。

图片流通过 `parameters.stream` 选择 `sse` 或 `msgpack`，默认 `sse`。MessagePack 每帧由 4 字节大端长度和对应的消息体组成。

图片生成示例（返回与官方接口一致的图片结果）：

```bash
curl 'https://你的域名/ai/generate-image' \
  -H 'Authorization: Bearer nai-你的虚拟Key' \
  -H 'Content-Type: application/json' \
  -d '{"input":"1girl, best quality","model":"nai-diffusion-4-5-full","action":"generate","parameters":{"width":832,"height":1216,"steps":23,"n_samples":1}}'
```

第三方 NovelAI 客户端需要支持自定义站点地址和 Bearer Key；本站提供 `/user/subscription` 等兼容接口，但并不保证兼容所有客户端。

## 默认限制

下面是新 Key 的默认值，站长可在面板调整；已有 Key 以各自保存的设置为准。

| 项目 | 默认值 |
| --- | ---: |
| V4.5 及以下免费生图 | 每 Key 每天 100 张 |
| 免费 V5 | 每 Key 每天 50 单位；全站每天 150 单位 |
| Anlas（须单独授权） | 每 Key 每天 100、每月 2500 |
| 文本输出 | 每 Key 每天 150,000 tokens |
| 请求频率 | 每 Key 每分钟 10 次 |

免费 Key（未开通 Anlas）的 V5 最多 23 步，V4.5 及以下最多 28 步，关闭 `SAFE_CLAMP` 后仍适用；`MAX_STEPS` 默认 28，可进一步降低免费 Key 的上限。已开通 Anlas 的 Key 和管理员 Key 不强制钳制步数。V5 High 首张占 1 单位；Medium 默认占 0.60 单位，累计图片数仍按实际张数统计。同批其他图片仍按 Anlas 计费。

“设置”中可调整 V5 自动节省开关、剩余额度阈值（默认 20%）和 Medium 倍率（默认 0.60）。免费 Key 的 V5 Full 纯文生图在全站今日剩余额度不高于阈值时自动切到 `nai-diffusion-5-full-medium`；进行中的额度预留也参与判断。Curated、已开通 Anlas 的 Key 与管理员 Key 不自动切换。Medium 固定 14 步、Euler Ancestral、Heavy 负面预设，自定义负面词和 CFG Rescale 不生效，手动选择 Medium 时也适用；日志记录最终模型、参数调整和扣除额度。用户、全站、每把上游的 V5 日额度采用相同折算；它是本站预算估算，不是官方精确计费。历史 V5 使用按每次 1 单位迁移，图片张数不变；上游实际额度、Anlas 权限和 429 保护继续生效。

`/v1/me` 的 `image_policy.max_steps_by_tier` 分别返回 `legacy` 和 `v5` 的步数上限；兼容字段 `max_steps` 返回两类中的最大上限。付费 Key 和管理员 Key 的这些值均为 `null`。

### 只读图片报价

`POST /v1/image/quote` 使用与生成接口相同的 Bearer Key。请求包含 `operation`（`generate-image`、`upscale`、`augment-image` 或 `encode-vibe`）与对应接口的原始 `payload`。例如：

```json
{"operation":"generate-image","payload":{"model":"nai-diffusion-5-full","action":"generate","parameters":{"width":832,"height":1216,"steps":28,"n_samples":1}}}
```

响应包含 `anlas`、`v5`、`generation_anlas`、`encoding_anlas`、`effective_model`、`effective_parameters`、`notes` 及 `cost_limit`。报价与生成共用步数/尺寸调整、Medium 路由、参考附加费和工具计费代码。需要编码的 V4/V4.5 参考图可放在原始参考数组中，并通过 `pending_vibe_indices` 指定索引；相同原图及信息提取值只计一次编码费，生成附加费仍按参考条目数计算。

`GET /v1/image/quote` 只验证 Key 并返回用户名，供适配服务在解析大图片前鉴权。两种报价请求都不更新 Key 使用时间、不创建计数或日志、不预留额度、不排队生图，也不请求官方账号。并行报价数量受限，繁忙时返回 429。

`cost_limit` 是本次生成的 `{ "anlas": 0, "v5": 0.6 }` 形式上限；Vibe 编码使用单独返回的 `encoding_cost_limit`。客户端确认费用后，将相应上限放到实际生成/图片工具/编码请求的顶层。Gate 会在派发前复核，包括排队后的 V5 状态变化；超出任一上限返回 409，不发送该次生成。未携带上限的已有客户端沿用原有行为。上游 V5 可用额度仍由真实生成在派发前核验，报价中的 `upstream_allowance_verified` 为 `false`。

Medium 的 Anlas 基础公式在取整前对步数项应用官网的 `1 / 1.06521739` 系数；它与站内 V5 额度的 0.60 倍率独立。相同 1408×2176、14 步、强度 0.5 的图生图，High 预计 26 Anlas、Medium 预计 25 Anlas。

### Max Enhance

V5 Full、Curated 与 Full Medium 的单张图生图可使用 `parameters.upscaled_enhance=true`。请求的宽高必须保留原图实际尺寸；不能与 mask、嵌套 img2img 或另一种放大叠加。Gate 校验源图，并按照最高 2 倍、3,145,728 像素和官网对齐规则分别计算计费尺寸与预期输出尺寸；报价分别返回 `billing_size`、`output_size`。免费资格也按增强后的计费尺寸判断。例如 832×1216、28 步、强度 0.5：计费尺寸 1440×2144，预期输出 1467×2144，预计 45 Anlas。此处为参数报价，真实生成结果仍由上游返回。

图片任务在同一用户 Key 与同一上游 Token 上默认各至少间隔 15 秒，等待时会自动排队；每个排队阶段默认最多等待 90 秒。每把上游 Token 的图片并发默认 1，可在面板单独调整；提高并发会增加上游限流风险。上游图片请求触发 429 后，全站图片生成默认至少冷却 60 秒；若上游要求更长时间，则以更长者为准。以上时间可在面板调整并持久保存。免费图与付费图分开计数；流式部分成功按已完成张数结算。完整规则见[用户限制说明.md](用户限制说明.md)。

## 配置与数据

完整环境变量见 [.env.example](.env.example)。特别注意：

- `ADMIN_PASSWORD`、`NAI_TOKENS` 必须自行设置；不要提交 `.env`。
- `KEY_INACTIVITY_DELETE_DAYS` 默认 `3`，长期未使用的普通 Key 会被永久删除；设为 `0` 可关闭，后台保存的自动清理设置优先生效。它与 Key 到期时间不同。
- `ADMIN_COOKIE_SECURE` 默认开启。仅在可信的本地 HTTP 调试环境才考虑设为 `0`。
- 本地状态保存在 `data/nai_gate.db`；备份运行中的 SQLite 数据库时请使用 SQLite 的在线备份功能。历史日志可能包含用量与请求元数据，应限制访问。

用量日志记录 Key、模型、状态和消耗，不保存生成的图片或提示词。历史版本遗留的文件不会因升级自动删除。

本地测试：安装 `requirements.txt`、`pytest` 和 `pytest-asyncio`，然后执行 `python -m pytest -q`。测试使用假上游，不消耗真实 NovelAI 额度。
