"""Failover transport: carries one client request through the node pool
under the failover rule until an upstream response succeeds or every node
is exhausted.

Framework-free: callers hand over request primitives and get back a
SendResult or AllNodesFailed; mapping results to HTTP responses (Flask or
otherwise) is the view's job. Selection, cooldown accounting, and key
ownership stay in the rotator's NodeSelector/HealthLedger — this module
only drives attempts and classifies outcomes.
"""

import logging
import random
import time
from dataclasses import dataclass, field

import requests
from requests.exceptions import ConnectionError, RequestException, Timeout

logger = logging.getLogger(__name__)

_HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
    "date", "server", "content-encoding", "content-length",
})

# Client headers that must never be forwarded upstream; Authorization is
# injected per attempt and anything host/framing-related is the transport's
# business, not the client's.
_OUTBOUND_DROPPED_HEADERS = frozenset({"host", "content-length"})

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def parse_retry_after(value):
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def compute_backoff(attempt, retry_after=None, rng=random.uniform,
                    backoff_base=0.5, backoff_max=8.0):
    if retry_after is not None:
        return min(retry_after, backoff_max)
    delay = backoff_base * (2 ** attempt) + rng(0, backoff_base)
    return min(delay, backoff_max)


@dataclass
class SendResult:
    """One successful upstream response.

    body() returns bytes when sent with stream=False, or a chunk iterator
    when stream=True; either way the underlying response is closed exactly
    once, after the body is consumed.
    """
    status_code: int
    header_pairs: list
    node_id: int
    _response: requests.Response = field(repr=False)
    _streamed: bool = field(repr=False)

    def body(self):
        if self._streamed:
            return self._iter_chunks()
        content = self._response.content
        self._response.close()
        return content

    def _iter_chunks(self):
        try:
            # chunk_size=1: larger sizes do exact blocking reads and
            # buffer small SSE events until EOF, killing latency.
            for chunk in self._response.iter_content(chunk_size=1):
                yield chunk
        finally:
            self._response.close()


@dataclass
class AllNodesFailed:
    last_error: str


class FailoverTransport:
    """
    Drives the retry/failover loop: picks nodes from the selector, injects
    each node's API key, routes through its egress, classifies the outcome,
    reports failures/successes to the ledger, and paces retries with
    Retry-After-aware exponential backoff. Session, sleeper, and jitter rng
    are injectable so the whole loop is testable without HTTP or real time.
    """

    def __init__(self, selector, ledger, session=None, sleep=time.sleep,
                 rng=random.uniform, max_retries=4, timeout=25.0,
                 backoff_base=0.5, backoff_max=8.0):
        self.selector = selector
        self.ledger = ledger
        self.session = session if session is not None else requests.Session()
        self.sleep = sleep
        self.rng = rng
        self.max_retries = max_retries
        self.timeout = timeout
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max

    def send(self, method, url, headers, payload=b"", cookies=None,
             stream=False):
        last_error = "no attempt was made (max_retries is 0)"
        for attempt in range(self.max_retries):
            node = self.selector.select()

            request_headers = {
                k: v for k, v in headers.items()
                if k.lower() not in _OUTBOUND_DROPPED_HEADERS
            }
            request_headers["Authorization"] = f"Bearer {node.api_key}"
            proxies = {"http": node.proxy, "https": node.proxy}

            logger.info(
                f"Attempt {attempt + 1}/{self.max_retries}: Routing via Node {node.node_id} "
                f"({node.proxy.split('://')[1].split(':')[0]})"
            )

            retry_after = None
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    data=payload,
                    cookies=cookies,
                    proxies=proxies,
                    timeout=self.timeout,
                    stream=stream,
                )
            except Timeout as e:
                logger.error(f"Timeout on Node {node.node_id}: {str(e)}. Retrying...")
                last_error = f"Timeout: {str(e)}"
                self.ledger.record_failure(node)
            except ConnectionError as e:
                logger.error(f"Connection error on Node {node.node_id}: {str(e)}. Retrying...")
                last_error = f"Connection error: {str(e)}"
                self.ledger.record_failure(node)
            except RequestException as e:
                logger.error(f"Request failed on Node {node.node_id}: {str(e)}. Retrying...")
                last_error = f"Request error: {str(e)}"
                self.ledger.record_failure(node)
            else:
                if response.status_code in RETRY_STATUSES:
                    logger.warning(
                        f"Node {node.node_id} returned HTTP {response.status_code}. "
                        f"Retrying with next node..."
                    )
                    last_error = f"Upstream error: {response.status_code}"
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    response.close()
                    self.ledger.record_failure(node)
                else:
                    self.ledger.record_success(node)
                    return SendResult(
                        status_code=response.status_code,
                        header_pairs=[
                            (k, v) for k, v in response.headers.items()
                            if k.lower() not in _HOP_BY_HOP_HEADERS
                        ],
                        node_id=node.node_id,
                        _response=response,
                        _streamed=stream,
                    )

            if attempt < self.max_retries - 1:
                self.sleep(compute_backoff(
                    attempt,
                    retry_after,
                    rng=self.rng,
                    backoff_base=self.backoff_base,
                    backoff_max=self.backoff_max,
                ))

        return AllNodesFailed(last_error=last_error)
