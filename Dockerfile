# The image. Every base is pinned by digest; uv is pinned to the exact version that
# required-version in pyproject.toml starts at (ADR 0006). No syntax directive: it would
# pull an unpinned frontend image. tests/test_dockerfile.py reads this file; `make
# docker-build` builds and smoke-tests it.

FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv

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
# SITREP_HOST=0.0.0.0 is the container's own interface; `make docker-build` publishes it
# on 127.0.0.1 only. The app answers only the Host 127.0.0.1:8000 unless
# SITREP_ALLOWED_HOSTS names the published address (M1 PR 5): the smoke test sets it. SITREP_DEV_AUTH stays off: .dockerignore leaves the dev login shim
# out of the image, and the entry point refuses SITREP_DEV_AUTH=1 without it (ADR 0009).
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SITREP_HOST=0.0.0.0 \
    SITREP_PORT=8000 \
    SITREP_DEV_AUTH=0 \
    SITREP_TRACE_PLAINTEXT=0
USER 10001:10001
EXPOSE 8000
CMD ["python", "-P", "-m", "app"]
