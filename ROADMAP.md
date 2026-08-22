# ROADMAP.md

Prioritized future work for the LLM rotator proxy. Each item lists the rationale
and a concrete acceptance test. Findings context: `FINDINGS.md` (gitignored);
operational gotchas live in `AGENTS.md`.

Status vocabulary: `todo` · `in progress` · `done`.

---

## Now (high value, small effort)

### 1. CI pipeline — `done` (2026-08-21)
GitHub Actions (or equivalent) running on every push/PR:
1. `pip install -r requirements.txt && pip install pytest` → `pytest tests/ -v`
2. `python3 -m py_compile rotator.py`
3. `docker build` + run container with dummy env + `curl /health`

*Rationale:* requirements.txt rotted into prose and nobody noticed for exactly
this reason; the image was unbuildable while README claimed production readiness.
*Acceptance:* a commit that breaks tests or the build turns CI red.

### 2. Optional bearer-token auth in the proxy — `done` (2026-08-21)
New env var `PROXY_AUTH_TOKEN` (empty = disabled, current behavior). When set,
reject requests whose `Authorization` doesn't match before proxying; `/health`
stays open for orchestrators.
*Rationale:* the proxy spends every node's API key on behalf of any caller;
containerization makes exposure (`PROXY_BIND_HOST=0.0.0.0`) tempting and currently
reckless. ~15 lines + tests.
*Acceptance:* with token set, mismatched requests get 401 and never reach upstream
(mock capture proves it); unset behaves exactly as today.

### 3. Node-aware readiness — `done` (2026-08-21)
Add `nodes_available` (count of nodes not in cooldown) to `/health`; optionally a
separate `/ready` returning 503 when zero nodes are usable.
*Rationale:* `/health` reports `healthy` even with every node dead — orchestrators
can't distinguish "process up" from "service usable". Data already exists in
`node_iterator.snapshot()`.
*Acceptance:* kill all node targets, `/ready` flips to 503 within one cooldown
window; `/health` unchanged shape otherwise.

### 13. Deterministic clock/sleeper seams — `done` (2026-08-21)
Pass clock/sleeper/rng as plain parameters into the timing code
(`time.monotonic` at rotator.py:215/257/269, `random.uniform` at :91); stdlib
functions remain the production adapters.
Amendment: `time.sleep` at :1006 was **deferred to item 14** — a Flask view can't
take injected parameters, and #14's send-result interface is the natural home for
sleeper injection. Nothing asserts on that sleep today.
*Rationale:* cooldown expiry is currently tested by really sleeping 0.12 s and
asserting a range (`index in (1, 2)`) — slow and scheduler-dependent; backoff
bounds are inequality-only because jitter is unseedable. Prerequisite for clean
tests in items 14 and 17. From the 2026-08-21 architecture review.
*Acceptance:* no real sleeps for timing assertions in the suite;
failure→cooldown→skip→expiry covered deterministically via a stepped virtual clock.

## Next (real work, plan before starting)

### 4. Graceful stream draining — `done` (2026-08-22, issue #30)
On SIGTERM/gunicorn graceful shutdown, in-flight SSE completions are cut at
`graceful_timeout`. Options: short `Retry-After`-style SSE error event before cut,
or hold shutdown until streams finish with a bounded drain window env var
(`GUNICORN_GRACEFUL_TIMEOUT` already plumbed).
Amendment: finish-first draining — streams pump until natural completion or
`STREAM_DRAIN_WINDOW` (default 20s, kept below graceful_timeout), then end with a
terminal SSE event (OpenAI-style error + `[DONE]`). Fact-find against pinned
gunicorn 26.1.0 showed `worker_int` never fires on SIGTERM (only INT/QUIT), so
arming is chained SIGTERM/SIGINT handlers installed in `create_app()` — under
gunicorn they delegate to the worker's own handler; the bare dev server gets a
drain-wait-then-exit watchdog (with a settle beat so guards flush the event).
Guard lives in the view layer; ADR 0001.
*Acceptance:* integration test: start stream, SIGTERM master, client either receives
the full body within the drain window or a well-formed terminal SSE event — landed
as `tests/test_graceful_shutdown_live.py` (gunicorn full-body path, gunicorn cut
path before hard kill, dev-server parity).

### 5. Idempotency-aware retry policy — `done` (2026-08-22)
Today POSTs are retried verbatim on 502/503/504 — a 504 may mean upstream completed
(double billing). Add per-attempt budget note in logs, consider honoring
`X-Request-Id` passthrough, document the trade-off, or gate POST-retry behind an env
flag defaulting to current behavior.
Amendment: `RETRY_POSTS` env (default `true` = unchanged) gates verbatim POST
failover; `false` gives POSTs one attempt, ledger still records the outcome.
Trade-off documented in `.env.example` + AGENTS.md. Per-attempt budget already in
the attempt log; `X-Request-Id` passthrough deferred to item 9 (observability).
*Acceptance:* documented policy + flag test; no silent behavior change by default.

