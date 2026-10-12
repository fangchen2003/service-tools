#!/bin/bash
# 每晚 03:30（/etc/cron.d/naigate-backup）。GFS 轮换：每日留 14 份、每周日留 8 份、每月 1 号留 6 份。
set -euo pipefail
R=/opt/backups
mkdir -p "$R/daily" "$R/weekly" "$R/monthly"
OUT="$R/daily/nai_gate-$(date +%F).db.gz"
/usr/local/bin/naigate-snapshot "$OUT"
[ "$(date +%u)" = 7 ] && cp -p "$OUT" "$R/weekly/"
[ "$(date +%d)" = 01 ] && cp -p "$OUT" "$R/monthly/"
ls -1t "$R"/daily/nai_gate-*.db.gz   2>/dev/null | tail -n +15 | xargs -r rm -f
ls -1t "$R"/weekly/nai_gate-*.db.gz  2>/dev/null | tail -n +9  | xargs -r rm -f
ls -1t "$R"/monthly/nai_gate-*.db.gz 2>/dev/null | tail -n +7  | xargs -r rm -f
