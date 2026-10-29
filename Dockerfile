# The image. Every base is pinned by digest; uv is pinned to the exact version that
# required-version in pyproject.toml starts at (ADR 0006). No syntax directive: it would
# pull an unpinned frontend image. tests/test_dockerfile.py reads this file; `make
# docker-build` builds and smoke-tests it.

FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07 AS build
COPY --from=uv /uv /usr/local/bin/uv
# The base image's Python, a venv outside the source tree, no cache, no downloads of
# Python itself, and the lock as the only source of versions.
ENV UV_LOCKED=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY app ./app
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07 AS runtime
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /opt/venv /opt/venv
# HOST=0.0.0.0 is the container's own interface; `make docker-build` publishes it on
# 127.0.0.1 only. DEV_AUTH stays off in the image (PR 7 removes the shim from it).
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    DEV_AUTH=0 \
    TRACE_PLAINTEXT=0
USER 10001:10001
EXPOSE 8000
CMD ["python", "-P", "-m", "app"]