### 6. Session/threading hygiene — `done` (2026-08-22)
Module-level shared `requests.Session`: cookie-jar mutation races under gthread;
client cookies are forwarded upstream (privacy leak across egress nodes). Stop
forwarding cookies (drop `cookies=` unless a use case appears) and evaluate
per-thread sessions or disabling cookie persistence entirely.
Amendment: landed on the failover transport (#14's seam) — client cookies are
dropped entirely (both the removed `cookies=` channel *and* the `Cookie`
header, which rode general header passthrough), and the production session
refuses to store any `Set-Cookie` (`_NoStoreCookiePolicy`): never-mutating jar,
no race, no bleed, keep-alive kept. Per-thread sessions rejected as unneeded.
*Acceptance:* suite passes without cookie forwarding; concurrent-load smoke shows no
cross-request cookie bleed.

### 7. Cache key redesign or removal — `done` (2026-08-22)
The optimization cache is keyed on the MD5 of the entire message array; growing
conversations ≈ never hit, so it mostly adds memory and risk. Either key on
(stable prefix + model params), make hits append-aware, or remove the feature
until a design exists that measurably hits.
Amendment: removed outright (maintainer confirmed exact-repeats are unlikely);
flags hard-deleted rather than deprecated-in-place — stale `.env` entries go
inert. Determinism replaces memoization as the tested invariant. If multi-
tenant scale ever appears, reintroduce behind a measured hit-rate design.
*Acceptance:* measured hit-rate improvement on a simulated growing conversation, or
feature removed with its config flags deprecated.

### 8. README rewrite — `done` (2026-08-22)
README still documents ~10 nonexistent env vars, `/health/detailed`, wrong bind var
names, Python 3.8+ support. Rewrite against actual code; keep AGENTS.md as the
agent-facing source of truth and link both.
Amendment: rewritten from a grep-verified inventory — env-var tables now match
rotator.py/gunicorn.conf.py exactly (including RETRY_POSTS/PROXY_AUTH_TOKEN/
GUNICORN_*), endpoints include /ready, /health example is the real JSON shape,
Python 3.10+. Acceptance run mechanically: zero fake vars/endpoints.
*Acceptance:* every env var/endpoint in README greps clean against `rotator.py`.

### 14. Extract failover transport from the proxy view — `done` (2026-08-22)
Give retry/failover its own module behind a send-result interface: node pick, key
injection, proxy mapping, status classification (429/5xx), Retry-After parsing,
backoff, cooldown reporting, header hygiene, and the streaming generator all sit
behind one seam; the `/v1/*` view and `list_models` become thin callers
(requests session = production adapter, scripted fake = test adapter).
Amendment: landed as `failover.py` (`FailoverTransport.send()` →
`SendResult | AllNodesFailed`, framework-free); `list_models` inherited full
failover + header hygiene (its hardcoded `timeout=10` unified to
`REQUEST_TIMEOUT`); sleeper injection from #13 landed here. Design record:
issue #10 comment, 2026-08-22.
*Rationale:* the deletion test already failed in production — `list_models`
(rotator.py:1037–1054) re-implemented node pick/key/proxies shallowly with a
hardcoded `timeout=10` and leaked hop-by-hop headers; meanwhile every new feature
lands inside the view's loop body (:861–1020, ~21 responsibilities). From the
2026-08-21 architecture review.
*Acceptance:* `list_models` inherits failover + header hygiene from the same
module; "429 → cooldown → retry" and 502-exhaustion unit-tested without HTTP;
the drift-twin logic is deleted.

### 15. App factory replacing import-time execution — `done` (2026-08-22)
Move node-pool/session/optimizer/logging construction out of module scope into a
factory that builds settings → services → Flask app; keep a gunicorn-compatible
module-level `app` as a one-line call to it. Fold in the env-contract cleanup:
Dockerfile/compose healthchecks and gunicorn bind defaults read the same settings
instead of drifting copies (compose hardcodes `:8080` today; bind default differs
between rotator.py and gunicorn.conf.py).
Amendment: `create_app(cfg, optimization_config)` + PEP 562 lazy `rotator.app`
(bare import constructs nothing); frozen `Settings`; factory assigns the existing
module globals so views/tests kept their seams. Bind defaults unified on loopback;
compose healthcheck follows `$PROXY_BIND_PORT`. Design record: issue #27.
*Rationale:* importing `rotator` executes everything and `SystemExit(1)`s when
unconfigured; config frozen per process forces three escalating test workarounds
(conftest env-before-import, env scrubbing in the streaming test,
subprocess-per-assertion for LOG_LEVEL). From the 2026-08-21 architecture review.
*Acceptance:* bare `import rotator` neither exits nor builds network objects;
LOG_LEVEL testable in-process; conftest env-juggling removed; changing
`PROXY_BIND_PORT` in `.env` yields a healthy container.

### 16. Deepen compression pipeline contract — `done` (2026-08-22)
Make optimization pure payload-in/payload-out, owning its own routing
(chat/completions check) and enabling (single gate); one config value object
built in one place replaces ~15 module-global reads (including `ENABLE_PROMPT_CACHING`
computed twice at :106/:146); a no-op adapter covers the disabled case; an explicit
error mode replaces the caller's blanket except. Relates to item 7 (cache design).
Amendment: landed as frozen `OptimizationConfig.from_env()` (profile + env folded
once) + `optimize_context(payload, *, path, is_streaming)` with copy-on-write
purity and an optimizer-owned never-break-proxying degradation policy; stage tests
now drive the public interface (`tests/test_optimization.py`). Design record:
issue #17 comment, 2026-08-22.
*Rationale:* newest code (fa69cac/7df096e) concentrates the smear: caller owns
routing + flag pre-check, payload mutated in place AND returned, `max_tokens`
rewritten silently, cross-request cache invisible to callers; six riskiest stages
(truncation ladder, dedup, importance, summarization stub) have zero tests because
tests reach past the interface into private methods. From the 2026-08-21
architecture review.
*Acceptance:* config parsed in exactly one place; flag tests use constructor
params instead of monkeypatched module globals; truncation/dedup/importance stages
tested through the public interface.

### 17. Split round-robin cursor from node-health ledger — `done` (2026-08-22)
Type nodes (dataclass) and split `ThreadSafeIterator`'s two roles: cursor vs
failure/cooldown ledger taking time as an argument (pairs with item 13).
Amendment: landed as frozen `Node` dataclass + `NodeSelector`/`HealthLedger`
sharing one reentrant lock; ledger raises `ValueError` on out-of-pool nodes
instead of silently no-oping. Design record: issue #11 comment, 2026-08-22.
*Rationale:* the node dict shape is established twice (:172–173, :208–210)
because neither constructor trusts the other; cooldown math/consumption/display/
reset live in four neighborhoods; `report_failure` silently no-ops when handed a
dict without `node_id`. From the 2026-08-21 architecture review.
*Acceptance:* typo'd node fields are constructor errors, not silent state; health
rules (record_failure / usable / cooldown_remaining) unit-tested without sleeps
or HTTP.

## Later (nice-to-have / strategic)

### 9. Observability — `in progress` (2026-08-22, issue #34)
Request IDs (generate + echo via header), structured logging (JSON option),
per-node success/failure counters exposed in `/health`, optional Prometheus
endpoint. Prerequisite for operating multi-node pools with confidence.
Amendment (design settled 2026-08-22, issue #34): request IDs honor inbound
`X-Request-Id` (sanitized) else `uuid4().hex`, echoed on every response,
proxy-local (not forwarded upstream); `LOG_FORMAT=json` stdlib formatter with
structured extras on the ~7 request-lifecycle sites; counters recorded at the
ledger's existing mutation sites, surfaced additively in `/health`; `/metrics`
hand-rolled text exposition, unauthenticated like `/health`, no new dependency.
Non-goals: histograms, per-path labels, persistence, upstream forwarding.

### 10. Supply-chain hardening — `todo`
Digest-pin `python:3.12-slim`, add OCI labels, consider multi-stage build,
multi-arch manifest. Low urgency single-host, high value if images get pushed.

### 11. Optional llmlingua image variant — `todo`
Compose profile (`--profile semantic`) building a torch-included variant (~2GB)
for `ENABLE_SEMANTIC_COMPRESSION=true` users; base image stays lean.

### 12. Smarter rotation — `todo`
Weighted nodes (capacity-based), per-node rate budgets, mid-stream failover is
impossible by design (document it); consider `max_completion_tokens` passthrough
for newer OpenAI models alongside legacy `max_tokens`.

---

## Repo hygiene quick-wins

- [x] Untrack the committed bytecode — done 2026-08-21 (`git rm -r --cached __pycache__`; files remain on disk, now ignored).
- [x] LICENSE file referenced by README didn't exist — MIT text added (holder: ZeroClue, matching git identity/repo org).
- [x] `tailscale_acl.json`, `PR_DESCRIPTION.md` review — both deleted 2026-08-21: the ACL file was invalid JSON/HuJSON duplicating README's Security section; PR_DESCRIPTION described merged PRs and contained inaccurate claims (e.g., `/health/detailed`). Recoverable from git history.
