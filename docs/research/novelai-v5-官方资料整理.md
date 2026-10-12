# NovelAI Diffusion V5 与 Opus 用量上限 · 官方资料整理（2026-10-09 核对）

> 说明：官方文档受版权保护，这里不逐段搬运英文原文，而是按页给出原文链接 + 逐段中文详解，关键术语保留英文。
> 对照阅读请点每节的链接。标「未经官方确认」的是在官方页面里找不到的内容。

**一句话**：每 1% ≈ 17 张、100% ≈ 1,730 张、每月约 7,000 张（都按 23 步、约 100 万像素估）；
新订阅首 30 天约 11%/天、续订约 14%/天；免费条件 = 一次 1 张、不用底图、≤ Normal 尺寸（单张最大 1024×1024）、≤ 28 步。
**官方没有公开**：额度用完后每张 V5 扣多少 Anlas、并发 / 速率限制的具体数字、"Normal resolution" 的像素表。

## 1. 官方页面清单

| # | 标题 | 链接 |
|---|---|---|
| A | Image Generation Models | https://docs.novelai.net/en/image/models |
| B | Subscription | https://docs.novelai.net/en/subscription |
| C | FAQ（第 36–49 条为 Opus Usage Limit） | https://docs.novelai.net/en/faq |
| D | Effort Toggle | https://docs.novelai.net/en/image/effort |
| E | Quality Tags | https://docs.novelai.net/en/image/qualitytags |
| F | Undesired Content | https://docs.novelai.net/en/image/undesiredcontent |
| G | Multi-Character Prompting | https://docs.novelai.net/en/image/multiplecharacters |
| H | Tagging | https://docs.novelai.net/en/image/tags |
| I | Text Rendering | https://docs.novelai.net/en/image/textrendering |
| J | Vibe Transfer | https://docs.novelai.net/en/image/vibetransfer |
| K | Precise Reference | https://docs.novelai.net/en/image/precisereference |
| L | Inpaint | https://docs.novelai.net/en/image/inpaint |
| M | Image Generation 概览 | https://docs.novelai.net/en/image |
| N | Steps & Prompt Guidance | https://docs.novelai.net/en/image/stepsguidance |
| O | Sampling | https://docs.novelai.net/en/image/sampling |
| P | Basics | https://docs.novelai.net/en/image/basics |
| Q | Image2Image / Strength & Noise / Enhance / Upscale / Director Tools | https://docs.novelai.net/en/image/controltools 等 |
| R | Subscription Updates: Usage Limits（2026-08-20） | https://journal.novelai.net/subscription-updates-usage-limits-2025-88a208d5d9c5/ |
| S | NovelAI Diffusion V5 is here!（2026-08-21） | https://journal.novelai.net/image-generation-novelai-diffusion-v5-is-here-c2df7c6b8d2d/ |
| T | Understanding the Opus Usage Limit（2026-09-15） | https://journal.novelai.net/opus-usage-limit-explained/ |
| U | The Anlas Policy Change Is Live（2026-09-22） | https://journal.novelai.net/anlas-policy-change/ |
| V | V5 Full "Medium/High" Effort Toggle（2026-10-08） | https://journal.novelai.net/novelai-diffusion-v5-full-effort-toggle/ |
| W | Terms of Service | https://novelai.net/terms |

R–V 都有日文版（路径带 `-jp`）。blog.novelai.net 拒绝访问，官方公告实际发在 journal.novelai.net。

## 2. 逐页详解

### A. Image Generation Models
- V5 分 Full 和 Curated，所有付费档可用；自研全新架构，规模为 V4.5 两倍以上，训练用了 26.8 万 B200 GPU 小时。
- 能力：自然语言理解更强；多语言提示词（含日语）；Alpha 透明；重做的角色定位；多角色最多 22 个；英 / 日 / 中文字渲染改进；单张可出完整分格漫画。
- Token 上限：V5 Full 基础提示词约 1471、文字渲染约 750；V5 Curated 约 703 / 374。
- Full 数据更大、筛选更少，适合 Curated 处理不好的题材；Curated 更干净，适合日常与直播，最不容易意外出敏感内容。
- 前缀：`fur dataset,` 进入兽人模式；`background dataset,` 出无人物的风景 / 静物写实图。
- V5 是第一个对 Opus 免费生成设上限的模型，旧模型仍不限。

### B. Subscription
- 档位：Paper 免费试用（30 张图，最大 1024×1024）；Tablet $10/月 1000 Anlas；Scroll $15/月 1000 Anlas；Opus $25/月 10,000 Anlas。
- Opus 免费出图要同时满足：一次只生成一张、不用底图、尺寸不超过 "Normal"（单张最大 1024×1024）、步数 ≤ 28。
- 比 V4.5 新的模型才有用量上限；Opus 超出后这类模型都要花 Anlas，直到额度回补。
- 付费档付款后满 30 天续费；取消后权益保留到本期结束。
- 订阅 Anlas：V5 发布后第 31 天起，订阅结束即清零（提前取消的在原到期日清零）；取消后不能再买 Anlas；续费只补回本期花掉的部分。
- 付费 Anlas（"+" 购买）在订阅 Anlas 用完后才消耗，不参与月度补充。只有图像生成消耗 Anlas。

