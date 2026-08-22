# Tailscale LLM Rotator

A Flask reverse proxy that rotates OpenAI-compatible LLM requests across multiple
Tailscale egress nodes: thread-safe round-robin selection, per-node API-key
injection, automatic failover on 429/5xx/timeouts with backoff and per-node
cooldowns, plus an optional token-compression pipeline for `/v1/chat/completions`.

> **Agent-facing docs:** [`AGENTS.md`](AGENTS.md) is the source of truth for
> code layout, gotchas, testing, and deployment quirks. This README is the
> human-facing overview; if the two disagree, trust `rotator.py`/`failover.py`.

## How it works

- **Nodes** — each entry in your node pool pairs one egress (a Tailscale SOCKS5
  endpoint) with the API key to spend through it. Nodes are numbered contiguously
  from 1 (`PROXY_1_URL`/`API_KEY_1`, `PROXY_2_URL`/`API_KEY_2`, ...).
- **Rotation** — requests advance a round-robin cursor across usable nodes.
- **Failover** — on 429/500/502/503/504, timeouts, or connection errors, the
  request retries on the next usable node with exponential backoff, honoring
  `Retry-After`. Failed nodes enter an exponentially growing cooldown; if every
  node is cooling, rotation serves the cursor node anyway (never-starve).
- **Optimization** — chat/completions payloads can pass through a six-stage
  compression pipeline (dedup → whitespace → semantic compression → prompt
  caching → importance filtering → summarization/truncation), pure
  payload-in/payload-out, never breaking the proxied request.

## Prerequisites

