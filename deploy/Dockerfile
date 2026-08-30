FROM python:3.12-slim

RUN useradd --create-home --uid 1000 appuser
WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --extra web

ENV PATH="/app/.venv/bin:${PATH}"

RUN mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser
VOLUME ["/app/data"]

ENTRYPOINT ["gcal-sync"]
CMD ["start"]
