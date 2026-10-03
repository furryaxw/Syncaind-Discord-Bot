# 构建与运行都在 Linux 侧完成：
#   docker compose -f deploy/docker/compose.yaml up -d --build
#
# 不要在 Windows 上执行 docker build —— 换行、文件权限与路径语义都不同，
# 构建出来的镜像行为会和 Linux 侧不一致。

FROM python:3.10-slim

# 地板版本与开发机一致，保证「本地能跑」等价于「容器能跑」。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY bot ./bot

RUN useradd --create-home --uid 10001 botuser \
    && mkdir -p /app/data \
    && chown -R botuser:botuser /app
USER botuser

# 数据库与日志都落在 /app/data，由 compose 挂载卷。
VOLUME ["/app/data"]

CMD ["python", "-m", "bot"]
