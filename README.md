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
3. 在“总览”查看全站和上游用量；可单独修改每把上游 Token 的免费 V5 日限额。用量日志支持按 Key 筛选。

多把上游 Token 用英文逗号写入 `NAI_TOKENS`。可用 `NAI_TOKEN_ALLOW_ANLAS=1,0` 按顺序指定哪些上游 Token 允许付费；`NAI_TOKEN_V5_DAILY_LIMITS=0,150` 可设初始免费 V5 日限额，`0` 表示不限。管理面板保存的 V5 限额优先于环境变量，且跟随具体 Token，不受排序影响。Anlas 权限目前仍需在 `.env` 中设置。

## 用户怎么接入

所有生成请求使用 `Authorization: Bearer nai-...`。支持自定义 NovelAI API 地址的客户端填写站点根地址，例如 `https://你的域名`；OpenAI 兼容文本客户端填写 `https://你的域名/v1`。

常用接口：

| 用途 | 接口 |
| --- | --- |
| 图片生成 / 流式预览 | `POST /ai/generate-image` / `POST /ai/generate-image-stream` |
| NovelAI 文本 / 语音 | `POST /ai/generate-stream` / `POST /ai/generate-voice` |
| OpenAI 兼容文本 | `GET /v1/models`、`POST /v1/chat/completions` |
| 查询当前 Key 的额度 | `GET /v1/me` |

图片生成示例（返回与官方接口一致的图片结果）：

```bash
curl 'https://你的域名/ai/generate-image' \
  -H 'Authorization: Bearer nai-你的虚拟Key' \
  -H 'Content-Type: application/json' \
  -d '{"input":"1girl, best quality","model":"nai-diffusion-4-5-full","action":"generate","parameters":{"width":832,"height":1216,"steps":28,"n_samples":1}}'
```

第三方 NovelAI 客户端需要支持自定义站点地址和 Bearer Key；本站提供 `/user/subscription` 等兼容接口，但并不保证兼容所有客户端。

## 默认限制

下面是新 Key 的默认值，站长可在面板调整；已有 Key 以各自保存的设置为准。

| 项目 | 默认值 |
| --- | ---: |
| V4.5 及以下免费生图 | 每 Key 每天 100 张 |
| 免费 V5 | 每 Key 每天 50 张；全站每天 150 张 |
| Anlas（须单独授权） | 每 Key 每天 100、每月 2500 |
| 文本输出 | 每 Key 每天 150,000 tokens |
| 请求频率 | 每 Key 每分钟 10 次 |

图片任务在同一用户 Key 与同一上游 Token 上各至少间隔 15 秒，等待时会自动排队；排队超过 90 秒才报错。全站并发默认 1，可通过 `.env` 调整。上游图片请求触发 429 后，全站图片生成至少冷却 60 秒；若上游要求更长时间，则以更长者为准。免费图与付费图分开计数；失败生成不计成功图片数。完整规则见[用户限制说明.md](用户限制说明.md)。

## 配置与数据

完整环境变量见 [.env.example](.env.example)。特别注意：

- `ADMIN_PASSWORD`、`NAI_TOKENS` 必须自行设置；不要提交 `.env`。
- `KEY_INACTIVITY_DELETE_DAYS` 默认 `3`，长期未使用的 Key 会被永久删除；设为 `0` 可关闭。
- `ADMIN_COOKIE_SECURE` 默认开启。仅在可信的本地 HTTP 调试环境才考虑设为 `0`。
- 本地状态保存在 `data/nai_gate.db`；备份运行中的 SQLite 数据库时请使用 SQLite 的在线备份功能。历史日志可能包含用量与请求元数据，应限制访问。

用量日志记录 Key、模型、状态和消耗，不保存生成的图片或提示词。历史版本遗留的文件不会因升级自动删除。

本地测试：安装 `requirements.txt`、`pytest` 和 `pytest-asyncio`，然后执行 `python -m pytest -q`。测试使用假上游，不消耗真实 NovelAI 额度。
