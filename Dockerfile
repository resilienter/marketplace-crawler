FROM python:3.11-slim AS builder
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --upgrade pip && pip install --prefix=/install ".[stealth]"

FROM python:3.11-slim AS runner
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd -g 1001 appuser && useradd -u 1001 -g appuser -m appuser \
    && mkdir -p /app/data /app/dumps /app/logs /app/config && chown -R appuser:appuser /app
COPY --from=builder /install /usr/local
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser tests/ ./tests/
USER appuser
CMD ["python", "-m", "src.main"]
