# The image. Every base is pinned by digest; uv is pinned to the exact version that
# required-version in pyproject.toml starts at (ADR 0006). No syntax directive: it would
# pull an unpinned frontend image. tests/test_dockerfile.py reads this file; `make
# docker-build` builds and smoke-tests it.

FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.15.0-slim-trixie@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58 AS build
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

FROM python:3.15.0-slim-trixie@sha256:f5b92f2a669a4c347044f29367f31983280b648250ca94dea66f98c53bc87e58 AS runtime
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
