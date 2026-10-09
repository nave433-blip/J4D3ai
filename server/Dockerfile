# Jade reference cloud server.
# stdlib-only runtime; cryptography enables encrypted connector-token storage.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JADE_DATA_DIR=/data

RUN useradd --create-home --uid 10001 --user-group jade \
    && mkdir -p /srv/jade /data \
    && chown -R jade:jade /srv/jade /data

WORKDIR /srv/jade
COPY server/ ./server/
COPY LICENSE ./LICENSE

RUN pip install --no-cache-dir -r server/requirements.txt

USER jade
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).read()"

CMD ["python", "-m", "server.app"]
