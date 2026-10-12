# 运维脚本（服务器上放在 /opt/backups/）

| 文件 | 用途 |
|---|---|
| `deploy.sh <版本号>` | 等队列空闲 → 备份数据库与 .env 到 `/opt/backups/pre-deploy-<时间>` → 重建容器 → 验证 |
| `nai-watch.sh` | 连续监控：成员、拒绝、报错、Bug、动态额度、自动驾驶、跑图分享 |
| `lab-daily.sh` | 实验室日报：`sqlite3 .backup` 出副本到 /tmp → `docker compose --profile lab run --rm lab`（回放校准 + 蒙特卡洛 + 日报图，结果在 `data/lab/<日期>/`）→ `post_report.py` 默认 dry-run；`LAB_POST=1` 才真的发到公告频道 |
| `post_report.py` | 把 `report.png` + `summary.md` 以 multipart 发到 `ANNOUNCE_CHANNEL_ID`（`allowed_mentions` 为空）；默认 `--dry-run` 只打印，`--send` 才发 |
| `announce.py` | 往公告频道发一条消息（正文从 stdin 读）：`docker cp announce.py nai-gate:/tmp/ && docker exec -i -u 0 nai-gate python /tmp/announce.py` |

只重启机器人：`docker compose --profile discord up -d --no-deps discord-bot`（不加 `--no-deps` 会连带重建网关）。
