# Override image references at build time, never pass credentials as build args.
ARG PYTHON_IMAGE=docker.io/library/python:3.13.15-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.19
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS builder
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --locked --no-dev --no-default-groups --no-editable

FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --home-dir /app app
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
USER app
STOPSIGNAL SIGTERM
ENTRYPOINT ["aquaservice2mqtt"]
CMD ["run"]
