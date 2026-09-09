FROM python:3.12-slim

WORKDIR /app

# System deps for lxml / pdfplumber build steps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml requirements.txt ./
COPY src ./src
COPY run_server.py ./

# Install core + AI extras (document analysis: pdfplumber, chromadb, sentence-transformers).
# This is the heavy step (~1-2GB with torch) — expect a slow first build.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[ai]"

# Pre-download the sentence-transformers embedding model at build time so the
# first request doesn't pay a multi-second cold-start download from HF Hub.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

ENV MCP_TRANSPORT=streamable-http
ENV HF_HOME=/app/.cache/huggingface

# EXPOSE here is documentation only; the app reads PORT/MCP_PORT at runtime.
EXPOSE 9000

CMD ["python", "run_server.py"]
