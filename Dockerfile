FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    PYTHONPATH=/app/src

WORKDIR /app

# Install build tools only if needed for cryptg/native wheels, then clean up
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    (pip install cryptg || true)

COPY src ./src
COPY pyproject.toml README.md ./

RUN mkdir -p /app/data && \
    useradd --create-home --shell /bin/bash botuser && \
    chown -R botuser:botuser /app

USER botuser

VOLUME ["/app/data"]

CMD ["python", "-m", "qna_bot"]
