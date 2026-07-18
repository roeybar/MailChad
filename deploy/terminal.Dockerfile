# MailChad terminal image - local admin UI + sync client.
# Build context: repo root.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates sqlite3 \
    wireguard-tools iproute2 iptables \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Runtime dependencies
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# src/ layout - mailchad.terminal / mailchad.cloud import unambiguously
COPY src/                  ./src/
COPY tests                 ./tests

ENV PYTHONPATH=/app/src

COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

CMD ["/entrypoint.sh"]
