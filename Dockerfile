FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    PYTHONPATH=/app/src

WORKDIR /app

# gcc/libssl-dev: wheels for cryptg and friends. util-linux: setpriv, which the
# entrypoint uses to drop from root to `botuser` after fixing a mounted volume's
# ownership. One layer, so apt metadata is cleaned once.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libssl-dev \
    util-linux \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    (pip install cryptg || true)

COPY src ./src
COPY pyproject.toml README.md ./

COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

RUN mkdir -p /app/data && \
    useradd --create-home --shell /bin/bash botuser && \
    chown -R botuser:botuser /app

VOLUME ["/app/data"]

# Deliberately no `USER botuser`: the entrypoint starts as root, gives DATA_DIR to
# botuser, and then drops to botuser via setpriv. A mounted volume arrives
# root-owned on Kubernetes platforms (Northflank), and a container already running
# as botuser cannot fix that - it dies on the first mkdir. The app itself never
# runs as root.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "qna_bot"]
