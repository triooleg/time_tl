FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv

RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TIME_TL_DATA_DIR=/app/data \
    PATH="/opt/venv/bin:$PATH"

RUN groupadd --system --gid 10001 time_tl \
    && useradd --system --uid 10001 --gid time_tl --home-dir /app --no-create-home time_tl

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini ./
COPY migrations ./migrations
COPY data ./data
COPY docs ./docs
USER 10001:10001

ENTRYPOINT ["time-tl"]
