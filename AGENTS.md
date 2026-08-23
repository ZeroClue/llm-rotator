# AGENTS.md

Flask proxy that rotates OpenAI-compatible LLM requests across Tailscale SOCKS5 egress nodes: thread-safe round-robin, per-node API-key injection, failover on 429/5xx/timeouts with backoff and per-node cooldowns, plus an optional token-compression pipeline applied to `/v1/chat/completions` payloads. `rotator.py` holds the app, node pool (`Node`/`HealthLedger`/`NodeSelector`), and views; `failover.py` holds the framework-free retry/failover transport (`FailoverTransport.send()` → `SendResult | AllNodesFailed`). All configuration is environment variables; `.env.example` matches the actual code.

## Planning

`ROADMAP.md` tracks prioritized future work with acceptance tests. Check it before proposing or building new features (avoid duplicating planned items), and flip item statuses (`todo`/`in progress`/`done`) as part of any change that affects them. Agent workflow docs: `docs/agents/`.

## Commands

```bash
# Dependencies — requirements.txt is a real pinned file now:
pip install -r requirements.txt    # flask, gunicorn, requests, tiktoken
pip install pytest                 # test-only

# Only static check available:
python3 -m py_compile rotator.py failover.py

# Test suite — fully offline (mock upstream, no Tailscale needed):
python3 -m pytest tests/ -v

# Manual smoke test — nodes are contacted only when a request is proxied:
PROXY_1_URL=socks5h://127.0.0.1:9 API_KEY_1=dummy python3 rotator.py &
curl -s http://127.0.0.1:8080/health
```

Production runs use gunicorn (`gunicorn.conf.py`, gthread workers): `gunicorn -c gunicorn.conf.py rotator:app`. The config reads `PROXY_BIND_HOST`/`PROXY_BIND_PORT`/`GUNICORN_*` from the environment — same env contract as the app.

## Gotchas

