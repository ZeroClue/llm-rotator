# ROADMAP.md

Prioritized future work for the LLM rotator proxy. Each item lists the rationale
and a concrete acceptance test. Findings context: `FINDINGS.md` (gitignored);
operational gotchas live in `AGENTS.md`.

Status vocabulary: `todo` · `in progress` · `done`.

---

## Now (high value, small effort)

### 1. CI pipeline — `todo`
GitHub Actions (or equivalent) running on every push/PR:
1. `pip install -r requirements.txt && pip install pytest` → `pytest tests/ -v`
2. `python3 -m py_compile rotator.py`
3. `docker build` + run container with dummy env + `curl /health`

*Rationale:* requirements.txt rotted into prose and nobody noticed for exactly
this reason; the image was unbuildable while README claimed production readiness.
*Acceptance:* a commit that breaks tests or the build turns CI red.

### 2. Optional bearer-token auth in the proxy — `todo`
New env var `PROXY_AUTH_TOKEN` (empty = disabled, current behavior). When set,
reject requests whose `Authorization` doesn't match before proxying; `/health`
stays open for orchestrators.
*Rationale:* the proxy spends every node's API key on behalf of any caller;
containerization makes exposure (`PROXY_BIND_HOST=0.0.0.0`) tempting and currently
reckless. ~15 lines + tests.
*Acceptance:* with token set, mismatched requests get 401 and never reach upstream
(mock capture proves it); unset behaves exactly as today.

### 3. Node-aware readiness — `todo`
Add `nodes_available` (count of nodes not in cooldown) to `/health`; optionally a
separate `/ready` returning 503 when zero nodes are usable.
*Rationale:* `/health` reports `healthy` even with every node dead — orchestrators
can't distinguish "process up" from "service usable". Data already exists in
`node_iterator.snapshot()`.
*Acceptance:* kill all node targets, `/ready` flips to 503 within one cooldown
window; `/health` unchanged shape otherwise.

### 13. Deterministic clock/sleeper seams — `todo`
Pass clock/sleeper/rng as plain parameters into the timing code
(`time.monotonic` at rotator.py:214/255/265, `time.sleep` at :1006,
`random.uniform` at :91); stdlib functions remain the production adapters.
*Rationale:* cooldown expiry is currently tested by really sleeping 0.12 s and
asserting a range (`index in (1, 2)`) — slow and scheduler-dependent; backoff
bounds are inequality-only because jitter is unseedable. Prerequisite for clean
tests in items 14 and 17. From the 2026-08-21 architecture review.
*Acceptance:* no real sleeps for timing assertions in the suite;
failure→cooldown→skip→expiry covered deterministically via a stepped virtual clock.

## Next (real work, plan before starting)

### 4. Graceful stream draining — `todo`
On SIGTERM/gunicorn graceful shutdown, in-flight SSE completions are cut at
`graceful_timeout`. Options: short `Retry-After`-style SSE error event before cut,
or hold shutdown until streams finish with a bounded drain window env var
(`GUNICORN_GRACEFUL_TIMEOUT` already plumbed).
*Acceptance:* integration test: start stream, SIGTERM master, client either receives
the full body within the drain window or a well-formed terminal SSE event.

### 5. Idempotency-aware retry policy — `todo`
Today POSTs are retried verbatim on 502/503/504 — a 504 may mean upstream completed
(double billing). Add per-attempt budget note in logs, consider honoring
`X-Request-Id` passthrough, document the trade-off, or gate POST-retry behind an env
flag defaulting to current behavior.
*Acceptance:* documented policy + flag test; no silent behavior change by default.

### 6. Session/threading hygiene — `todo`
Module-level shared `requests.Session`: cookie-jar mutation races under gthread;
client cookies are forwarded upstream (privacy leak across egress nodes).
Stop forwarding cookies (drop `cookies=` unless a use case appears) and evaluate
per-thread sessions or disabling cookie persistence entirely.
*Acceptance:* suite passes without cookie forwarding; concurrent-load smoke shows no
cross-request cookie bleed.

### 7. Cache key redesign or removal — `todo`
The optimization cache is keyed on the MD5 of the entire message array; growing
conversations ≈ never hit, so it mostly adds memory and risk. Either key on
(stable prefix + model params), make hits append-aware, or remove the feature until
a design exists that measurably hits.
*Acceptance:* measured hit-rate improvement on a simulated growing conversation, or
feature removed with its config flags deprecated.

