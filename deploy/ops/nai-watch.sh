#!/bin/bash
# 猫头鹰公益站上线监控：每 60 秒轮询一次，只输出需要关注的事件。
DB=/opt/service-tools/data/nai_gate.db
q(){ sqlite3 -readonly -separator ' ' "$DB" "$1" 2>/dev/null; }
last=$(date +%s); hour=$(date +%H); shared_prev=""; down_prev=""; cool_prev=0
summary(){
  echo "📊 $(date +%H:%M) 汇总：今日请求 $(q "SELECT COALESCE(SUM(requests),0) FROM counters WHERE day=date('now','+8 hours')") · 图片 $(q "SELECT COALESCE(SUM(images),0) FROM counters WHERE day=date('now','+8 hours')") 张 · Anlas $(q "SELECT COALESCE(SUM(anlas),0) FROM counters WHERE day=date('now','+8 hours')") · 已领 Key $(q "SELECT COUNT(*) FROM discord_registrations")/$(q "SELECT CASE WHEN CAST(value AS INT)>0 THEN value ELSE '不限' END FROM site_settings WHERE key='register_max_users'") · 今日拒绝 $(q "SELECT COUNT(*) FROM usage_log WHERE status='rejected' AND ts>strftime('%s','now','+8 hours','start of day','-8 hours')")"
  mem=$(free -m | awk '/Mem:/{print $7}'); disk=$(df / | awk 'NR==2{print $5+0}')
  echo "🖥 服务器：负载 $(cut -d' ' -f1-3 /proc/loadavg) · 可用内存 ${mem}MB · 磁盘已用 ${disk}%"
  [ "$mem" -lt 300 ] && echo "🚨 服务器可用内存只剩 ${mem}MB，考虑升级配置"
  [ "$disk" -gt 80 ] && echo "🚨 服务器磁盘已用 ${disk}%"
  dbmb=$(du -m /opt/service-tools/data/nai_gate.db 2>/dev/null | cut -f1)
  imgmb=$(q "SELECT CAST(COALESCE(SUM(LENGTH(image)),0)/1048576 AS INT) FROM generation_audit")
  imgn=$(q "SELECT COUNT(*) FROM generation_audit WHERE image IS NOT NULL")
  irdays=$(q "SELECT COALESCE(value,3) FROM site_settings WHERE key=\"audit_image_retention_days\""); irdays=${irdays:-3}
  echo "💾 数据库 ${dbmb}MB · 原图 ${imgmb}MB（${imgn} 张，约 ${irdays} 天后自动清）"
  [ "${imgmb:-0}" -gt 5000 ] && echo "🚨 原图已占 ${imgmb}MB，考虑调小保留天数"
}

