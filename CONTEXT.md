# LLM Rotator

A reverse proxy that rotates OpenAI-compatible LLM requests across multiple
egress paths, injecting each node's API key and failing over on rate limits,
server errors, and timeouts.

## Language

**Node**:
One upstream target: an egress paired with the API key to spend through it,
identified by `node_id`, a stable 1-based integer fixed when the pool is built.
A node carries no failure history itself; that lives in the health ledger.
_Avoid_: upstream, backend, host

**Egress**:
The network path (a Tailscale SOCKS5 endpoint) a request travels through to
reach the LLM provider. Each node has exactly one.
_Avoid_: proxy (reserved for this service itself)

**Proxy**:
This service — the rotator sitting between OpenAI-compatible clients and the
nodes. Never use it for a node's SOCKS5 address; that is the node's _egress_
(even where env var names like `PROXY_1_URL` predate this distinction).

**Node pool**:
The ordered set of configured nodes, built from `PROXY_N_URL`/`API_KEY_N`
pairs numbered contiguously from 1.

**Rotation**:
Round-robin selection across the node pool; each request advances the cursor.

**Cursor**:
The pool position where the next selection starts scanning for a usable node.

**Failover**:
Retrying a failed request (429/5xx/timeout/connection error) on the next
usable node instead of returning the error to the client.

**Transport**:
The framework-agnostic machinery that carries one client request through
nodes under the failover rule — selection, key injection, egress wiring,
status classification, retry pacing — and hands back either an upstream
response or exhaustion. Knows nothing about HTTP frameworks or prompt
contents.

**Cooldown**:
The period after a node failure during which rotation skips that node;
grows exponentially with consecutive failures up to a cap.

**Health ledger**:
The per-node record of consecutive failures and cooldown deadlines — the
authority rotation consults to decide whether a node is usable. Distinct
from the cursor, which only tracks pool position.

**Never-starve rule**:
When every node is in cooldown, rotation uses the cursor node anyway rather
than rejecting the request.
