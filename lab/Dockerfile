# 实验室镜像（离线回测 / 蒙特卡洛 / 日报）。与网关镜像分开：网关不装 numpy / matplotlib，也不 import app.lab。
# 构建上下文是仓库根目录（docker-compose.yml 里 build.context: .）。
# 用法：docker compose --profile lab run --rm -v /tmp/nai-lab.db:/snapshot/gate.db:ro lab
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir numpy==2.2.6 matplotlib==3.10.3 \
 && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR /app
# 只需要 app/ 代码（lab 从 app/guard.py、app/quota_algo.py、app/config.py 读默认参数）；不需要网关依赖
COPY --chown=app:app app ./app
ENV TZ=Asia/Shanghai MPLCONFIGDIR=/tmp/mpl PYTHONDONTWRITEBYTECODE=1
USER app
ENTRYPOINT ["python", "-m", "app.lab.run"]
CMD ["--db", "/snapshot/gate.db", "--out", "/app/data/lab", "--replicates", "200", "--max-scale", "10"]
