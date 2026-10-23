FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy SALESTREND_ROOT=/app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
EXPOSE 8000
# worker 数由 API_WORKERS 决定（默认值与取舍见 design.md §7.4 容量规划）。多个 worker 各数各的：
# prometheus_client 多进程模式把各 worker 的计数写到这个目录，/metrics 汇总。
# 启动时清空：上一次运行留下的文件会被当成「还活着的 worker」继续累加，计数从旧值起跳
ENV PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus-multiproc
CMD ["sh", "-c", "rm -rf \"$PROMETHEUS_MULTIPROC_DIR\" && mkdir -p \"$PROMETHEUS_MULTIPROC_DIR\" && exec uv run --no-dev uvicorn salestrend.api.app:create_app --factory --host 0.0.0.0 --port 8000 --workers ${API_WORKERS:-2}"]