algo(){
  # 算法指标：动态额度结果、V5 用量、本小时用量、排队等待、保底借用、自动驾驶判断
  q "SELECT '🧮 算法：V4.5 上限 '||json_extract(value,'$.ceiling')||'（保底 '||json_extract(value,'$.base')||'）· V5 每人 '||json_extract(value,'$.v5.each')||'，全站 '||json_extract(value,'$.v5.global')||'（剩余 '||COALESCE(json_extract(value,'$.v5.percent'),'?')||'%，系数 '||json_extract(value,'$.v5.k')||'，按 '||json_extract(value,'$.v5.people')||' 人分）' FROM site_settings WHERE key='quota_algo_last'"
  q "SELECT '🧮 今日 V5 已用 '||COALESCE(SUM(v5),0)||' · 今日图片 '||COALESCE(SUM(images),0)||' · 本小时 '||(SELECT COUNT(*) FROM usage_log WHERE ts>strftime('%s','now')-3600 AND status='ok' AND kind LIKE 'image%')||' 张 · 近 1 小时排队 中位 '||(SELECT COALESCE(CAST(AVG(wait_ms)/1000 AS INT),0) FROM usage_log WHERE ts>strftime('%s','now')-3600 AND status='ok' AND kind LIKE 'image%')||'s 最长 '||(SELECT COALESCE(MAX(wait_ms)/1000,0) FROM usage_log WHERE ts>strftime('%s','now')-3600 AND status='ok' AND kind LIKE 'image%')||'s · 保底拦截 '||(SELECT COUNT(*) FROM usage_log WHERE ts>strftime('%s','now')-3600 AND detail LIKE '%保底%')||' · 每小时上限拦截 '||(SELECT COUNT(*) FROM usage_log WHERE ts>strftime('%s','now')-3600 AND detail LIKE '%本小时出图量已达上限%') FROM counters WHERE day=date('now','+8 hours')"
  q "SELECT '🤖 自动驾驶：名额 '||json_extract(value,'$.rules.slots.value')||' · 熔断 '||CASE json_extract(value,'$.rules.breaker.value') WHEN 1 THEN '会触发' ELSE '正常' END||' · Key 守护 '||json_array_length(json_extract(value,'$.rules.key_guard.value'))||' 把' FROM site_settings WHERE key='autopilot_last'"
}
echo "▶ 监控已启动 $(date '+%m-%d %H:%M')"; summary; algo; algo_prev=""; ap_prev=""; tick=0
while true; do
  sleep 60; now=$(date +%s)
  q "SELECT '🆕 新注册：'||COALESCE(display_name,username,'?')||'（'||COALESCE(username,'')||'）' FROM discord_registrations WHERE created_at>$last"
  q "SELECT '❌ 请求出错：'||key_name||' · '||kind||' · '||model||' · '||substr(detail,1,90) FROM usage_log WHERE ts>$last AND status='error' AND COALESCE(key_id,0) NOT IN (SELECT id FROM api_keys WHERE is_test=1)"
  q "SELECT '🚫 被拒绝：'||key_name||' · '||kind||' · '||model||' · '||substr(detail,1,90)||CASE WHEN COUNT(*)>1 THEN '（×'||COUNT(*)||'）' ELSE '' END FROM usage_log WHERE ts>$last AND status='rejected' AND detail NOT LIKE '%已达今日%额度%' AND detail NOT LIKE '%请求过于频繁%' AND key_name NOT LIKE 'smoke-test%' AND COALESCE(key_id,0) NOT IN (SELECT id FROM api_keys WHERE is_test=1) GROUP BY key_name, kind, model, detail"
  q "SELECT '✅ 成功：'||key_name||' · '||GROUP_CONCAT(DISTINCT kind)||' '||COUNT(*)||' 次'||CASE WHEN SUM(images)>0 THEN '，'||SUM(images)||' 张' ELSE '' END||' · '||GROUP_CONCAT(DISTINCT substr(detail,1,40)) FROM usage_log WHERE ts>$last AND status='ok' AND COALESCE(key_id,0) NOT IN (SELECT id FROM api_keys WHERE is_test=1) GROUP BY key_name"
  q "SELECT '🎉 首次成功使用：'||k.name FROM api_keys k WHERE k.is_test=0 AND k.last_used_at>$last AND NOT EXISTS (SELECT 1 FROM usage_log u WHERE u.key_id=k.id AND u.ts<=$last AND u.status='ok') AND EXISTS (SELECT 1 FROM usage_log u WHERE u.key_id=k.id AND u.ts>$last AND u.status='ok')"
  # 成员接口的 4xx/5xx（Key 填错、路径不对、被限流等不会进用量日志）
  docker logs --since 61s nai-gate 2>&1 | grep -E '"(GET|POST) /(ai|nai|v1|user)[^"]*" (4[0-9][0-9]|5[0-9][0-9])' | grep -v -E '127.0.0.1|172.18.|" (402|429) ' | sed -E 's/^INFO: +([0-9.]+):[0-9]+ - "([A-Z]+ [^ ]+) HTTP[^"]*" ([0-9]+).*/\1 \2 \3/' | awk '{split($1,a,"."); printf "🌐 成员接口 %s → %s（来源 %s.%s.*.*）\n", $2" "$3, $4, a[1], a[2]}' | sort | uniq -c | sed -E 's/^ +([0-9]+) (.*)/\2 ×\1/' | head -8
  # Bug 追踪（v1.8+）：新出现 / 复发 / 再次出现的错误；注意级别（上游故障、客户端提前断开、网页脚本报错）汇总显示
  q "SELECT '🐞 Bug（'||source||'）：'||substr(title,1,110)||'（累计 '||count||' 次'||CASE WHEN last_rid<>'' THEN ' · 编号 '||last_rid ELSE '' END||'）' FROM error_events WHERE last_seen>$last AND level='error'"
  q "SELECT '⚠ 注意（'||source||'）：'||substr(title,1,90)||'（累计 '||count||' 次）' FROM error_events WHERE last_seen>$last AND level='warn'"
  q "SELECT '💸 可能产生费用：'||key_name||' ≈'||unconfirmed_anlas||' Anlas' FROM usage_log WHERE ts>$last AND unconfirmed_anlas>0"
  q "SELECT '💳 消耗 Anlas：'||key_name||' '||anlas FROM usage_log WHERE ts>$last AND anlas>0"
  q "SELECT CASE e.kind WHEN 'action' THEN '🛡 防分享处罚：' ELSE '🔍 防分享证据：' END||COALESCE(k.name,'#'||e.key_id)||' · '||e.detail||CASE WHEN e.points>0 THEN '（+'||CAST(e.points AS INT)||'）' ELSE '' END FROM share_evidence e LEFT JOIN api_keys k ON k.id=e.key_id WHERE e.ts>$last"
  cool=$(curl -s -m 5 127.0.0.1:3003/queue-status | python3 -c 'import json,sys;print(json.load(sys.stdin).get("image_cooldown_remaining",0))' 2>/dev/null || echo 0)
  if [ "${cool:-0}" -gt 0 ] && [ "$cool_prev" -eq 0 ]; then echo "⏸ 上游 429 冷却中：约 ${cool} 秒"; fi; cool_prev=${cool:-0}
  down=$(for c in nai-gate nai-gate-discord caddy; do [ "$(docker inspect -f '{{.State.Running}}' $c 2>/dev/null)" = true ] || echo -n "$c "; done)
  if [ "$down" != "$down_prev" ]; then [ -n "$down" ] && echo "🚨 容器未运行：$down" || echo "✅ 容器已恢复"; fi; down_prev=$down
  curl -s -m 5 -o /dev/null -w '%{http_code}' 127.0.0.1:3003/healthz | grep -q 200 || echo "🚨 /healthz 无响应"
  docker logs --since 61s nai-gate 2>&1 | grep -E 'Traceback|\[error\]|\[warn\]|" 5[0-9][0-9] ' | grep -v '^\[bug\]' | grep -v 'healthz' | head -5 | sed 's/^/⚠ 网关日志：/'
  docker logs --since 61s nai-gate 2>&1 | grep '\[alert-quiet\] upstream_degraded' | head -1 | sed 's/^.*\] upstream_degraded: /🔕 上游波动（只记日志、未私信站长）：/'
  docker logs --since 61s nai-gate-discord 2>&1 | grep -E 'Traceback|ERROR|Exception|\[bug\]' | head -3 | sed 's/^/⚠ 机器人日志：/'
  docker logs --since 61s nai-gate-discord 2>&1 | grep '\[gallery\] new post' | sed 's/^.*\[gallery\] new post /🎨 跑图分享新帖（已自动点赞，待写评论）：/'

  # 算法结果变化（每 10 分钟重算）或自动驾驶出现动作时立即报告；另每 30 分钟报一次算法指标
  cur=$(q "SELECT json_extract(value,'$.ceiling')||'/'||json_extract(value,'$.base')||'/'||json_extract(value,'$.v5.each')||'/'||json_extract(value,'$.v5.global') FROM site_settings WHERE key='quota_algo_last'")
  if [ -n "$algo_prev" ] && [ "$cur" != "$algo_prev" ]; then echo "🧮 动态额度变化：$algo_prev → $cur（上限/保底/V5 每人/V5 全站）"; fi; algo_prev=$cur
  ap=$(q "SELECT json_extract(value,'$.rules.key_guard.value')||json_extract(value,'$.rules.breaker.value') FROM site_settings WHERE key='autopilot_last'")
  if [ "$ap" != "$ap_prev" ] && [ "$ap" != "[]0" ] && [ -n "$ap" ]; then q "SELECT '🤖 自动驾驶（观察）判断：'||json_extract(value,'$.rules.key_guard.why')||'；'||json_extract(value,'$.rules.breaker.why')||' '||json_extract(value,'$.rules.key_guard.value') FROM site_settings WHERE key='autopilot_last'"; fi; ap_prev=$ap
  q "SELECT '🧯 Bug 新增（'||source||'）：'||substr(title,1,100) FROM error_events WHERE first_seen>$last"
  tick=$((tick+1)); if [ $((tick % 30)) -eq 0 ]; then algo; fi
  if [ "$(date +%H)" != "$hour" ]; then hour=$(date +%H); summary; fi
  last=$now
done
