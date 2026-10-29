# Python 启动与测试

适合在部署前检查项目。以下命令均在项目根目录执行，建议使用 Python 3.12。

## 1. 安装依赖

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt pytest pytest-asyncio
```

## 2. 运行自动化测试

```bash
python -m pytest -q
```

测试使用假上游，不需要 NovelAI Token，也不会消耗生图额度。

## 3. 本机启动并检查页面

```bash
DATA_DIR=./data-python-test \
ADMIN_PASSWORD='换成你自己的测试密码' \
ADMIN_COOKIE_SECURE=0 \
NAI_TOKENS= \
KEY_INACTIVITY_DELETE_DAYS=0 \
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000/> 和 <http://127.0.0.1:8000/admin>；也可运行 `curl http://127.0.0.1:8000/healthz`。不配置上游 Token 时，页面和管理功能可测试，生图请求会返回不可用。若端口已被占用，修改 `--port`。

需要实际测试生图时，再用自己的测试账号 Token 设置 `NAI_TOKENS`；真实请求可能消耗上游额度。不要使用线上数据库或把 Token 提交到仓库。直接运行 Python 不会自动读取 `.env`，请在启动前显式设置所需环境变量。

上面的 `ADMIN_COOKIE_SECURE=0` 仅用于本机 HTTP 测试；公网部署应使用 HTTPS 并恢复安全 Cookie。按 `Ctrl+C` 可停止服务，测试数据会保留在 `./data-python-test`。
