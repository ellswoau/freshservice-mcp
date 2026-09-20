# FreshService MCP server — containerised.
# Runs as a non-root user. stdio (embedded MCP client) by default; override the
# CMD to run as a network daemon: --transport http|sse|streamable-http.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Create an unprivileged runtime user.
RUN groupadd -r freshservice && useradd -r -g freshservice -d /app freshservice

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY freshservice_mcp ./freshservice_mcp
RUN python -m compileall -q freshservice_mcp

USER freshservice

# HTTP/SSE transports bind here.
EXPOSE 8000

# Default: stdio transport. Override CMD for a daemon:
#   docker run -p 8000:8000 freshservice-mcp --transport http --port 8000
ENTRYPOINT ["python", "-m", "freshservice_mcp"]
CMD []

# Healthcheck: succeeds only when the daemon is listening and /health returns
# HTTP 200 (matters for the http/sse daemon modes; harmless otherwise).
HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:' + __import__('os').environ.get('FRESHSERVICE_PORT','8000') + '/health', timeout=6)" || exit 1