### 8. README rewrite — `todo`
README still documents ~10 nonexistent env vars, `/health/detailed`, wrong bind var
names, Python 3.8+ support. Rewrite against actual code; keep AGENTS.md as the
agent-facing source of truth and link both.
*Acceptance:* every env var/endpoint in README greps clean against `rotator.py`.

### 14. Extract failover transport from the proxy view — `todo`
Give retry/failover its own module behind a send-result interface: node pick, key
injection, proxy mapping, status classification (429/5xx), Retry-After parsing,
backoff, cooldown reporting, header hygiene, and the streaming generator all sit
behind one seam; the `/v1/*` view and `list_models` become thin callers
(requests session = production adapter, scripted fake = test adapter).
*Rationale:* the deletion test already failed in production — `list_models`
(rotator.py:1037–1054) re-implemented node pick/key/proxies shallowly with a
hardcoded `timeout=10` and leaked hop-by-hop headers; meanwhile every new feature
lands inside the view's loop body (:861–1020, ~21 responsibilities). From the
2026-08-21 architecture review.
*Acceptance:* `list_models` inherits failover + header hygiene from the same
module; "429 → cooldown → retry" and 502-exhaustion unit-tested without HTTP;
the drift-twin logic is deleted.

### 15. App factory replacing import-time execution — `todo`
Move node-pool/session/optimizer/logging construction out of module scope into a
factory that builds settings → services → Flask app; keep a gunicorn-compatible
module-level `app` as a one-line call to it. Fold in the env-contract cleanup:
Dockerfile/compose healthchecks and gunicorn bind defaults read the same settings
instead of drifting copies (compose hardcodes `:8080` today; bind default differs
between rotator.py and gunicorn.conf.py).
*Rationale:* importing `rotator` executes everything and `SystemExit(1)`s when
unconfigured; config frozen per process forces three escalating test workarounds
(conftest env-before-import, env scrubbing in the streaming test,
subprocess-per-assertion for LOG_LEVEL). From the 2026-08-21 architecture review.
*Acceptance:* bare `import rotator` neither exits nor builds network objects;
LOG_LEVEL testable in-process; conftest env-juggling removed; changing
`PROXY_BIND_PORT` in `.env` yields a healthy container.

### 16. Deepen compression pipeline contract — `todo`
Make optimization pure payload-in/payload-out, owning its own routing
(chat/completions check) and enabling (single gate); one config value object built
in one place replaces ~15 module-global reads (including `ENABLE_PROMPT_CACHING`
computed twice at :106/:146); a no-op adapter covers the disabled case; an explicit
error mode replaces the caller's blanket except. Relates to item 7 (cache design).
*Rationale:* newest code (fa69cac/7df096e) concentrates the smear: caller owns
routing + flag pre-check, payload mutated in place AND returned, `max_tokens`
rewritten silently, cross-request cache invisible to callers; six riskiest stages
(truncation ladder, dedup, importance, summarization stub) have zero tests because
tests reach past the interface into private methods. From the 2026-08-21
architecture review.
*Acceptance:* config parsed in exactly one place; flag tests use constructor
params instead of monkeypatched module globals; truncation/dedup/importance stages
tested through the public interface.

### 17. Split round-robin cursor from node-health ledger — `todo`
Type nodes (dataclass) and split `ThreadSafeIterator`'s two roles: cursor vs
failure/cooldown ledger taking time as an argument (pairs with item 13).
*Rationale:* the node dict shape is established twice (:172–173, :208–210)
because neither constructor trusts the other; cooldown math/consumption/display/
reset live in four neighborhoods; `report_failure` silently no-ops when handed a
dict without `node_id`. From the 2026-08-21 architecture review.
*Acceptance:* typo'd node fields are constructor errors, not silent state; health
rules (record_failure / usable / cooldown_remaining) unit-tested without sleeps
or HTTP.

## Later (nice-to-have / strategic)

### 9. Observability — `todo`
Request IDs (generate + echo via header), structured logging (JSON option),
per-node success/failure counters exposed in `/health`, optional Prometheus
endpoint. Prerequisite for operating multi-node pools with confidence.

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