- Python **3.10+**
- [Tailscale](https://tailscale.com) installed and authenticated
- One or more Tailscale nodes running SOCKS5 proxies
- API keys for each node's LLM provider account

## Installation

### Option A: Direct

```bash
pip install -r requirements.txt   # flask, gunicorn, requests, tiktoken
cp .env.example .env              # then edit with real values
python rotator.py                 # binds PROXY_BIND_HOST:PROXY_BIND_PORT
```

Production runs use gunicorn:

```bash
gunicorn -c gunicorn.conf.py rotator:app
```

### Option B: Docker Compose

```bash
cp .env.example .env    # edit with real values
docker-compose up -d --build
```

Docker uses `network_mode: host` — Tailscale's 100.64.x.x addresses aren't
reachable through bridge networking.

### Option C: Manual Docker

```bash
docker build -t llm-rotator .
docker run -d \
  --name llm-rotator \
  --network host \
  --env-file .env \
  --restart unless-stopped \
  llm-rotator
```

## Endpoints

| Endpoint | Methods | Description |
|----------|---------|-------------|
| `/v1/<path>` | GET, POST, PUT, DELETE, PATCH | Proxied upstream call with rotation + failover |
| `/v1/models` | GET | Model listing with full failover |
| `/health` | GET | Process health + per-node status (stays open when auth is on) |
| `/ready` | GET | Readiness: 503 while every node is in cooldown |

Point any OpenAI-compatible client at `http://127.0.0.1:8080/v1`.

### `/health` response shape

```json
{
  "status": "healthy",
  "nodes_configured": 2,
  "nodes_available": 1,
  "current_node_index": 1,
  "nodes": [
    {"node_id": 1, "proxy": "socks5h://100.64.0.1:1055", "consecutive_failures": 3, "cooldown_seconds": 14.5},
    {"node_id": 2, "proxy": "socks5h://100.64.0.2:1055", "consecutive_failures": 0, "cooldown_seconds": 0.0}
  ],
  "token_optimization_enabled": true,
  "max_context_tokens": 128000,
  "reserved_response_tokens": 4096
}
```

## Configuration

All configuration is environment variables; `.env.example` matches the code.
Node vars must be contiguous from 1 — the loader stops at the first missing
index and silently drops later nodes.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_BIND_HOST` | `127.0.0.1` | Bind host — both `python rotator.py` and gunicorn read the same var with the same loopback fallback; set `0.0.0.0` only if you truly want all interfaces |
| `PROXY_BIND_PORT` | `8080` | Bind port (the compose healthcheck follows this) |
| `LLM_PROVIDER_URL` | `https://api.openai.com/v1` | Upstream provider base URL |
| `MAX_RETRIES` | `4` | Attempts across nodes per request |
| `REQUEST_TIMEOUT` | `25.0` | Per-attempt upstream timeout (seconds) |
| `RETRY_BACKOFF_BASE` | `0.5` | Failover backoff base (seconds) |
| `RETRY_BACKOFF_MAX` | `8.0` | Failover backoff cap (seconds); a `Retry-After` header overrides up to this cap |
| `NODE_COOLDOWN_BASE` | `2.0` | First-failure cooldown (seconds); doubles per consecutive failure |
| `NODE_COOLDOWN_MAX` | `60.0` | Cooldown cap (seconds) |
| `RETRY_POSTS` | `true` | `false` gives POSTs exactly one attempt — a 504/timeout may mean the upstream completed, so verbatim retries can double-bill |
| `DEFAULT_MODEL` | `gpt-4o` | Model used for token counting |
| `LOG_LEVEL` | `INFO` | Logging level |
| `PROXY_AUTH_TOKEN` | *(empty)* | Optional bearer gate: when set, `/v1/*` requires `Authorization: Bearer <token>` (401 otherwise). `/health` and `/ready` stay open |

### Nodes (repeat contiguously from 1)

| Variable | Required | Description |
|----------|----------|-------------|
| `PROXY_N_URL` | ✅ | Egress URL, e.g. `socks5h://100.64.0.1:1055` (`socks5h` = DNS resolved remotely) |
| `API_KEY_N` | ✅ | API key injected as `Authorization: Bearer …` for node N |

### Gunicorn (`gunicorn.conf.py`)

| Variable | Default | Description |
|----------|---------|-------------|
| `GUNICORN_WORKERS` | `1` | Worker processes |
| `GUNICORN_THREADS` | `8` | Threads per worker |
| `GUNICORN_TIMEOUT` | `60` | Worker timeout (seconds) |
| `GUNICORN_GRACEFUL_TIMEOUT` | `30` | Graceful shutdown window (seconds) |

### Optimization pipeline (`OptimizationConfig`)

Provider profiles preset the context budget; explicit env vars win over profile
defaults.

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_CONTEXT_COMPRESSION` | `true` | Master switch |
| `PROVIDER_PROFILE` | `openai` | Budget presets: `openai` (128k/4k), `anthropic` (200k/8k), `groq` (32k/2k) |
| `MAX_CONTEXT_TOKENS` | profile | Context ceiling for optimization decisions |
| `RESERVED_RESPONSE_TOKENS` | profile | Tokens reserved for the model's reply |
| `COMPRESSION_THRESHOLD` | `0.85` | Summarize/truncate when context exceeds this fraction of budget |
| `REMOVE_DUPLICATE_MESSAGES` | `true` | Collapse consecutive duplicate messages |
| `STRIP_WHITESPACE` | `true` | Normalize whitespace runs |
| `ENABLE_STREAMING_FASTPATH` | `true` | Streaming requests skip expensive stages (hygiene only) |
| `ENABLE_SEMANTIC_COMPRESSION` | `false` | LLMLingua compression (optional dependency, pulls torch) |
| `SEMANTIC_COMPRESSION_RATIO` | `0.5` | Target compression ratio |
| `ENABLE_PROMPT_CACHING` | `false` | Anthropic-style `cache_control` markers — OpenAI rejects these; off unless explicitly enabled |
| `ENABLE_IMPORTANCE_SCORING` | `false` | Score-and-filter messages by recency/length/role/keywords |
| `MIN_MESSAGE_IMPORTANCE` | `0.3` | Filter threshold (system prompts always kept) |
| `ENABLE_RECURSIVE_SUMMARIZATION` | `false` | Summarize older context near the budget |
| `SUMMARIZATION_MODEL` | `gpt-4o-mini` | Reserved for summarization cost |

Notes:
- The pipeline is **pure** — input payloads are never mutated — and **never
  breaks a proxied request**: on internal failure it logs and forwards the
  payload unoptimized.
- Client cookies are never forwarded upstream, and the proxy stores no
  upstream cookies.
- `max_tokens` may be clamped down to fit the remaining context budget
  (logged when it happens).

## Security

- The proxy spends every node's API key on behalf of anyone who can reach the
  bind address. It binds loopback by default; set `PROXY_AUTH_TOKEN` before
  ever exposing it, and prefer not to expose it at all.
- Compose deliberately uses `network_mode: host` for Tailscale reachability —
  mind what else listens on the host.
- Secrets live in `.env` (gitignored); keys are injected per attempt and are
  excluded from logs and `/health`.
- The container runs as a non-root user.

## Troubleshooting

```bash
# Is anything usable?
curl -s http://127.0.0.1:8080/health | jq '{nodes_available, nodes}'

# Orchestrator view
curl -si http://127.0.0.1:8080/ready | head -1

# Watch failover/cooldown decisions
LOG_LEVEL=DEBUG python rotator.py
```

- **All nodes failing**: `tailscale status`; check each SOCKS5 listener;
  `curl --socks5-hostname 100.64.0.1:1055 https://api.openai.com/v1/models`.
- **401s you didn't expect**: `PROXY_AUTH_TOKEN` is set — send
  `Authorization: Bearer <token>`.
- **Double-billed completions**: set `RETRY_POSTS=false` (see trade-off above).
- **First start hangs on tokenizer download**: tiktoken fetches its BPE file
  unless cached — the Docker image pre-bakes it via `TIKTOKEN_CACHE_DIR`;
  bare-metal needs network or a warm cache.

## Testing

```bash
pip install pytest
python -m pytest tests/ -v
```

The suite is fully offline: node 1 is a dead port (exercising failover), node 2
is a scripted mock upstream, and the transport/pipeline units run without HTTP
or real sleeps.

## Contributing

1. Branch per ticket (`git checkout -b implement-<slug>`)
2. Tests green at every commit (`pytest tests/`, `python -m py_compile rotator.py failover.py`)
3. PR referencing the issue whose acceptance criteria the diff implements

## License

MIT — see [LICENSE](LICENSE).
