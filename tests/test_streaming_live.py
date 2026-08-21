import json
import os
import socket
import subprocess
import sys
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mock_upstream import MockUpstream

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESSAGES = [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello"},
]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def live(mock=None):
    upstream = MockUpstream(chunk_delay=0.15)
    upstream.start()
    bind_port = _free_port()
    env = os.environ.copy()
    for var in list(env):
        if var.startswith(("PROXY_", "API_KEY_", "LLM_PROVIDER_URL", "MAX_RETRIES")):
            del env[var]
    env.update({
        "PROXY_BIND_HOST": "127.0.0.1",
        "PROXY_BIND_PORT": str(bind_port),
        "LLM_PROVIDER_URL": upstream.url("/v1"),
        "PROXY_1_URL": f"http://127.0.0.1:{upstream.port}",
        "API_KEY_1": "live-key",
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO_ROOT, "rotator.py")],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{bind_port}"
    for _ in range(40):
        try:
            if requests.get(f"{base}/health", timeout=1).status_code == 200:
                break
        except Exception:
            time.sleep(0.25)
    else:
        proc.terminate()
        raise RuntimeError("rotator did not become healthy")
    yield base, upstream, proc
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    upstream.stop()


def _timed_post(base, url_suffix, payload):
    t0 = time.time()
    arrivals = []
    with requests.post(
        f"{base}/v1/chat/completions{url_suffix}",
        json=payload, stream=True, timeout=30,
    ) as resp:
        assert resp.status_code == 200
        pieces = []
        for chunk in resp.iter_content(chunk_size=16):
            arrivals.append(time.time() - t0)
            pieces.append(chunk)
    body = b"".join(pieces).decode()
    return arrivals, body


def test_body_stream_true_streams_incrementally(live):
    base, _, _ = live
    arrivals, body = _timed_post(base, "", {"model": "gpt-4o", "stream": True, "messages": MESSAGES})
    span = arrivals[-1] - arrivals[0]
    assert "[DONE]" in body and body.count("part") >= 5
    assert span >= 0.3, f"response was buffered: first={arrivals[0]:.2f}s last={arrivals[-1]:.2f}s"


def test_query_param_stream_still_streams(live):
    base, _, _ = live
    arrivals, body = _timed_post(
        base, "?stream=true", {"model": "gpt-4o", "messages": MESSAGES},
    )
    span = arrivals[-1] - arrivals[0]
    assert "[DONE]" in body
    assert span >= 0.3, f"query-param streaming regressed to buffering: {arrivals[0]:.2f}s..{arrivals[-1]:.2f}s"


def test_non_streaming_response_is_buffered_json_with_usage_log(live):
    base, upstream, proc = live
    resp = requests.post(
        f"{base}/v1/chat/completions",
        json={"model": "gpt-4o", "messages": MESSAGES}, timeout=30,
    )
    assert resp.status_code == 200
    assert resp.json()["usage"]["total_tokens"] == 10
