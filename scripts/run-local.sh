#!/bin/bash
# 本地测试启动：加载 .env 后运行，访问 http://127.0.0.1:3003/admin
cd "$(dirname "$0")/.."
set -a; source .env; set +a
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 3003
