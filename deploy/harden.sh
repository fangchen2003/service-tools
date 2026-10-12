#!/usr/bin/env bash
# Ubuntu/Debian 服务器基础加固（需 root，可重复执行）：
# 防火墙(22/80/443)、SSH 仅密钥登录 + fail2ban、自动安全更新、1GB 交换分区、SQLite 每日备份(保留 7 天)。
# 运行前请确认你已经能用密钥登录——脚本会在 authorized_keys 为空时跳过关闭密码登录，避免把自己锁在外面。
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "请用 root 运行"; exit 1; }
APP_DIR="${APP_DIR:-/opt/service-tools}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q && apt-get install -y -q ufw fail2ban unattended-upgrades sqlite3

# 先放行当前实际的 SSH 端口（改过端口的机器若只放行 22 会把自己锁在外面）。
ssh_port="$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')"; ssh_port="${ssh_port:-22}"
ufw default deny incoming >/dev/null; ufw default allow outgoing >/dev/null
ufw allow "${ssh_port}/tcp" >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null; ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null

cat > /etc/fail2ban/jail.local <<'CONF'
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5
[sshd]
enabled = true
CONF
systemctl enable --now fail2ban >/dev/null 2>&1

if [ -s /root/.ssh/authorized_keys ]; then
  cat > /etc/ssh/sshd_config.d/00-hardening.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 3
CONF
  sshd -t && { systemctl reload ssh 2>/dev/null || systemctl reload sshd; }
else
  echo "⚠ /root/.ssh/authorized_keys 为空：已跳过关闭 SSH 密码登录。"
fi

printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' > /etc/apt/apt.conf.d/20auto-upgrades
echo 'Unattended-Upgrade::Automatic-Reboot "false";' > /etc/apt/apt.conf.d/52no-reboot

if ! swapon --show | grep -q swapfile; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-swap.conf && sysctl -q -p /etc/sysctl.d/99-swap.conf
fi

mkdir -p /opt/backups
cat > /usr/local/bin/naigate-backup <<CONF
#!/bin/bash
set -e
DB=$APP_DIR/data/nai_gate.db
[ -f "\$DB" ] || exit 0
OUT=/opt/backups/nai_gate-\$(date +%F).db
sqlite3 "\$DB" ".backup '\$OUT'" && gzip -f "\$OUT"
find /opt/backups -name 'nai_gate-*.db.gz' -mtime +7 -delete
CONF
chmod +x /usr/local/bin/naigate-backup
echo '30 3 * * * root /usr/local/bin/naigate-backup >> /var/log/naigate-backup.log 2>&1' > /etc/cron.d/naigate-backup

# Docker 日志轮转，避免日志撑满磁盘（修改后需重启 docker 并重建容器）
if command -v docker >/dev/null && [ ! -f /etc/docker/daemon.json ]; then
  echo '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}' > /etc/docker/daemon.json
  echo "已写入 Docker 日志轮转；请执行 systemctl restart docker 后重建容器。"
fi
echo "完成。"
