FROM ghcr.io/astral-sh/uv:0.9.5 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /srv
ENV UV_CACHE_DIR=/tmp/uv-cache UV_LINK_MODE=copy \
    UV_HTTP_TIMEOUT=300 UV_HTTP_RETRIES=5 UV_CONCURRENT_DOWNLOADS=3 \
    PYTHONUNBUFFERED=1 HF_HUB_DISABLE_XET=1
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/tmp/uv-cache,sharing=locked uv sync --frozen --no-dev
COPY app ./app
COPY documents ./documents
RUN useradd --uid 10001 --create-home app && mkdir -p .cache && chown -R app:app /srv
USER app
EXPOSE 8000
CMD [".venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
