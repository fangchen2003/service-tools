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

1. 在“密钥管理”创建虚拟 Key，选择“仅 V4.5 及更低”或“所有当前支持模型（含 V5）”，设置免费图、V5 用量和请求频率。普通用户生图始终仅允许免费规格，没有 Anlas 付费权限或数值预算可开放。
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
| Vibe 编码 | 通过 Discord `/vibe` 领取，每 Key 每天最多成功编码 3 次，北京时间零点恢复 |
| 文本输出 | 每 Key 每天 150,000 tokens |
| 请求频率 | 每 Key 每分钟 10 次 |

普通用户生图强制免费档，不受历史 `allow_anlas` 标记或 `SAFE_CLAMP=0` 影响：V4.5 及以下最多 28 步，V5 High 最多 28 步、Medium 固定 14 步，多张强制为单张，SMEA 关闭，超免费尺寸自动调整。img2img、局部重绘、ControlNet、精确参考及超过 4 个已编码 Vibe 不能安全转换，直接拒绝；V5 官方额度耗尽时拒绝，不回退 Anlas 付费。管理员和内部服务 Key 保留豁免，但仍受上游 Token 的付费权限与安全保护约束。V5 High 每张占 1 单位；Medium 默认占 0.60 单位，累计图片数仍按实际张数统计。

普通用户的日/月/全站 Anlas 数值预算已停用；旧数据库字段保留兼容，Anlas 用量仍用于审计，不能解释为付费余额。Vibe 编码是单独的每日 3 次授权：成功编码实际消耗上游 2 Anlas，失败不扣本地次数，使用已编码 Vibe 不重复计编码次数。`/vibe` 上传参考图后获取可导入工作台的文件；V4/V4.5 每次生图最多使用 4 个已编码参考，V3 使用原图，V5 不支持 Vibe。免费 Director 工具在图片处理权限开放时可使用；普通用户的付费超分和去背景等工具拒绝。

“设置”中可调整 V5 自动节省开关、剩余额度阈值（默认 20%）和 Medium 倍率（默认 0.60）。普通 Key 的 V5 Full 纯文生图在全站今日剩余额度不高于阈值时自动切到 `nai-diffusion-5-full-medium`；进行中的额度预留也参与判断。Curated 与管理员 Key 不自动切换。Medium 固定 14 步、Euler Ancestral、Heavy 负面预设，自定义负面词和 CFG Rescale 不生效，日志记录最终模型、参数调整和扣除额度。用户、全站、每把上游的 V5 日额度采用相同折算；它是本站预算估算，不是官方精确计费。历史 V5 使用按每次 1 单位迁移，图片张数不变；上游实际额度、Anlas 权限和 429 保护继续生效。

TAG 补全不受用户或上游图片任务的 15 秒间隔限制，也不占用下一次生图的时间窗；仍受并发、排队超时和真实上游 429 冷却保护。

图片任务在同一用户 Key 与同一上游 Token 上默认各至少间隔 15 秒，等待时会自动排队；每个排队阶段默认最多等待 90 秒。每把上游 Token 的图片并发默认 1，可在面板单独调整；提高并发会增加上游限流风险。上游图片请求触发 429 后，全站图片生成默认至少冷却 60 秒；若上游要求更长时间，则以更长者为准。以上时间可在面板调整并持久保存。免费旧模型图、V5 用量及 Anlas 审计分开统计；失败生成不计成功图片数。完整规则见[用户限制说明.md](用户限制说明.md)。

`/v1/me` 的 `image_policy.max_steps_by_tier` 分别返回 `legacy` 和 `v5` 的步数上限；兼容字段 `max_steps` 返回两类中的最大上限。本站普通 Key 无论历史 Anlas 标记如何均最多 28 步，只有管理员 Key 返回 `null`；Medium 固定 14 步。

### 只读图片报价

`POST /v1/image/quote` 使用与生成接口相同的 Bearer Key。请求最多 4 KiB，顶层只接受 `operation`、`model`、`parameters`。报价只接收计价元数据，不接受图片、蒙版、Vibe 编码、提示词或旧的 `payload` 格式。例如：

```json
{"operation":"generate-image","model":"nai-diffusion-5-full","parameters":{"mode":"generate","width":832,"height":1216,"steps":28,"n_samples":1}}
```

`generate-image` 必填整数 `width`、`height`（64–8192）和正整数 `steps`；`mode` 为 `generate`（默认）、`img2img` 或 `inpaint`。可选字段：`n_samples`（正整数，默认 1），`strength`、`inpaint_strength`（0–1，默认 1），`sm`、`sm_dyn`、`upscaled_enhance`、`has_controlnet`（布尔，默认 `false`），`vibe_count`、`precise_count`、`pending_vibe_encoding_count`（0–16 的整数，默认 0）。模型 ID 使用实际生成请求的 ID，包括重绘后缀。Max 增强的宽高为规范化后的源图尺寸，其余生成使用目标尺寸。