### C. FAQ（Opus Usage Limit，第 36–49 条）
- #36 为什么设限：V5 运行成本是 V4.5 的两倍多，官方选择设上限而不给 Opus 涨价；额度连续补充，不按周重置。
- #37 价格：Opus 仍为 $25，附带的 10,000 Anlas 单买要 $14；没有更高档位。
- #38 范围：只限 Opus 在 V5 上不花 Anlas 的生成（Normal、≤28 步）；本来就扣 Anlas 的高分辨率 / 高步数不受此上限影响；其他模型、非 Opus 用户不受影响。
- #39 机制：像充电电池一样持续回补，没有固定的日 / 月重置点。
- #40 补充速率：新订阅首 30 天约 11%/天；持续订阅约 14%/天；重新订阅按断档 0–1 天约 14%、2–3 天约 13%、4–5 天约 12%、6 天以上约 11%（到期时的剩余量也会影响）。从空到满：新订阅 9 天、持续订阅 7 天、重新订阅 7–9 天。
- #41 额度与账单日无关，续费不会瞬间回满。#42 过期期间继续补但不能用，重新订阅不额外加量。
- #43 每个身份只能有一个有效订阅；多账号造成的支付问题官方可能不处理。
- #44 用掉后立即开始回补。#45 汉堡菜单的 "Opus Usage Limit" 显示电量、充电速度、充满时间。
- #46 用完后可以继续用订阅或付费 Anlas 生成，Anlas 生成享有最高优先级。
- #47 张数：每月约 7,000 张；每 1% 约 17 张；100% 约 1,730 张——按 23 步（默认）、约 100 万像素估算，28 步会明显少一些。
- #48 消耗：分辨率和步数都影响，成本按像素数缩放，降分辨率比降步数省得多；effort 选 Medium 时用量和 Anlas 都少约 42%。
- #49 超过 100% 是 2026-08-22 给当时 Opus 用户的一次性奖励。

### D. Effort Toggle（仅 V5 Full 及其 inpainting）
- High：标准模型，所有功能可用（对比基准为 23 步默认设置）。
- Medium：蒸馏模型，固定 14 步，只能 Euler Ancestral，不能自定义 UC / 换 UC 预设，可调 Prompt Guidance 不能调 Rescale，负向提示改用负权重（如 `-3::hat::`）。比 High 少用约 42%，盲测质量相当。

### E. Quality Tags
- 默认开启，追加在提示词末尾（不显示但计入长度）。V5 两档：Light `, very aesthetic, amazing quality, no text`；Standard `, very aesthetic, masterpiece, no text`。

### F. Undesired Content
- V5 Full / Curated 各有 Heavy、Light、Furry Focus、Human Focus 四个 UC 预设，外加不加预设。Heavy 针对低分辨率、颗粒 / 压缩伪影、网点、多视图、水印等；Light 针对手部 / 解剖、偏色，带 `0::ai-generated::`；Human Focus 在 Heavy 上加人体和眼部项。

### G. Multi-Character Prompting
- V5 最多 22 个角色（V4 为 6）。基础提示词写场景和画风，每个角色有独立提示框和独立 UC。
- 位置默认由 AI 安排，改 Custom 可在画布上手动摆（V4/V4.5 限 5×5 网格）。
- 人数标签写在基础提示词里，角色框只写 `girl` / `boy` / `other`；`|` 分隔语法和角色框互斥；动作可加 `source#` / `target#` / `mutual#` 前缀（不总是可靠）。

### H. Tagging
- `transparent background` / `has alpha` / `alpha transparency` 只在 V5 有效。数据集前缀放在基础提示词最前面。`year XXXX` 可用但不稳定。

### I. Text Rendering
- V5 能渲染英 / 日 / 中等文字（V4/V4.5 只有英文）。写法：加 `text, english text`，基础提示词末尾写 `Text:` 加内容，多段用空行分隔，`Text:` 必须放最后。
- 字符上限：V4/V4.5 118，V5 Curated 374，V5 Full 750。短文字出不来可关 Quality Tags（其中有 `no text`）。

### J. Vibe Transfer
- Reference Strength 0–1，合计最好不超过 1.0；每次最多 16 个 vibe。
- 编码一次 2 Anlas，改 Information Extracted 后重编码再收 2；V4 及以上超过 4 个 vibe 每多 1 个加 2 Anlas；从图片元数据导入不收编码费。
- V5 首发不支持 Vibe Transfer（S 页），目前是否支持未经官方确认。

### K. Precise Reference
- 角色 / 风格 / 角色+风格三种参照；每次生成加收 5 Anlas。页面写仅 V4.5 可用，V5 首发不含（S 页）。与 Vibe Transfer 不兼容。

