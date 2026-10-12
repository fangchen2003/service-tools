#!/bin/bash
# Mac 端异地备份：把服务器最新的每晚备份拉到本机，校验后保留最近 30 份。
# 由 launchd 每天 04:30 运行（服务器 03:30 出备份）；Mac 睡眠错过的话，醒来后会补跑。
set -euo pipefail
DEST="$HOME/Backups/naigate"
KEY="$HOME/.ssh/naigate_ed25519"
HOST="root@47.76.230.107"
OPTS=(-i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=20)
mkdir -p "$DEST/daily"; chmod 700 "$DEST"
LATEST=$(ssh "${OPTS[@]}" "$HOST" 'ls -1t /opt/backups/daily/nai_gate-*.db.gz 2>/dev/null | head -1')
[ -n "$LATEST" ] || { echo "$(date '+%F %T') 服务器上没有每晚备份" >&2; exit 1; }
NAME=$(basename "$LATEST")
if [ -f "$DEST/daily/$NAME" ]; then echo "$(date '+%F %T') 已有 $NAME，跳过"; exit 0; fi
PART="$DEST/daily/.$NAME.part"
scp "${OPTS[@]}" -q "$HOST:$LATEST" "$PART"
TMP=$(mktemp); trap 'rm -f "$TMP" "$PART"' EXIT
gunzip -c "$PART" > "$TMP"
CHK=$(sqlite3 "$TMP" "PRAGMA integrity_check;")
[ "$CHK" = "ok" ] || { echo "$(date '+%F %T') $NAME 完整性校验失败：$CHK" >&2; exit 1; }
KEYS=$(sqlite3 "$TMP" "SELECT COUNT(*) FROM api_keys;")
mv "$PART" "$DEST/daily/$NAME"; chmod 600 "$DEST/daily/$NAME"
ls -1t "$DEST"/daily/nai_gate-*.db.gz | tail -n +31 | while read -r old; do rm -f "$old"; done
echo "$(date '+%F %T') 已拉取 $NAME（$(du -h "$DEST/daily/$NAME" | cut -f1)，完整性 ok，keys=$KEYS）"