`upscale` 只接收源图 `width`、`height`，模型固定为 `nai-diffusion-5-curated`。`augment-image` 接收源图 `width`、`height` 和 `req_type`。`encode-vibe` 的 `parameters` 为 `{}`，模型限 V4/V4.5。工具仍遵循至少 64 像素、最多 3,145,728 像素的源尺寸限制。

响应包含 `anlas`、`v5`、`generation_anlas`、`encoding_anlas`、`effective_model`、`effective_parameters`、`notes` 及 `cost_limit`。报价与生成共用本站强制免费策略、步数/尺寸调整、Medium 路由、参考附加费和工具计费代码。历史 `allow_anlas` 或关闭 `SAFE_CLAMP` 不会开放付费生成；普通 Key 仍为 Legacy 28 / High 28 / Medium 14 步，V5 耗尽不回退付费。V4/V4.5 的 `pending_vibe_encoding_count` 是客户端按原图、模型和信息提取值去重后的待编码数量，不能超过 `vibe_count`；服务端不接收原图或 `pending_vibe_indices`，生成附加费仍按参考条目数计算。普通 Key 最多使用 4 个已编码 Vibe，不能使用精准参考。报价检查已领取的 `/vibe` 机会并返回 `vibe_required`、`vibe_remaining`，但不预扣；真实编码原子消费一次机会，每个上海自然日可领取一次、共 3 次编码机会。编码 Anlas 金额仅表示实际成本审计，不是普通生图付费余额。Vibe 与精准参考不能混用。

`GET /v1/image/quote` 只验证 Key 并返回用户名。两种报价请求都不更新 Key 使用时间、不创建计数或日志、不预留额度、不排队生图，也不请求官方账号。报价没有单独的并发槽位限制。

`cost_limit` 是本次生成的 `{ "anlas": 0, "v5": 0.6 }` 形式上限；Vibe 编码使用单独返回的 `encoding_cost_limit`。客户端确认费用后，将相应上限放到实际生成/图片工具/编码请求的顶层。真实请求仍发送完整图片和参数，Gate 验证图片及参考数据并从真实请求重新计算费用，不信任其中附带的报价元数据字段。派发前会复核费用上限，包括排队后的 V5 状态变化；超出任一上限返回 409，不发送该次生成。未携带上限的已有客户端沿用原有行为。上游 V5 可用额度仍由真实生成在派发前核验，报价中的 `upstream_allowance_verified` 为 `false`。

Medium 的 Anlas 基础公式在取整前对步数项应用官网的 `1 / 1.06521739` 系数；它与站内 V5 额度的 0.60 倍率独立。相同 1408×2176、14 步、强度 0.5 的图生图，High 预计 26 Anlas、Medium 预计 25 Anlas。

### 图片提交队列

`POST /v1/image/queue` 使用 Bearer Key，最多接收 4 KiB 的 `{ "request_id": "UUID", "operation": "generate-image" }`，可另带布尔值 `requires_anlas`。操作还可为 `upscale`、`augment-image`、`encode-vibe`。付费标记用于匹配允许 Anlas 的上游容量，放大和编码始终按付费操作准入；实际计费仍由完整请求独立核验。队列只保存身份、请求 UUID、操作、付费标记和状态时间，不接收图片、提示词或费用。相同 Key 和 UUID 返回同一票据；更换操作或付费标记返回 409。每个 Key 最多有 50 个未完成票据，终态不占此上限；重复请求仍可取得已有票据。

响应包含 `ticket_id`、`request_id`、`operation`、`state`、`position`、`expires_in`、`poll_after`。状态依次为 `queued`、`ready`、`active`、`finished`，未提交的票据也可能变为 `cancelled` 或 `expired`。`queued` 的 `position` 是从 1 开始的未完成请求顺位，包含前面的已放行和正在执行请求；其他状态为 0。同 Key 或付费上游暂无容量时可先放行其他合格请求，这个顺位不承诺上游实际开始时间。派发时优先选择仍有空位、间隔等待较短的合格上游；条件相同时，免费任务先使用禁付费上游，其余沿用轮询。

`GET /v1/image/queue/{ticket_id}` 读取本人票据；`POST /v1/image/queue/{ticket_id}/heartbeat` 续租等待中的票据并返回状态；`DELETE /v1/image/queue/{ticket_id}` 取消尚未提交的票据。建议每 3 秒通过心跳获取状态。等待租期 120 秒，领取窗口最多 120 秒且不超过已有心跳租期，ready 后的心跳不会延长该窗口。终态保留 15 分钟用于去重；服务重启后内存队列不恢复。