- **README.md and PR_DESCRIPTION.md are aspirational and diverge from the code — trust `rotator.py`.** README documents env vars that don't exist (`ENABLE_STRUCTURAL_HYGIENE`, `ENABLE_SMART_TRUNCATION`, `MIN_MESSAGE_LENGTH`, `LLMLINGUA_*`, `IMPORTANCE_KEYWORDS`, `SYSTEM_PROMPT_WEIGHT`, `RECENT_MESSAGES_WEIGHT`, `ENABLE_SUMMARIZATION`, `MAX_SUMMARY_TOKENS`, `STREAMING_CHUNK_SIZE`, `PROMPT_CACHE_TTL`) and a `/health/detailed` endpoint. Actual endpoints: `/v1/<path>`, `/v1/models`, `/health` (which includes a per-node `nodes` array).
- **Bind vars are `PROXY_BIND_HOST`/`PROXY_BIND_PORT`**, not the README's `BIND_HOST`/`BIND_PORT`.
- **Requires Python >= 3.10** despite README saying 3.8+: `dict | None` annotations are evaluated eagerly.
- **Importing `rotator` is side-effect-free**: nothing is parsed or built at import; `get_app()` (or attribute `.app`, via module `__getattr__`) runs `create_app()` once from the environment and caches. Tests get the built app through the `rotator` fixture. (`failover.py` is also safe to import bare — it builds nothing.)
- **Node env vars must be contiguous from 1** (`PROXY_1_URL`, `PROXY_2_URL`, ...): the loader stops at the first missing index, silently dropping later nodes.
- **Streaming passthrough uses `iter_content(chunk_size=1)` on purpose** (in `failover.py`'s streaming generator): requests does exact blocking reads, so any larger chunk size buffers small SSE events until EOF and destroys latency. Don't "optimize" it back to 8192.
- **Client cookies never reach upstream**: `Cookie` headers are stripped outbound and the transport's session refuses to store any `Set-Cookie` (shared jar = gthread race; stored cookies would leak across egress nodes). Don't re-add cookie passthrough without revisiting both.
- **Credential/org headers are dropped unconditionally** (`x-api-key`, `api-key`, `openai-organization`, `openai-project`): a client's real key arriving via `x-api-key` would ride upstream verbatim and defeat per-node key injection. `PERSONA_HYGIENE=true` extends the outbound drops with client telemetry (`x-stainless-*`, `x-app`, `x-title`, `http-referer`) and strips provider identity fields (`user`, `metadata`, `prompt_cache_key`, `safety_identifier`) from chat payloads — off by default while it soaks. See issue #49 / ROADMAP #19.
- **POST retry trade-off**: POSTs fail over verbatim by default (`RETRY_POSTS=true`) — a 504/timeout may mean the upstream completed, so retries can double-bill. `RETRY_POSTS=false` gives POSTs one attempt (ledger still records the outcome).
- **Prompt-caching markers are Anthropic-style** (`cache_control: {type: ephemeral}`) and OpenAI rejects them, so stage 3 is off unless `ENABLE_PROMPT_CACHING=true` is set explicitly — provider profiles never enable it.
- **`.gitignore` now has real patterns** (`.env`, `__pycache__/`, `FINDINGS.md`, caches) — but `__pycache__/` was committed earlier and stays tracked until `git rm -r --cached __pycache__` is run; ignore rules don't untrack files.
- First startup with tiktoken downloads the tokenizer file for the default model unless cached — the Docker image pre-bakes it via `TIKTOKEN_CACHE_DIR`; bare-metal first runs need network or a warm cache.
- llmlingua is intentionally absent from requirements.txt (pulls torch, ~2GB); install separately only if `ENABLE_SEMANTIC_COMPRESSION=true` is wanted.
- **Keep `STREAM_DRAIN_WINDOW` below `GUNICORN_GRACEFUL_TIMEOUT`** — the terminal SSE event must flush before gunicorn's hard kill. Arming rides SIGTERM/SIGINT handlers chained by `create_app()` because gunicorn 26.1.0's `worker_int` hook never fires on SIGTERM (only INT/QUIT; verified in pinned source). That install must run on the main thread, so `--preload` (unused here) would break it.
- **The container manager is an outer killer too**: plain `docker stop` grants 10s on standard Docker but only ~3s on Docker-29-desktop-style daemons (measured; reproduced with a stock Flask app, so not our code). If that grace expires first, streams are SIGKILLed with no terminal event. Ordering to enforce: `STREAM_DRAIN_WINDOW` < `GUNICORN_GRACEFUL_TIMEOUT` < `stop_grace_period` (compose sets 35s).

## Testing

- `tests/test_rotator.py` runs against Flask's test client with two env-configured nodes: node 1 is a dead port (exercises failover), node 2 is `MockUpstream` (`tests/mock_upstream.py`), a scriptable OpenAI-compatible upstream that captures every request.
- `tests/test_failover.py` drives `FailoverTransport` directly with a scripted fake session, recording sleeper, and stubbed rng — retry/failover logic is tested without HTTP or real sleeps.
- `tests/test_streaming_live.py` spawns `rotator.py` as a subprocess and asserts chunks arrive incrementally (TTFB well before completion); it scrubs inherited `PROXY_*`/`API_KEY_*`/`LLM_PROVIDER_URL` env vars first.
- Flag-specific tests flip module globals via `monkeypatch.setattr(rotator, ...)` instead of re-importing; transport/cooldown/optimization knobs are constructor-injected instead — build fakes explicitly.

## Deployment

- Docker needs `network_mode: host`: Tailscale's 100.64.x.x addresses aren't reachable through bridge networking. Caveat learned the hard way: on WSL2/Docker Desktop the daemon lives in a separate netns, so host-mode containers are unreachable from the WSL shell — verify such setups from inside the container (`docker compose exec`), not via published ports.
- The proxy has no built-in authentication — anything that can reach the bind address can spend every node's API key. Compose deliberately binds loopback; front it with auth before exposing it.
- `config.json` is a client-side IDE snippet (points tools at `http://127.0.0.1:8080/v1`), not server config.
- Compose `environment:` overrides `env_file:` — don't pin `PROXY_BIND_PORT` in compose or `.env` changes to it silently stop applying (this bit once already).
- CI's container smoke test sets `-e PROXY_BIND_HOST=0.0.0.0` on purpose: published ports forward to the container's eth0, and standard runner Docker can't reach a loopback-bound service through them (local Docker 29 happens to bridge it, so this reproduces only in CI). Host side stays `-p 127.0.0.1:8080:8080`; don't "simplify" the override away.

## Agent skills

### Implementation workflow

Before committing anything, opening a PR, or merging: follow the gated implementation workflow. See `docs/agents/implementation-workflow.md`.

### Issue tracker

Issues live in GitHub Issues at ZeroClue/llm-rotator via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary — label strings equal the five canonical role names. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.