### L. Inpaint
- 遮罩重绘；Focused Inpainting 会把选区放大到约 100 万像素再重绘，Opus 在大图上用它不花 Anlas。V5 Full 首发即有 inpainting，V5 Curated 暂用 V4.5 Curated 的版本。

### M. Image Generation 概览
- 张数上限：Small 最多 6 张，Normal / Large 最多 4 张。**即使是 Opus，一次生成多张也总要花 Anlas。**

### N–P. Steps & Guidance / Sampling / Basics
- V3 及以上推荐 Guidance 约 5–6。采样器推荐 DPM++ 2M 和 Euler Ancestral；SMEA 稍贵，超过 1024×1024 自动启用。V5 可用的采样器未经官方确认（Medium 限 Euler Ancestral）。
- 重申 Opus 免费条件：≤28 步、≤Normal、不批量。

### Q. Image2Image / Enhance / Upscale / Director Tools
- 均未写 V5 支持情况或 Anlas 数字。Upscale 放大 4 倍；Director Tools 包括去背景、线稿、草图、上色、表情、整理。

### R. Subscription Updates（2026-08-20）
- 理由：V5 规模大（本文称 2.5 倍），少数用户占用不成比例的算力，所以定向限制而不全面涨价。
- 上限只针对 Opus 的 V5 免费生成，连续回补，满了才停，从空到满约一周；官方称设计成「大多数 Opus 用户永远用不完」。用完后可用 Anlas 继续，享最高优先级。旧模型和文本模型不限。

### S. NovelAI Diffusion V5 is here!（2026-08-21）
- 32 通道 VAE（V4.5 为 16），细节更准。新标签：`depthness`、复杂度四级（推荐 high）、`has alpha`、`meta:novel era` / `meta:golden era`、视觉小说类标签。
- 英日语正式支持，中德西葡有测试。引号括起的文字自动生成 `Text:` 块。Enhance 新增 "Max" 档。
- 首发不含：Precise Reference、Curated Inpainting、Vibe Transfer。

### T. Understanding the Opus Usage Limit（2026-09-15）——**和网关算法最相关**
- 额度按每张 V5 图的 Anlas 成本扣，越便宜的图扣得越少；每分钟连续补充。
- 每月 400%（按 4 周），约 7,000 张；每 1% ≈ 17 张、100% ≈ 1,730 张（23 步、100 万像素；28 步更少）。
- 速率来源：新订阅开局预充 100%，剩下 300% 当月补完 → 约 11%/天；第二个月起整月补 400% → 100% ÷ 7 天 ≈ 14.28%/天（上限）。
- 重新订阅：断档 0–1 天 14%、2–3 天 13%、4–5 天 12%、6 天以上 11%；断档 7 天以上重新给满 100% 预充。
- 从空到满：新订阅 9 天、续订 7 天。官方不推荐降低步数省额度（质量没把握）。

### U. Anlas Policy Change（2026-09-22）
- 当天起所有现存订阅 Anlas 转为付费 Anlas（永不过期）；之后订阅 Anlas 在订阅结束时清零。

### V. Effort Toggle 公告（2026-10-08）
- High 档用量不变；Medium 固定 14 步，是 Opus 免费上限 28 步的一半。

### W. Terms of Service（与公益站相关）
- §5.1 一份订阅对应一个账号；§5.3.2 禁止让第三方远程访问账号；§5.3.5 禁止出租 / 出售 / 转让账号；§8.1 不得共享账号信息；§9.1.2 禁止复制、托管、转售服务；§9.1.3 禁止让他人通过你的账号使用服务；§9.1.6 未经允许禁止自动化系统、禁止给服务造成压力。没有公开速率限制数字。

## 3. 对网关算法有用的事实（已用于 app/quota_algo.py）

| 事实 | 数值 | 来源 |
|---|---|---|
| 每 1% 额度 | ≈ 17 张（23 步、100 万像素）；**本站按 28 步折算 ≈ 14.2 张** | C #47、T |
| 100% | ≈ 1,730 张 | C #47、T |
| 恢复速率 | 新订阅首 30 天 ≈ 11%/天；续订 ≈ 14%/天；网关直接读接口的实测值 | C #40、T |
| 扣量方式 | 按 Anlas 成本扣，与像素和步数成正比 | T、C #48 |
| Medium effort | 比 High 省约 42%（固定 14 步、Euler Ancestral） | D、V |
| 免费条件 | 一次 1 张、不用底图、≤ Normal（单张最大 1024×1024）、≤ 28 步 | B、N、P |
| 一次多张 | 即使 Opus 也扣 Anlas（网关已强制每次 1 张） | M |
| 用完后每张 V5 扣多少 Anlas | 未经官方确认 | — |
| 并发 / 速率限制数字 | 未经官方确认（只有 ToS §9.1.6 原则性条款） | W |

第三方中文文章里「约 0.5%/小时」的说法与官方不一致，未采用。
