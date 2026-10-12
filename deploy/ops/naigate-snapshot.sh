#!/bin/bash
# 用法：naigate-snapshot <输出文件.db.gz>
# 部署前快照和每晚备份共用：在线一致备份 → 去掉原图 BLOB → 完整性校验 → gzip（权限 600）。
# 为什么不备份原图：原图是临时数据（3 天自动删、成员可在首页自行打包下载），却占整库 90% 以上；
# 去掉后每份从 ~250MB 降到几 MB，真正要保的 Key / 注册 / 设置 / 用量历史 / 缩略图一样不少。
set -euo pipefail
DB=${NAIGATE_DB:-/opt/service-tools/data/nai_gate.db}
OUT=${1:?usage: naigate-snapshot <out.db.gz>}
TMP=$(mktemp "$(dirname "$OUT")/.snap-XXXXXX")
trap 'rm -f "$TMP"' EXIT
sqlite3 "$DB" ".backup '$TMP'"
if [ "$(sqlite3 "$TMP" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='generation_audit';")" = 1 ]; then
  sqlite3 "$TMP" "UPDATE generation_audit SET image=NULL, image_type='' WHERE image IS NOT NULL; VACUUM;"
fi
CHK=$(sqlite3 "$TMP" "PRAGMA integrity_check;")
[ "$CHK" = "ok" ] || { echo "snapshot FAILED integrity_check: $CHK" >&2; exit 1; }
KEYS=$(sqlite3 "$TMP" "SELECT COUNT(*) FROM api_keys;")
gzip -c "$TMP" > "$OUT"
chmod 600 "$OUT"
echo "snapshot $OUT ($(du -h "$OUT" | cut -f1), integrity ok, keys=$KEYS)"
