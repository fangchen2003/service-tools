# 猫头鹰公益站 · 系统事实清单（唯一来源）

> 更新：2026-10-10，线上版本 v2.13.0。数字均从线上数据库和代码核对过。
> **规则**：首页、后台说明、代码注释、Artifact、运维文档里的数字都以本文件为准。改了系统就先改这里，再同步各处。

## 0. 一句话
47 位 Discord 成员（名额上限 61）共用站长的 **1 个 NovelAI Opus 账号**。网关给每人发虚拟 Key，所有出图串行经过这一个账号；
额度由算法每 10 分钟重算，各模块用独立数据来源互相校验。

## 1. 账号保护（硬上限，guard.py）
| 参数 | 当前值 | 说明 |
|---|---|---|
| 并发 | 1 | 每个上游账号同时只画 1 张 |
| 出图间隔 | 15 秒 + 随机 0～5 秒 | 平均约 17.5 秒一张（`image_min_interval` + `interval_jitter`） |
| 每小时上限 | **150 张** | AIMD 自动调：上游 429 减半；前一天顶到过上限且没有 429 才 +10，范围 100～160 |
| 3 小时上限 | 400 张 | 防止连续几小时都在冲 |
| 每日上限 | 1000 张 | |
| 安静时段 | 不启用 | 2026-10-10 起夜里不限速 |
| 每把 Key 排队 | 1 张生成中 + 最多再排 1 张 | `key_image_queue` |
| 全站排队 | 最多 5 张 | `queue_per_account` |
| 每把 Key 请求频率 | **5 次/分钟** | 成员 Key 的 RPM（`REGISTER_RPM`）；后台手动建的 Key 默认 10；超出返回「请求过于频繁」 |
| 每把 Key 出图间隔 | 15 秒 | `key_image_min_interval`，同一把 Key 两张图之间 |
| 每把 Key 同时在途 | 最多 4 个请求 | `MAX_INFLIGHT_PER_KEY`（含排队中的） |
| 排队超时 | 90 秒 | `queue_timeout`；我们的域名是 Cloudflare 灰云（只托管 DNS、不代理），没有 CF 的 100 秒断开限制 |

理论最快：3600 ÷ 17.5 ≈ 205 张/小时，被每小时上限 150 封顶，**实际卡住的是 150**。
生成本身中位约 4～9 秒（V5 快、V4.5 慢），比间隔短，所以等待主要来自排队，不是上游慢。

## 2. 额度分配（quota_algo.py，每 10 分钟重算、每天微调一次）
| 量 | 当前值 | 怎么定 |
|---|---|---|
| A：每人每天 V4.5 上限 | **150** | 昨天拥挤（用量 ≥ 日上限 85% 或 ≥ 3 个小时被每小时上限拦）→ −25；有余量且有人顶格 → +25；范围 B～300 |
| B：保底 | **100** | 拥挤 → −10（不低于 50）；有余量且有人因「超过保底、全站不空闲」被拦 → +10 |
| 超过保底后 | 只在全站空闲时放行 | 空闲 = 无人排队且本小时 < 60% 上限 |
| G：全站每天 V5 | **203** | 恢复 %/天 × 14.2 张/1% × 系数 k |
| D5：每人每天 V5 | **18** | G ÷ 最近 3 天用过 V5 的人数（至少按 10 人算），限制 3～30；每天只定一次 |
| k | 1.3（账号剩 97%） | 剩 ≥90% → 1.3，70–90% → 1.1，40–70% → 0.9，20–40% → 0.6，<20% → 0.3；目标让剩余稳定在 60–80% |

14.2 张/1% = 官方「1% ≈ 17.3 张（23 步）」按 28 步折算（17.3 × 23/28）。恢复速度用接口实测（拿不到按 11%/天）。
**名额和账号负载无关**：每小时 / 每日总上限不变，人多只是分得更细，所以扩名额不增加账号风险。

## 3. Anlas 续杯（anlas_pool.py）
每日池 = (账号剩余 Anlas − 保留) ÷ 距续费天数，且 ≤ (月预算 9000 − 本月已用) ÷ 本月剩余天数。
只给「近 7 天常用、领取满 M 天」的成员，用于当天 V5 用完后续杯。**目前 0 人合格**（数据库 10-09 重建，历史不足）。

## 4. 自动驾驶（autopilot.py，每 10 分钟）
| 规则 | 线上模式 | 能否执行 | 做什么 |
|---|---|---|---|
| slots 名额 | enforce | 能（只加不减） | 快满或有人候补，且昨天用量 < 60% → +5；需要昨天数据 ≥ 20 小时 |
| breaker 熔断 | enforce | 能 | 15 分钟内**上游**失败 ≥ 5 次且 ≥ 30% → 全站暂停 5 分钟。只算 5xx 和流中途断开；本地拦截、上游 429、参数错误（4xx）不算；只看上次熔断结束后的数据 |
| key_guard | enforce | 只有一条能 | 1 小时内被拒 ≥ 60 次（不含「Key 暂停中」本身的拒绝）→ 暂停该 Key 1 小时（不私信）；换网段 / 重置 Key 两条只观察 |
| economy 节约模式 | observe | 能（未开启） | 15 分钟内「排队的人太多」≥ 8 次 → 建议开；30 分钟 ≤ 1 次 → 建议关；切换间隔 ≥ 1 小时 |

