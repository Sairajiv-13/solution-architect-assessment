FROM python:3.12-slim

ENV UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    QUOTA_DB=/data/quota.sqlite3
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.7
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable \
    && mkdir /data \
    && chown 10001:10001 /data
USER 10001:10001
CMD ["python", "-m", "assessment.demo"]
