"""Live shutdown tests: SIGTERM while SSE streams are in flight must yield
either the complete body (finished within STREAM_DRAIN_WINDOW) or the
terminal proxy_shutdown event — never a silent truncation. Covers both
entrypoints: gunicorn master and the bare werkzeug dev server."""

import os
import signal
import socket
import subprocess
import sys
import threading
import time

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mock_upstream import MockUpstream

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MESSAGES = [{"role": "user", "content": "Hello"}]
TERMINAL_MARK = b"proxy_shutdown"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _scrubbed_env():
    env = os.environ.copy()
    for var in list(env):
        if var.startswith((
            "PROXY_", "API_KEY_", "LLM_PROVIDER_URL", "MAX_RETRIES",
            "STREAM_DRAIN_WINDOW", "GUNICORN_",
        )):
            del env[var]
    return env


def _stream_in_thread(base, arrivals):
    def run():
        t0 = time.time()
        with requests.post(
            f"{base}/v1/chat/completions",
            json={"model": "gpt-4o", "stream": True, "messages": MESSAGES},
            stream=True, timeout=30,
        ) as resp:
            assert resp.status_code == 200
            pieces = []
            for chunk in resp.iter_content(chunk_size=16):
                arrivals.append((time.time() - t0, chunk))
                pieces.append(chunk)
        arrivals.append((time.time() - t0, b"<<EOF>>"))
        arrivals.append(b"".join(pieces))
    thread = threading.Thread(target=run)
    thread.start()
    return thread


@pytest.fixture(scope="module")
def slow_mock():
    upstream = MockUpstream(chunk_delay=0.1)
    upstream.sse_parts = 40  # ~4s stream: outlives a 1s drain window
    upstream.start()
    yield upstream
    upstream.stop()


def _wait_healthy(proc, base, attempts=60):
    for _ in range(attempts):
        try:
            if requests.get(f"{base}/health", timeout=1).status_code == 200:
                return
        except Exception:
            time.sleep(0.25)
    proc.terminate()
    raise RuntimeError(f"server at {base} did not become healthy")


def _spawn_gunicorn(upstream, drain_window, graceful_timeout):
    bind_port = _free_port()
    env = _scrubbed_env()
    env.update({
        "PROXY_BIND_HOST": "127.0.0.1",
        "PROXY_BIND_PORT": str(bind_port),
        "LLM_PROVIDER_URL": upstream.url("/v1"),
        "PROXY_1_URL": f"http://127.0.0.1:{upstream.port}",
        "API_KEY_1": "live-key",
        "STREAM_DRAIN_WINDOW": str(drain_window),
        "GUNICORN_GRACEFUL_TIMEOUT": str(graceful_timeout),
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "gunicorn", "-c", "gunicorn.conf.py", "rotator:app"],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{bind_port}"
    _wait_healthy(proc, base)
    return proc, base


def _stop(proc, timeout):
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise AssertionError(f"server did not exit within {timeout}s")


def test_gunicorn_stream_finishing_within_window_gets_full_body(slow_mock):
    slow_mock.sse_parts = 5  # ~0.5s total; fits easily inside the window
    proc, base = _spawn_gunicorn(slow_mock, drain_window=5, graceful_timeout=10)
    try:
        arrivals = []
        reader = _stream_in_thread(base, arrivals)
        while len(arrivals) < 1:
            time.sleep(0.05)
        os.kill(proc.pid, signal.SIGTERM)  # master; workers inherit the stop
        reader.join(timeout=15)
        assert not reader.is_alive()
        body = arrivals[-1]
        assert body.count(b"part") >= 5
        assert b"[DONE]" in body
        assert TERMINAL_MARK not in body
        _stop(proc, timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_gunicorn_stream_outliving_window_gets_terminal_event_before_hard_kill(slow_mock):
    slow_mock.sse_parts = 40
    drain_window, graceful_timeout = 1, 8
    proc, base = _spawn_gunicorn(slow_mock, drain_window, graceful_timeout)
    try:
        arrivals = []
        reader = _stream_in_thread(base, arrivals)
        while len(arrivals) < 1:
            time.sleep(0.05)
        term_at = time.time()
        os.kill(proc.pid, signal.SIGTERM)
        reader.join(timeout=graceful_timeout + 5)
        assert not reader.is_alive(), "stream was still open at hard-kill time"
        cut_latency = arrivals[-2][0]  # the <<EOF>> marker's timestamp
        body = arrivals[-1]
        assert TERMINAL_MARK in body
        assert body.rstrip().endswith(b"[DONE]")
        # Terminal event flushed well inside gunicorn's kill window.
        assert cut_latency <= drain_window + 2.0, (
            f"cut arrived {cut_latency:.1f}s after first chunk; "
            f"drain window is {drain_window}s"
        )
        _stop(proc, timeout=graceful_timeout + 5)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_bare_dev_server_survives_sigterm_and_delivers_terminal_event(slow_mock):
    slow_mock.sse_parts = 40
    bind_port = _free_port()
    env = _scrubbed_env()
    env.update({
        "PROXY_BIND_HOST": "127.0.0.1",
        "PROXY_BIND_PORT": str(bind_port),
        "LLM_PROVIDER_URL": slow_mock.url("/v1"),
        "PROXY_1_URL": f"http://127.0.0.1:{slow_mock.port}",
        "API_KEY_1": "live-key",
        "STREAM_DRAIN_WINDOW": "1",
        "PYTHONUNBUFFERED": "1",
    })
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO_ROOT, "rotator.py")],
        cwd=REPO_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{bind_port}"
    try:
        _wait_healthy(proc, base)

        arrivals = []
        reader = _stream_in_thread(base, arrivals)
        while len(arrivals) < 1:
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)  # previously: instant death mid-stream
        reader.join(timeout=15)
        assert not reader.is_alive()
        body = arrivals[-1]
        assert TERMINAL_MARK in body
        assert body.rstrip().endswith(b"[DONE]")
        _stop(proc, timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