## 5. 其他机制
| 机制 | 状态 |
|---|---|
| 节约模式 | 已上线、当前**关**。开启时免费档统一 14 步 + Euler Ancestral（保留负面词），Anlas 约省 42%（成本 ∝ 步数，28 → 14 步）。手动或算法切换都在公告频道通知 |
| 防分享 share_guard | 线上 **observe**（只记证据不处罚），校准前不执行；未设置时也按 observe（fail-closed） |
| **私信总闸门** | `dm_enabled` 默认**关**：闲置提醒、候补邀请、Anlas 通知、后台操作通知、防分享处罚全部不私信。开启后也限速：10 分钟 ≤ 3 条、24 小时 ≤ 20 条；闲置提醒每小时最多处理 3 个 |
| 机器人画廊互动 | 自动点赞、AI 看图评论默认**关**；拉不到后台配置时也按关 |
| 公平调度 DRR | **只观察**：真实顺序仍是 FIFO，DRR 重排没接入派发路径 |
| 领 Key 限速 | 每小时最多 12 人（`issue_hourly_cap`） |
| 名额 | 上限 61；候补邀请**不私信**（`waitlist_dm` 默认 0），公告频道发一条汇总 |
| 网页 Discord 登录 | **暂停**（`web_login_paused` 默认 1，设 0 才开放）：首页不显示按钮，服务端 `/login` 也直接跳回首页、不发起 OAuth。应用被 Discord 标记，申诉 10-10 已提交；领 Key 用 `/register`、查额度用 `/quota` |
| 网页 Key 登录 | `POST /login/key`：粘贴 `/register` 领到的 Key 换取同一个成员会话（按 discord_id 签发，30 天）；后台手建、未绑定 Discord 的 Key 不行；每 IP 10 分钟错 8 次锁定；校验 Origin 防登录 CSRF。不受 `web_login_paused` 影响 |
| 作品备份（beta） | 首页登录后「作品备份」：EPUB 画册（一图一页 + 提示词/参数）或原图 ZIP（+ prompts.txt），每人 60 秒 1 次，只含仍在保留期内的原图 |
| 后台 Key 额度 | 后台新建的 Key 为手动额度（算法不覆盖）；编辑 Key 只提交改动过的字段，只有改了额度才转为手动 |
| `/register` | **唯一的发 Key 路径**：斜杠命令 → `POST /self-register/issue` → `issue_direct` → `_provision`；回复只有本人可见，不走 OAuth、不私信。旧的 OAuth 领取（`/self-register/intent` + `/self-register/callback`，`begin`/`finish`）已删除，不要恢复 |
| 免费规格 | V4.5 ≤ 1024×1024；V5 用预设尺寸（如 832×1216）；≤ 28 步；每次 1 张；图生图 / Vibe 关闭 |

## 6. 数据与备份
| 项 | 当前值 |
|---|---|
| 提示词保留 | 14 天 |
| 原图保留 | 3 天（成员可在首页打包下载）；0 = 不存原图。流式出图也会保存（v2.12.3 起） |
| 缩略图 | **不再保存**；后台画廊小图从原图现场缩小。旧缩略图 14 天内自然过期 |
| 用量日志 | 长期保留 |
| 备份内容 | 在线一致备份，**去掉原图 BLOB**（临时数据），完整性校验后 gzip，约 11MB/份；校验失败则中止部署 |
| 部署前快照 | 只留最近 10 份 |
| 每晚备份 | GFS：每日 14 份、每周 8 份、每月 6 份 |
| 异地备份 | 站长 Mac：launchd 每天 04:30 拉取最新每晚备份，校验后保留 30 份（`~/Backups/naigate/`） |

## 7. 互相校验（kernel.py，每轮 tick 后）
每个模块的结论都用另一个独立数据来源核对，不一致记进 Bug 追踪并在后台标红：
| 模块 | 校验 |
|---|---|
| ① 容量 | 上游 V5 消耗 % ↔ 日志 V5 张数（每 1% 应在 7～28 张）；内存每小时计数 ↔ 用量日志；实际每小时出图 ≤ 上限 |
| ② 分配 | 算法管理的 Key 额度都已同步；保底 ≤ 上限；保底 = 算法当前值 |
| ③ 公平调度 | 没有 Key 垄断队列（影子统计 ≤ 60%） |
| ④ 诚信 | 每个处罚都有强证据（会处罚成员的决定需要 ≥ 2 个独立证据） |
| Anlas | 分到的 Anlas 至少够一张续杯 |
| 领 Key | 已领人数 ≤ 名额 |
| ⑥ 观测 | 生成记录 ↔ 用量日志张数 |

