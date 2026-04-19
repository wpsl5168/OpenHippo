FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/

# Install core only (no torch/sentence-transformers — use Ollama for embeddings in Docker)
RUN pip install --no-cache-dir .

RUN mkdir -p /data
ENV HIPPO_DB_PATH=/data/memory.db
ENV HIPPO_EMBEDDING_PROVIDER=ollama

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8200/health')" || exit 1

CMD ["uvicorn", "openhippo.api.rest:app", "--host", "0.0.0.0", "--port", "8200"]
