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

- [ ] Untrack the committed bytecode: `git rm -r --cached __pycache__` (ignore rules now exist; tracked files ignore them).
- [ ] LICENSE file referenced by README doesn't exist — add MIT text or drop the claim.
- [ ] `tailscale_acl.json`, `PR_DESCRIPTION.md` review: keep or fold into docs/.
