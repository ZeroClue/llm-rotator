import os

# Container convention is to bind all interfaces; the compose file narrows
# this to loopback because the proxy itself has no authentication.
bind = f"{os.getenv('PROXY_BIND_HOST', '0.0.0.0')}:{os.getenv('PROXY_BIND_PORT', '8080')}"

worker_class = "gthread"
workers = int(os.getenv("GUNICORN_WORKERS", "1"))
threads = int(os.getenv("GUNICORN_THREADS", "8"))
# Streaming SSE responses hold a thread for the whole completion; the gthread
# worker keeps heartbeating from its main loop, so the arbiter timeout only
# guards against a fully stalled worker, not long-lived requests.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "60"))
graceful_timeout = int(os.getenv("GUNICORN_GRACEFUL_TIMEOUT", "30"))
accesslog = "-"
errorlog = "-"
