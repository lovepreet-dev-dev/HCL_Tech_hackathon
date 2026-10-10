FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# CPU-only torch: the embedding model runs on CPU, and the default wheel adds GBs of CUDA libraries.
RUN pip install --no-cache-dir --prefix=/install \
    --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# ── Runtime stage ──────────────────────────────────────────────────────────

FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /install /usr/local

RUN useradd --create-home --shell /bin/bash appuser

COPY app/ app/
COPY data/ data/
COPY usecase3/ usecase3/

RUN mkdir -p /app/data/faiss_index /app/data/uploads /app/data/chroma \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/api/v1/health').raise_for_status()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
