FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cache layer)
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir ".[local]"

# Default config directory
RUN mkdir -p /data
ENV HIPPO_DB_PATH=/data/memory.db

EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8200/health')" || exit 1

CMD ["uvicorn", "openhippo.api.rest:app", "--host", "0.0.0.0", "--port", "8200"]