轮到 `ready` 后，向原图片 API 上传完整任务，同时发送 `X-NAI-Gate-Queue-Ticket` 与 `X-NAI-Gate-Queue-Request`。Gate 在读取图片前原子领取票据，核对 Key、操作和请求 UUID；领取后的上传最多等待 30 秒。`active` 表示请求已开始提交，取消接口返回 409，不保证能撤回上游操作。执行仍验证真实图片、费用上限、额度、Key 并发及上游并发与间隔，直到结果及记账清理完成才释放准入名额；票据本身不预留或扣除费用。

未带票据的旧图片客户端继续可用，但也按同一队列准入，等待上限沿用 `queue_timeout`。旧非流式请求只在完整读取请求体后观察断线，尚未放行的断线请求会退出队列。Key 被禁用、删除、更换凭据或在鉴权时发现已禁用、过期，会取消其未领取票据；已领取任务仍由执行清理流程结束。Vibe 编码先单独排队完成，再排主生图；标签补全仅使用没有生图等待时的空闲准入。`GET /queue-status` 另外返回 `image_queue` 的 `queued`、`ready`、`active`、`total`、`capacity` 汇总。

队列错误同时提供顶层 `code` 和 `error.code`。`queue_ticket_not_ready` 可继续等待原票据；`queue_ticket_used` 表示已经领取，不能自动重复执行。`queue_ticket_expired`、`queue_ticket_cancelled`、`queue_ticket_not_found` 需要重新确认任务状态；未知结果不能自动重发。`queue_ticket_mismatch`、`queue_request_conflict`、`queue_invalid_request` 表示请求不匹配；`queue_full` 表示该 Key 达到 50 个未完成票据上限；`queue_timeout` 为旧客户端排队超时，`queue_request_disconnected` 为旧客户端在等待期间断线，`queue_upload_timeout` 为领取后上传超时，`queue_unavailable` 为标签暂无空闲名额。

### Max Enhance

本站普通 Key 不开放图生图或 Max Enhance；以下计价能力只供管理员/内部服务使用，报价与执行均保持相同限制。

V5 Full、Curated 与 Full Medium 的单张图生图可使用 `parameters.upscaled_enhance=true`。请求的宽高必须保留原图实际尺寸；不能与 mask、嵌套 img2img 或另一种放大叠加。Gate 校验源图，并按照最高 2 倍、3,145,728 像素和官网对齐规则分别计算计费尺寸与预期输出尺寸；报价分别返回 `billing_size`、`output_size`。免费资格也按增强后的计费尺寸判断。例如 832×1216、28 步、强度 0.5：计费尺寸 1440×2144，预期输出 1467×2144，预计 45 Anlas。此处为参数报价，真实生成结果仍由上游返回。

### 执行回执

生成和图片工具请求可发送 `X-NAI-Gate-Receipt: 1`。非流式成功响应在同名响应头返回 JSON；SSE/MessagePack 在结算后发送 `gate_receipt` 事件，最后一张最终图随后交付。未请求回执的客户端保持原协议。

回执包含实际派发的模型与核心参数、完成张数和 Gate 已记账的 `anlas`、`v5`、`legacy_free_images`，不包含图片、提示词或凭据。金额是 Gate 的记账值，不是官方余额实扣核对；报价仍用于提交前确认。

## 配置与数据

完整环境变量见 [.env.example](.env.example)。特别注意：

- `ADMIN_PASSWORD`、`NAI_TOKENS` 必须自行设置；不要提交 `.env`。
- `KEY_INACTIVITY_DELETE_DAYS` 默认 `3`，长期未使用的普通 Key 会被永久删除；设为 `0` 可关闭，后台保存的自动清理设置优先生效。它与 Key 到期时间不同。
- `ADMIN_COOKIE_SECURE` 默认开启。仅在可信的本地 HTTP 调试环境才考虑设为 `0`。
- 本地状态保存在 `data/nai_gate.db`；备份运行中的 SQLite 数据库时请使用 SQLite 的在线备份功能。历史日志可能包含用量与请求元数据，应限制访问。

用量日志记录 Key、模型、状态和消耗，不保存生成的图片或提示词。历史版本遗留的文件不会因升级自动删除。

本地测试：安装 `requirements.txt`、`pytest` 和 `pytest-asyncio`，然后执行 `python -m pytest -q`。测试使用假上游，不消耗真实 NovelAI 额度。
