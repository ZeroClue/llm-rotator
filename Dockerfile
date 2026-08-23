# Secure Tailscale LLM Proxy Rotator
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy only requirements first (for layer caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code (dashboard templates/assets included — create_app
# imports admin_dashboard at startup, so a missing file kills the worker)
COPY rotator.py failover.py telemetry.py admin_dashboard.py gunicorn.conf.py ./
COPY templates/ templates/
COPY static/ static/

# Pre-cache the tiktoken BPE file for the default model so first startup
# never stalls on a tokenizer download (matters on egress-restricted hosts).
# The build fails here if the cache directory ends up empty.
ENV TIKTOKEN_CACHE_DIR=/app/.tiktoken-cache
RUN python -c "import tiktoken; assert tiktoken.encoding_for_model('gpt-4o').encode('hello')" \
    && test -n "$(ls -A /app/.tiktoken-cache)" && echo "tokenizer pre-cached"

# Create non-root user for security
RUN useradd --create-home --shell /usr/sbin/nologin appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Health check via stdlib (no curl needed in the image); honors PROXY_BIND_PORT
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; port=os.getenv('PROXY_BIND_PORT','8080'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4).status==200 else 1)" || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "rotator:app"]
