ARG PYTHON_IMAGE=python:3.12-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.5

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /app

# Dependencies first, so code changes do not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY heimdall ./heimdall
COPY docs/dependencies.yaml ./docs/dependencies.yaml
RUN uv sync --locked --no-dev

RUN useradd --uid 10001 --no-create-home heimdall && mkdir /data && chown heimdall /data
USER 10001
EXPOSE 8000
CMD ["heimdall", "serve"]
