FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 deployforge \
    && useradd --uid 10001 --gid 10001 --create-home deployforge

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
RUN pip install --no-cache-dir .

USER deployforge
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS test
USER root
RUN pip install --no-cache-dir ".[dev]"
COPY tests ./tests
COPY examples ./examples
ENV RUFF_CACHE_DIR=/tmp/ruff-cache \
    MYPY_CACHE_DIR=/tmp/mypy-cache
USER deployforge
CMD ["sh", "-c", "ruff check . && ruff format --check . && mypy app && pytest -p no:cacheprovider"]

FROM base AS runtime
