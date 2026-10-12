#!/usr/bin/env bash
# 每日实验室：备份副本 → 跑 lab（回放校准 + 蒙特卡洛 + 日报图）→ 发帖（默认 dry-run，只打印）。
# cron（北京时间 08:30 总结前一天，排在 0 点复盘之后）：30 8 * * * bash /opt/service-tools/deploy/ops/lab-daily.sh >> /opt/backups/lab.log 2>&1
# 环境变量：
#   DAY=YYYY-MM-DD        报告日，默认昨天
#   LAB_ARGS="..."        额外参数，例如 "--sweep account_hourly_cap=100,120,150,180,200"
#   LAB_POST=1            真的发到 Discord（需要 DISCORD_BOT_TOKEN、ANNOUNCE_CHANNEL_ID，可从 .env 读）；默认只 dry-run
set -euo pipefail
ROOT=${ROOT:-/opt/service-tools}
cd "$ROOT"
DAY=${DAY:-$(TZ=Asia/Shanghai date -d yesterday +%F 2>/dev/null || TZ=Asia/Shanghai date -v-1d +%F)}
NAME=.lab-snap-$$-$(date +%s).db
SNAP="$ROOT/data/$NAME"
trap 'rm -f "$SNAP"' EXIT

# 1) 一致快照：在网关容器里只读复制（不含原图，约 1 秒）。不要用宿主机 sqlite3 .backup 整库复制——
#    1.3GB 要几分钟、磁盘 IO 打满，网关写库超时，成员收到 500（2026-10-10 19:07 事故）。
RES=$(docker exec -i nai-gate python - "/app/data/$NAME" < "$ROOT/deploy/ops/snapshot.py")
[ "${RES%% *}" = "ok" ] || { echo "snapshot FAILED: $RES" >&2; exit 1; }
chmod 644 "$SNAP"                      # 容器内用户 10001 需要能读
echo "snapshot $SNAP ($(du -h "$SNAP" | cut -f1))"

# 2) 跑 lab（结果写 data/lab/<DAY>/，data/ 不进 git）
mkdir -p data/lab
chown 10001:10001 data/lab 2>/dev/null || true
docker compose --profile lab run --rm -v "$SNAP:/snapshot/gate.db:ro" lab \
  --db /snapshot/gate.db --out /app/data/lab --day "$DAY" --replicates "${REPLICATES:-200}" --max-scale "${MAX_SCALE:-10}" ${LAB_ARGS:-}

# 3) 发帖：默认 dry-run；LAB_POST=1 才真的发
OUT="data/lab/$DAY"
if [ "${LAB_POST:-0}" = "1" ]; then
  export $(grep -E '^(DISCORD_BOT_TOKEN|ANNOUNCE_CHANNEL_ID)=' .env | xargs)
  python3 deploy/ops/post_report.py --dir "$OUT" --send
else
  python3 deploy/ops/post_report.py --dir "$OUT"
fi