## 8. 指标口径（后台「上游表现」）
- 生成 P50 = 中位数（一半的图比它快）；P90 = 90% 的图比它快。
- 实际 / 上限（张/时）：实际 = 这一时段平均每小时出图；上限 = min(生成速度与间隔决定的速度, 每小时上限)。
- 成功率 = 成功 ÷ (成功 + 失败)，**不含本地拒绝**（额度、限流、功能未开放这些是有意拦截，不算失败）。

## 拒绝原因码（v2.14.2 起）

`usage_log.reason` 记录被拒请求的原因码（定义在 `app/reasons.py`）：`queue_full` 全站排队满、`key_busy` 本 Key 上一张没出完、`hourly_cap` / `quiet_cap` / `cap_3h` / `daily_cap` 账号上限、`breaker` 全站熔断、`key_paused` Key 被暂停。
节约模式数 `queue_full`，Key 限流排除 `key_paused`，AIMD 加窗看 `hourly_cap`——都按码数，不再 `LIKE` 中文说明。升级时一次性回填最近 2 天的旧记录。


## 共用站点设置（v2.14.3 起，`python -m app.site_flags` 生成）

读写只走 `site_flags.get / put`；缺失或格式不对一律回到默认值（默认值按 fail-closed 选）。`tests/test_site_flags.py` 禁止别处直接 `get_setting` 这些键。各模块自己的参数组（`guard_*`、`runtime_*`、`bot_*`、quota_algo / anlas_pool DEFAULTS、`module_*`）不在此表。

| 键 | 类型 | 默认 | 范围 | 归属 | 说明 |
|---|---|---|---|---|---|
| `waitlist_enabled` | flag | False |  | registration | 候补排队（关 = 名额满了直接提示，有空位先到先得；机器人不能私信时候补只会空占名额） |
| `member_sweep` | flag | True |  | registration | 退群回收：每人每天核对一次是否还在服务器，不在就删 Key |
| `web_login_paused` | flag | True |  | registration_routes | 网页 Discord OAuth 登录暂停（申诉期间）。Key 登录不受影响 |
| `economy_mode` | onoff | False |  | ops / autopilot | 节约模式：免费档统一 14 步 + Euler-a |
| `dm_enabled` | flag | False |  | registration | 私信总闸门（关 = 任何私信都不发） |
| `waitlist_dm` | flag | False |  | registration | 有名额时私信候补成员（关 = 只在公告频道发一条汇总） |
| `issue_hourly_cap` | int | 12 | 0–500 | registration | 每小时最多发放的新 Key |
| `register_open` | flag | False |  | modules / registration | 开放 /register 领取 |
| `global_daily_v5` | int | `.env global_daily_v5` | 0–100000 | 额度 / 首页 / 客户端 | 全站每日 V5 张数（0 = 不限） |
| `global_monthly_anlas` | float | `.env global_monthly_anlas` | 0–1e+06 | 额度 / Anlas 池 | 全站每月 Anlas 预算（0 = 不限） |
| `guard_base_daily_images` | int | 100 | 0–100000 | guard（注册私信也读） | V4.5 每人每天保底张数 |
| `algo_notice` | str | '' |  | quota_algo → 首页 | 首页显示的今日额度说明 |
| `audit_image_retention_days` | int | 3 | 0–365 | audit | （已停用，2026-10-11 起原图按容量上限清理） |
| `audit_image_cap_mb` | int | 8192 | 0–500000 | audit | 原图占用上限（MB）：超过后自动删掉最旧的原图；0 = 不限 |
| `share_guard_mode` | choice | 'observe' | enforce / observe / off | share_guard | 防分享：enforce 处罚 / observe 只记录 / off |

## 退群回收（v2.15.0 起）

维护循环每 5 分钟用机器人 Token 查 1 人 `GET /guilds/{DISCORD_GUILD_ID}/members/{id}`，每人每天一次（`discord_registrations.member_checked_at`）。只有 404 + code 10007（Unknown Member）才删 Key、放名额并写操作日志「退群回收 Key」；其他任何结果 1 小时后重查，绝不误删。总开关 `member_sweep`（默认开）。不发私信。

## 部署不中断（v2.15.3 起）

- Caddy `reverse_proxy` 加 `lb_try_duration 45s / lb_try_interval 500ms`：重启期间连不上网关时挂住请求重连，不再回 502（只重试拨号失败，请求未发出，不会重复出图）。线上 /opt/caddy/Caddyfile 已同步。
- `stop_grace_period: 120s` + uvicorn `--timeout-graceful-shutdown 110`：停机时让生成中的图完成并记账，不再 10 秒硬杀。
- deploy.sh 仍先等队列空闲（最多 60 秒）、先做快照再重建。

