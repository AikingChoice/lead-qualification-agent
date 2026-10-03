FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    RSS_AGENT_DB=/data/rss-agent.sqlite3 \
    APP_ENV=production

WORKDIR /app
ARG UV_VERSION=0.12.16
RUN python -m pip install --no-cache-dir "uv==${UV_VERSION}"
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY config ./config
COPY migrations ./migrations
COPY alembic.ini ./
COPY docker ./docker
RUN uv sync --frozen --no-dev \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data

USER appuser
VOLUME ["/data"]
EXPOSE 8000
CMD ["uvicorn", "rss_surveillance_agent.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
