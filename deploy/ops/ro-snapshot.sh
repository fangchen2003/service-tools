#!/bin/bash
# 每天 05:10（cron）做一份脱敏只读快照，给协作 AI 分析：/opt/backups/ro/owl-ro-YYYYMMDD.db.gz，保留 7 份。
# 底层用 naigate-snapshot（容器内只读事务，不锁库、不含原图），再用 ro_sanitize.py 去掉 Key 原文、盐、提示词。
set -euo pipefail
OUT=/opt/backups/ro; mkdir -p "$OUT"; chmod 700 "$OUT"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
/usr/local/bin/naigate-snapshot "$TMP/s.db.gz" >/dev/null
gunzip "$TMP/s.db.gz"
python3 /opt/service-tools/deploy/ops/ro_sanitize.py "$TMP/s.db"
F="$OUT/owl-ro-$(date +%Y%m%d).db.gz"
gzip -c "$TMP/s.db" > "$F"; chmod 600 "$F"
ls -1t "$OUT"/owl-ro-*.db.gz | tail -n +8 | xargs -r rm -f
echo "ro snapshot $F ($(du -h "$F" | cut -f1))"
