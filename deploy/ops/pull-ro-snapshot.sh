#!/bin/bash
# 本机执行：把服务器上最新的脱敏只读快照拉到 ~/dev1/owl-data/（仓库外），协作 AI 只读这里，不登录服务器。
set -euo pipefail
DEST=${1:-$HOME/dev1/owl-data}; mkdir -p "$DEST"; chmod 700 "$DEST"
SSH="ssh -i $HOME/.ssh/naigate_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes"
F=$($SSH root@47.76.230.107 'ls -1t /opt/backups/ro/owl-ro-*.db.gz | head -1')
scp -q -i "$HOME/.ssh/naigate_ed25519" -o IdentitiesOnly=yes "root@47.76.230.107:$F" "$DEST/"
gunzip -f "$DEST/$(basename "$F")"
chmod 400 "$DEST/$(basename "${F%.gz}")"
ln -sf "$(basename "${F%.gz}")" "$DEST/latest.db"
echo "$DEST/latest.db -> $(basename "${F%.gz}")"
